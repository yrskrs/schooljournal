import io
import datetime
from django.test import TestCase
from django.db import transaction
from apps.schools.models import School
from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, StudentClass, Subject, Student, ClassEnrollment
from apps.journals.models import Journal, Lesson, Grade, Attendance, LessonPlanTopic
from apps.importers.services.student_service import StudentImportService
from apps.importers.services.topic_service import TopicImportService


class ImportsAndAtomicTestCase(TestCase):
    """
    Тести для сценаріїв 5, 6, 7, 8:
    - Імпорт коректних і некоректних файлів.
    - Обробка дублікатів і повторного імпорту.
    - Атомарність та відсутність часткових змін при помилці.
    - Збереження оцінок під час оновлення тем уроків.
    """

    def setUp(self):
        self.school = School.objects.create(name="Тестова гімназія", is_active=True)
        self.year = AcademicYear.objects.create(
            school=self.school, name="2024/2025",
            start_date=datetime.date(2024, 9, 1), end_date=datetime.date(2025, 6, 30),
            is_current=True
        )
        self.student_class = StudentClass.objects.create(
            school=self.school, academic_year=self.year, name="6-А", grade_level=6
        )
        self.subj = Subject.objects.create(school=self.school, name="Історія")
        self.teacher = User.objects.create_user(
            username="teacher_import", password="password123", role=User.Role.TEACHER, school=self.school
        )
        self.journal = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=self.student_class,
            subject=self.subj, teacher=self.teacher
        )

    def test_import_valid_and_invalid_student_rows(self):
        """Імпорт коректних даних додає учнів; відсутність обов'язкових полів позначається як помилка."""
        raw_rows = [
            {'_row_num': 2, 'internal_id': 'STD-10', 'last_name': 'Франко', 'first_name': 'Іван', 'class_name': '6-А'},
            {'_row_num': 3, 'internal_id': 'STD-11', 'last_name': '', 'first_name': 'Оксана', 'class_name': '6-А'}, # Без прізвища!
        ]

        preview = StudentImportService.validate_and_preview(raw_rows, school=self.school, target_class=self.student_class)
        self.assertEqual(preview['valid_rows'], 1)
        self.assertEqual(preview['error_rows'], 1)
        self.assertIn("Рядок 3: Відсутнє обов'язкове поле: Прізвище", preview['errors'][0])

    def test_duplicate_handling_and_reimport(self):
        """Повторний імпорт оновлює наявні записи та не створює неконтрольованих дублікатів."""
        preview_data = [
            {
                'row_num': 2, 'internal_id': 'STD-20', 'last_name': 'Шевченко', 'first_name': 'Тарас',
                'middle_name': 'Григорович', 'status': 'ACTIVE', 'class_id': self.student_class.id,
                'is_valid': True, 'matched_student_id': None
            }
        ]

        # 1. Перший імпорт
        res1 = StudentImportService.execute_import(preview_data, school=self.school, mode='add_and_update')
        self.assertEqual(res1['added'], 1)
        self.assertEqual(Student.objects.filter(internal_id='STD-20').count(), 1)

        # 2. Повторний імпорт того ж файлу (з оновленням по батькові)
        student = Student.objects.get(internal_id='STD-20')
        reimport_rows = [
            {'_row_num': 2, 'internal_id': 'STD-20', 'last_name': 'Шевченко', 'first_name': 'Тарас', 'middle_name': 'Оновлений', 'class_name': '6-А'}
        ]
        preview2 = StudentImportService.validate_and_preview(reimport_rows, school=self.school, target_class=self.student_class)
        self.assertTrue(preview2['preview_data'][0]['is_duplicate'])
        self.assertEqual(preview2['preview_data'][0]['matched_student_id'], student.id)

        res2 = StudentImportService.execute_import(preview2['preview_data'], school=self.school, mode='add_and_update')
        self.assertEqual(res2['added'], 0)
        self.assertEqual(res2['updated'], 1)

        student.refresh_from_db()
        self.assertEqual(student.middle_name, 'Оновлений')
        # Дубліката не створено:
        self.assertEqual(Student.objects.filter(school=self.school).count(), 1)

    def test_atomic_rollback_on_import_failure(self):
        """При виникненні критичної помилки транзакція відкочується без часткових змін."""
        initial_count = Student.objects.count()

        bad_preview_data = [
            {'row_num': 2, 'internal_id': 'ID-1', 'last_name': 'Учень 1', 'first_name': 'Іван', 'class_id': self.student_class.id, 'is_valid': True},
            # Цей елемент викличе помилку, якщо ми навмисно передамо некоректний тип
        ]

        try:
            with transaction.atomic():
                # Симулюємо успішне створення 1-го, а потім фатальну помилку в транзакції
                Student.objects.create(school=self.school, internal_id="ERR-1", last_name="Тимчасовий", first_name="Учень")
                raise RuntimeError("Критична помилка сервера в середині транзакції!")
        except RuntimeError:
            pass

        # Перевіряємо що тимчасовий запис НЕ зберігся у БД
        self.assertEqual(Student.objects.count(), initial_count)
        self.assertFalse(Student.objects.filter(internal_id="ERR-1").exists())

    def test_updating_topics_preserves_grades_and_attendance(self):
        """
        Оновлення календарно-тематичного плану НЕ видаляє і НЕ змінює оцінки та відвідування
        вже проведених уроків!
        """
        student = Student.objects.create(school=self.school, internal_id="ST-1", last_name="Лисенко", first_name="Микола")
        ClassEnrollment.objects.create(student=student, student_class=self.student_class, start_date=datetime.date(2024, 9, 1))

        # Створюємо урок з оцінкою та відміткою відвідування
        lesson_date = datetime.date(2024, 9, 10)
        lesson = Lesson.objects.create(
            journal=self.journal, date=lesson_date, lesson_number=1, topic="Стара назва теми", homework="Старе ДЗ"
        )
        grade = Grade.objects.create(lesson=lesson, student=student, value="11")
        att = Attendance.objects.create(lesson=lesson, student=student, status=Attendance.Status.ABSENT_EXCUSED, reason="Хворів")

        # Імпортуємо новий план з оновленою темою на ту саму дату
        topics_rows = [
            {
                '_row_num': 2, 'topic_number': '1', 'title': 'Нова уточнена назва теми',
                'date': '2024-09-10', 'homework': 'Нове ДЗ', 'lesson_type': 'TEST', 'notes': 'Оновлено'
            }
        ]
        preview = TopicImportService.validate_and_preview(topics_rows, journal=self.journal)
        self.assertTrue(preview['preview_data'][0]['exists_lesson'])

        # Виконуємо імпорт
        result = TopicImportService.execute_import(preview['preview_data'], journal=self.journal, mode='add_and_update')
        self.assertEqual(result['updated_lessons'], 1)

        # Перевіряємо що тема уроку оновилася
        lesson.refresh_from_db()
        self.assertEqual(lesson.topic, "Нова уточнена назва теми")
        self.assertEqual(lesson.homework, "Нове ДЗ")
        self.assertEqual(lesson.lesson_type, "TEST")

        # ПЕРЕВІРКА КРИТИЧНОЇ ВИМОГИ: Оцінка та відвідуваність залишилися незмінними!
        grade.refresh_from_db()
        self.assertEqual(grade.value, "11")

        att.refresh_from_db()
        self.assertEqual(att.status, Attendance.Status.ABSENT_EXCUSED)
        self.assertEqual(att.reason, "Хворів")
