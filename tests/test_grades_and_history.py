import datetime
from django.test import TestCase, Client
from django.urls import reverse
from apps.schools.models import School
from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, Subject, StudentClass, Student, ClassEnrollment
from apps.journals.models import Journal, Lesson, Grade, Attendance, AuditLog


class GradesAndHistoryTestCase(TestCase):
    """
    Тести для сценаріїв 3 і 4:
    - Внесення й редагування оцінок (шкала 1-12, порожні клітинки, аудит, конфлікти).
    - Збереження історії та оцінок після переведення учня між класами.
    """

    def setUp(self):
        self.school = School.objects.create(name="Тестова школа", is_active=True)
        self.year = AcademicYear.objects.create(
            school=self.school, name="2024/2025",
            start_date=datetime.date(2024, 9, 1), end_date=datetime.date(2025, 6, 30),
            is_current=True
        )
        self.subj = Subject.objects.create(school=self.school, name="Математика")
        self.class_a = StudentClass.objects.create(school=self.school, academic_year=self.year, name="5-А", grade_level=5)
        self.class_b = StudentClass.objects.create(school=self.school, academic_year=self.year, name="5-Б", grade_level=5)

        self.teacher = User.objects.create_user(
            username="teacher_grades", password="password123", role=User.Role.TEACHER, school=self.school
        )

        self.journal_a = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=self.class_a,
            subject=self.subj, teacher=self.teacher
        )
        self.journal_b = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=self.class_b,
            subject=self.subj, teacher=self.teacher
        )

        self.student = Student.objects.create(
            school=self.school, internal_id="STD-001",
            last_name="Коваленко", first_name="Іван", status=Student.Status.ACTIVE
        )
        self.enrollment_a = ClassEnrollment.objects.create(
            student=self.student, student_class=self.class_a,
            start_date=datetime.date(2024, 9, 1), is_active=True
        )

        self.lesson1 = Lesson.objects.create(
            journal=self.journal_a, date=datetime.date(2024, 9, 5), lesson_number=1, topic="Урок 1"
        )
        self.lesson2 = Lesson.objects.create(
            journal=self.journal_a, date=datetime.date(2024, 9, 8), lesson_number=1, topic="Урок 2"
        )

    def test_grade_entry_and_audit_logging(self):
        """Внесення коректної оцінки (1-12) зберігається в БД та фіксується в аудиті."""
        client = Client()
        client.login(username="teacher_grades", password="password123")

        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '11',
        })
        self.assertEqual(response.status_code, 200)

        grade = Grade.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(grade.value, '11')
        self.assertEqual(grade.numeric_value, 11)

        # Перевірка запису в AuditLog
        log = AuditLog.objects.filter(target_model='Grade', target_id=str(grade.id)).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.action, AuditLog.Action.CREATE)
        self.assertEqual(log.changes.get('value', {}).get('new'), '11')

    def test_empty_grade_is_not_treated_as_zero(self):
        """Порожня клітинка очищує оцінку і НЕ є нулем."""
        Grade.objects.create(lesson=self.lesson1, student=self.student, value='10')

        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '',
        })
        self.assertEqual(response.status_code, 200)

        grade = Grade.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(grade.value, '')
        self.assertIsNone(grade.numeric_value)

    def test_invalid_grade_rejected(self):
        """Оцінка поза межами 1-12 відхиляється із повідомленням про помилку (400)."""
        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '15',
        })
        self.assertEqual(response.status_code, 400)

    def test_conflict_detection_returns_409(self):
        """Оптимістичне блокування повертає 409 Conflict при паралельному редагуванні."""
        grade = Grade.objects.create(lesson=self.lesson1, student=self.student, value='8')

        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})

        # Клієнт надсилає застарілу версію (час у минулому на 100 секунд)
        stale_client_version = str(grade.updated_at.timestamp() - 100)
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '9',
            'client_version': stale_client_version
        })
        self.assertEqual(response.status_code, 409)

    def test_student_transfer_preserves_historical_grades(self):
        """
        Переведення учня до іншого класу зберігає попередні оцінки у вихідному класі.
        Учень залишається у списку старого журналу з відміткою про вибуття.
        """
        # 1. Виставляємо оцінки в 5-А
        Grade.objects.create(lesson=self.lesson1, student=self.student, value='12')
        Grade.objects.create(lesson=self.lesson2, student=self.student, value='11')

        # 2. Переводимо учня з 5-А до 5-Б
        self.enrollment_a.is_active = False
        self.enrollment_a.end_date = datetime.date(2024, 10, 1)
        self.enrollment_a.transfer_note = "Переведено до 5-Б"
        self.enrollment_a.save()

        ClassEnrollment.objects.create(
            student=self.student,
            student_class=self.class_b,
            start_date=datetime.date(2024, 10, 1),
            is_active=True
        )

        # 3. Перевіряємо журнал 5-А
        client = Client()
        client.login(username="teacher_grades", password="password123")
        url_5a = reverse('portal:journal_detail', kwargs={'journal_id': self.journal_a.id})
        response_5a = client.get(url_5a)
        self.assertEqual(response_5a.status_code, 200)

        # Учень повинен бути присутній у контексті та мати оцінки!
        student_rows = response_5a.context['student_rows']
        found = next((r for r in student_rows if r['student'].id == self.student.id), None)
        self.assertIsNotNone(found)
        self.assertFalse(found['is_active'])
        self.assertIn("вибув", found['status_note'])

        grades_map = response_5a.context['grades']
        self.assertEqual(grades_map.get((self.lesson1.id, self.student.id)).value, '12')
        self.assertEqual(grades_map.get((self.lesson2.id, self.student.id)).value, '11')

    def test_unified_cell_attendance_entry(self):
        """Внесення 'Н', 'ХВ' або 'ЗП' через єдину комірку фіксує відвідуваність і скидає оцінку."""
        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})

        # 1. Виставляємо спочатку оцінку
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '10',
        })
        self.assertEqual(response.status_code, 200)
        grade = Grade.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(grade.value, '10')

        # 2. Через ту саму комірку вносимо 'Н' (пропуск)
        response_att = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': 'Н',
        })
        self.assertEqual(response_att.status_code, 200)
        att = Attendance.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(att.status, Attendance.Status.ABSENT_UNEXCUSED)

        # Оцінка очистилась
        grade.refresh_from_db()
        self.assertEqual(grade.value, '')

        # 3. Через ту саму комірку змінюємо на 'ХВ' (хвороба)
        response_att2 = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': 'ХВ',
        })
        self.assertEqual(response_att2.status_code, 200)
        att.refresh_from_db()
        self.assertEqual(att.status, Attendance.Status.ABSENT_EXCUSED)

    def test_unified_cell_switching_from_attendance_to_grade(self):
        """Внесення оцінки учню з позначкою 'Н' скидає пропуск у PRESENT і записує оцінку."""
        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal_a.id})

        # Спочатку Н
        client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': 'Н',
        })
        att = Attendance.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(att.status, Attendance.Status.ABSENT_UNEXCUSED)

        # Тепер вчитель вводить оцінку 12
        response = client.post(url, {
            'lesson_id': self.lesson1.id,
            'student_id': self.student.id,
            'value': '12',
        })
        self.assertEqual(response.status_code, 200)

        grade = Grade.objects.get(lesson=self.lesson1, student=self.student)
        self.assertEqual(grade.value, '12')
        att.refresh_from_db()
        self.assertEqual(att.status, Attendance.Status.PRESENT)

    def test_calendar_topics_rendered_below_journal(self):
        """Календарно-тематичний розділ відображається внизу журналу з датами та темами."""
        client = Client()
        client.login(username="teacher_grades", password="password123")
        url = reverse('portal:journal_detail', kwargs={'journal_id': self.journal_a.id})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)

        html = response.content.decode('utf-8')
        self.assertIn("Календарний план та теми уроків", html)
        self.assertIn("calendarSection", html)
        self.assertIn(self.lesson1.topic, html)
        self.assertIn(self.lesson2.topic, html)
        self.assertIn("journalQuickPicker", html)
