import datetime
from django.test import TestCase, Client
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from apps.schools.models import School
from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, Semester, Subject, StudentClass, TeacherAssignment
from apps.journals.models import Journal
from apps.portal.permissions import has_journal_access


class SchoolIsolationAndAccessTestCase(TestCase):
    """
    Тести для сценаріїв 1 і 2:
    - Розмежування доступу між школами та вчителями (Multi-tenancy).
    - Створення журналу лише для призначеного класу та предмета.
    - Унікальність журналів (запобігання дублікатам).
    """

    def setUp(self):
        # Школа 1
        self.school1 = School.objects.create(name="Школа №1", is_active=True)
        self.year1 = AcademicYear.objects.create(
            school=self.school1, name="2024/2025",
            start_date=datetime.date(2024, 9, 1), end_date=datetime.date(2025, 6, 30),
            is_current=True
        )
        self.subj1 = Subject.objects.create(school=self.school1, name="Математика")
        self.class1 = StudentClass.objects.create(
            school=self.school1, academic_year=self.year1, name="5-А", grade_level=5
        )
        self.teacher1 = User.objects.create_user(
            username="teacher1", password="password123", role=User.Role.TEACHER, school=self.school1
        )
        self.assignment1 = TeacherAssignment.objects.create(
            school=self.school1, teacher=self.teacher1, subject=self.subj1,
            student_class=self.class1, academic_year=self.year1
        )
        self.journal1 = Journal.objects.create(
            school=self.school1, academic_year=self.year1, student_class=self.class1,
            subject=self.subj1, teacher=self.teacher1
        )

        # Інший вчитель у Школі 1
        self.teacher1_other = User.objects.create_user(
            username="teacher1_other", password="password123", role=User.Role.TEACHER, school=self.school1
        )

        # Школа 2
        self.school2 = School.objects.create(name="Школа №2", is_active=True)
        self.year2 = AcademicYear.objects.create(
            school=self.school2, name="2024/2025",
            start_date=datetime.date(2024, 9, 1), end_date=datetime.date(2025, 6, 30),
            is_current=True
        )
        self.subj2 = Subject.objects.create(school=self.school2, name="Фізика")
        self.class2 = StudentClass.objects.create(
            school=self.school2, academic_year=self.year2, name="8-Б", grade_level=8
        )
        self.teacher2 = User.objects.create_user(
            username="teacher2", password="password123", role=User.Role.TEACHER, school=self.school2
        )
        self.journal2 = Journal.objects.create(
            school=self.school2, academic_year=self.year2, student_class=self.class2,
            subject=self.subj2, teacher=self.teacher2
        )

        # Суперадміністратор
        self.superadmin = User.objects.create_superuser(
            username="superadmin", password="password123", role=User.Role.SUPERADMIN
        )

    def test_teacher_of_school2_cannot_access_journal_of_school1(self):
        """Вчитель Школи 2 не повинен мати доступу до журналу Школи 1 (ізоляція шкіл)."""
        self.assertFalse(has_journal_access(self.teacher2, self.journal1, write=False))
        self.assertFalse(has_journal_access(self.teacher2, self.journal1, write=True))

        client = Client()
        client.login(username="teacher2", password="password123")
        url = reverse('portal:journal_detail', kwargs={'journal_id': self.journal1.id})
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_teacher_cannot_edit_other_teachers_journal_without_override(self):
        """Вчитель не може редагувати чужий журнал у своїй школі без дозволу на заміну."""
        # Перегляд дозволений або обмежений
        self.assertFalse(has_journal_access(self.teacher1_other, self.journal1, write=True))

        client = Client()
        client.login(username="teacher1_other", password="password123")
        # Спроба оновити оцінку в чужому журналі має повернути 403
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal1.id})
        response = client.post(url, {'lesson_id': 1, 'student_id': 1, 'value': '10'})
        self.assertEqual(response.status_code, 403)

    def test_teacher_can_edit_other_teachers_journal_with_override(self):
        """Вчитель може редагувати чужий журнал, якщо увімкнено дозвіл заміни (allows_override_teacher)."""
        self.journal1.allows_override_teacher = True
        self.journal1.save()
        self.assertTrue(has_journal_access(self.teacher1_other, self.journal1, write=True))

    def test_superadmin_has_full_access_to_all_journals(self):
        """Суперадміністратор має доступ до журналів усіх шкіл."""
        self.assertTrue(has_journal_access(self.superadmin, self.journal1, write=True))
        self.assertTrue(has_journal_access(self.superadmin, self.journal2, write=True))

    def test_unique_constraint_prevents_duplicate_journals(self):
        """База даних запобігає створенню однакових журналів (навчальний рік, клас, предмет)."""
        with self.assertRaises(IntegrityError):
            Journal.objects.create(
                school=self.school1,
                academic_year=self.year1,
                student_class=self.class1,
                subject=self.subj1,
                teacher=self.teacher1_other
            )

    def test_teacher_can_only_create_assigned_journal(self):
        """Вчитель може створювати журнал лише для призначеного класу та предмета."""
        client = Client()
        client.login(username="teacher1", password="password123")

        # Новий предмет без призначення для teacher1
        subj_unassigned = Subject.objects.create(school=self.school1, name="Хімія")
        assignment_fake = TeacherAssignment.objects.create(
            school=self.school1, teacher=self.teacher1_other, subject=subj_unassigned,
            student_class=self.class1, academic_year=self.year1
        )

        url = reverse('portal:create_journal')
        # Спроба використати чуже призначення
        response = client.post(url, {'assignment_id': assignment_fake.id})
        # Має повернути 404 (оскільки фільтрується за поточним користувачем)
        self.assertEqual(response.status_code, 404)
