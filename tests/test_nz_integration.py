import datetime
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, StudentClass, Subject, Student, ClassEnrollment, Semester
from apps.journals.models import Journal, Lesson, Grade, Attendance, NZAccount, NZSyncLog
from apps.journals.services.nz_service import NZService


class NZIntegrationTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Ліцей №1", edrpou_code="12345678")
        self.teacher = User.objects.create_user(
            username="teacher.nz",
            password="Password123!",
            role=User.Role.TEACHER,
            school=self.school,
            first_name="Оксана",
            last_name="Коваленко"
        )
        self.academic_year = AcademicYear.objects.create(
            school=self.school,
            name="2024/2025",
            start_date=datetime.date(2024, 9, 1),
            end_date=datetime.date(2025, 6, 30),
            is_current=True
        )
        self.semester = Semester.objects.create(
            academic_year=self.academic_year,
            number=1,
            start_date=datetime.date(2024, 9, 1),
            end_date=datetime.date(2024, 12, 31)
        )
        self.student_class = StudentClass.objects.create(
            school=self.school,
            academic_year=self.academic_year,
            name="5-А"
        )
        self.subject = Subject.objects.create(
            school=self.school,
            name="Математика"
        )
        self.journal = Journal.objects.create(
            school=self.school,
            academic_year=self.academic_year,
            student_class=self.student_class,
            subject=self.subject,
            teacher=self.teacher,
            nz_journal_id="998877"
        )
        self.student = Student.objects.create(
            school=self.school,
            last_name="Шевченко",
            first_name="Тарас",
            nz_student_id="st_101"
        )
        ClassEnrollment.objects.create(
            student=self.student,
            student_class=self.student_class,
            is_active=True
        )
        self.lesson = Lesson.objects.create(
            journal=self.journal,
            semester=self.semester,
            date=datetime.date(2024, 9, 10),
            lesson_number=1,
            topic="Вступний урок",
            nz_schedule_id="sch_501",
            is_synced_to_nz=True
        )
        self.nz_account = NZAccount.objects.create(
            user=self.teacher,
            login="teacher.nz@school.ua",
            phpsessid="mock_phpsessid_12345",
            is_active=True
        )

        self.client = Client()
        self.client.login(username="teacher.nz", password="Password123!")

    def test_grade_edit_marks_unsynced(self):
        """Редагування оцінки локально позначає її як несинхронізовану (is_synced_to_nz=False)."""
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal.id})
        response = self.client.post(url, {
            'lesson_id': self.lesson.id,
            'student_id': self.student.id,
            'value': '11',
        })
        self.assertEqual(response.status_code, 200)

        grade = Grade.objects.get(lesson=self.lesson, student=self.student)
        self.assertEqual(grade.value, '11')
        self.assertFalse(grade.is_synced_to_nz)

    def test_attendance_edit_marks_unsynced(self):
        """Внесення відвідуваності 'Н' через update_grade позначає її як is_synced_to_nz=False."""
        url = reverse('portal:update_grade', kwargs={'journal_id': self.journal.id})
        response = self.client.post(url, {
            'lesson_id': self.lesson.id,
            'student_id': self.student.id,
            'value': 'Н',
        })
        self.assertEqual(response.status_code, 200)

        att = Attendance.objects.get(lesson=self.lesson, student=self.student)
        self.assertEqual(att.status, Attendance.Status.ABSENT_UNEXCUSED)
        self.assertFalse(att.is_synced_to_nz)

    def test_lesson_edit_marks_unsynced(self):
        """Зміна теми уроку локально скидає is_synced_to_nz на False."""
        url = reverse('portal:lesson_edit', kwargs={'journal_id': self.journal.id, 'lesson_id': self.lesson.id})
        response = self.client.post(url, {
            'topic': 'Нова змінена тема',
            'homework': 'Впр. 45',
            'lesson_type': 'ORDINARY',
            'notes': '',
        })
        self.assertEqual(response.status_code, 302)

        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.topic, 'Нова змінена тема')
        self.assertFalse(self.lesson.is_synced_to_nz)

    def test_get_unsynced_changes_preview(self):
        """Сервіс коректно знаходить несинхронізовані дельти для журналу."""
        Grade.objects.create(
            lesson=self.lesson,
            student=self.student,
            value='10',
            is_synced_to_nz=False,
            nz_last_synced_value='9'
        )

        service = NZService()
        preview = service.get_unsynced_changes(self.journal)

        self.assertEqual(preview['journal_id'], self.journal.id)
        self.assertEqual(preview['total_count'], 1)
        self.assertEqual(preview['grades_count'], 1)
        self.assertTrue(preview['changes'][0]['can_sync'])
        self.assertEqual(preview['changes'][0]['old_value'], '9')
        self.assertEqual(preview['changes'][0]['new_value'], '10')

    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_verify_session_success(self, mock_get):
        """Перевірка валідності сесії на nz.ua."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://nz.ua/journal/list"
        mock_resp.text = '<html><head><meta name="csrf-token" content="mock_csrf_abc"></head><body><div class="user-name">Оксана Коваленко</div><a href="/logout">Вихід</a></body></html>'
        mock_get.return_value = mock_resp

        service = NZService(phpsessid="test_sess")
        valid, user_info = service.verify_session()

        self.assertTrue(valid)
        self.assertEqual(user_info, "Оксана Коваленко")
        self.assertEqual(service.csrf_token, "mock_csrf_abc")

    @patch('apps.journals.services.nz_service.requests.Session.post')
    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_push_journal_changes_dry_run(self, mock_get, mock_post):
        """Тестовий прогін (dry-run) створює звіт і не відправляє мережевих запитів на nz.ua."""
        grade = Grade.objects.create(
            lesson=self.lesson,
            student=self.student,
            value='12',
            is_synced_to_nz=False
        )

        service = NZService(phpsessid="test_sess")
        log = service.push_journal_changes(self.journal, user=self.teacher, dry_run=True)

        self.assertEqual(log.status, NZSyncLog.Status.SUCCESS)
        self.assertEqual(log.items_count, 1)
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.error_count, 0)
        self.assertTrue(log.details['dry_run'])

        # Переконуємось, що запитів POST на nz.ua не було
        mock_post.assert_not_called()

        # Статус у БД локально залишається незмінним
        grade.refresh_from_db()
        self.assertFalse(grade.is_synced_to_nz)

    @patch('apps.journals.services.nz_service.time.sleep')
    @patch('apps.journals.services.nz_service.requests.Session.post')
    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_push_journal_changes_live(self, mock_get, mock_post, mock_sleep):
        """Реальне вивантаження відправляє оцінки/відвідування/теми з затримкою та оновлює статус."""
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = '<html><meta name="csrf-token" content="csrf_live_123"></html>'
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.text = '{"success": true}'
        mock_post.return_value = mock_post_resp

        grade = Grade.objects.create(
            lesson=self.lesson,
            student=self.student,
            value='10',
            is_synced_to_nz=False
        )

        service = NZService(phpsessid="test_sess")
        log = service.push_journal_changes(self.journal, user=self.teacher, dry_run=False)

        self.assertEqual(log.status, NZSyncLog.Status.SUCCESS)
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.error_count, 0)

        # Переконуємось, що була затримка rate-limiting
        mock_sleep.assert_called_with(0.4)

        # Переконуємось, що POST на /journal/set-mark був викликаний із коректними параметрами
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://nz.ua/journal/set-mark")
        # Для оцінки 10: 10 + 5 = 15
        self.assertEqual(kwargs['data']['mark'], 15)
        self.assertEqual(kwargs['data']['schedule'], 'sch_501')
        self.assertEqual(kwargs['data']['student'], 'st_101')

        # Запис локально тепер позначений як синхронізований
        grade.refresh_from_db()
        self.assertTrue(grade.is_synced_to_nz)
        self.assertEqual(grade.nz_last_synced_value, '10')

    def test_view_nz_sync_preview(self):
        """HTMX/JSON ендпоінт прев'ю повертає деталі змін."""
        Grade.objects.create(
            lesson=self.lesson,
            student=self.student,
            value='8',
            is_synced_to_nz=False
        )
        url = reverse('portal:nz_sync_preview', kwargs={'journal_id': self.journal.id})
        response = self.client.get(url, {'format': 'json'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_count'], 1)
        self.assertEqual(data['journal_id'], self.journal.id)

    @patch('apps.journals.services.nz_service.requests.Session.post')
    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_view_nz_sync_execute_dry_run(self, mock_get, mock_post):
        """Ендпоінт виконання синхронізації через POST повертає JSON зі звітом."""
        Grade.objects.create(
            lesson=self.lesson,
            student=self.student,
            value='7',
            is_synced_to_nz=False
        )
        url = reverse('portal:nz_sync_execute', kwargs={'journal_id': self.journal.id})
        token = self.client.get(reverse('portal:nz_sync_preview', kwargs={'journal_id': self.journal.id}), {'format': 'json'}).json()['preview_token']
        response = self.client.post(url, {'dry_run': '1', 'preview_token': token})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(data['dry_run'])
        self.assertEqual(data['success_count'], 1)

    @patch('apps.journals.services.nz_service.NZService.import_journal_to_local')
    def test_auto_create_journal_on_import(self, mock_import):
        """Імпорт журналу без local_journal_id автоматично створює клас, предмет і журнал для вчителя."""
        mock_import.return_value = {
            'students_matched': 1,
            'students_created': 0,
            'lessons_synced': 2,
            'grades_synced': 3,
            'attendances_synced': 1,
        }
        url = reverse('portal:nz_journal_import')
        response = self.client.post(url, {
            'nz_journal_id': '887766',
            'nz_subgroup_id': '1',
            'class_name': '8-Б',
            'subject_name': 'Геометрія',
        })
        self.assertEqual(response.status_code, 302)

        # Перевіряємо автоматично створений журнал
        created_j = Journal.objects.filter(
            school=self.school,
            teacher=self.teacher,
            student_class__name='8-Б',
            subject__name='Геометрія',
            nz_journal_id='887766'
        ).first()
        self.assertIsNotNone(created_j)
        self.assertEqual(created_j.student_class.name, '8-Б')
        self.assertEqual(created_j.subject.name, 'Геометрія')
        self.assertEqual(created_j.nz_subgroup_id, '1')

    def test_nz_quick_login_view(self):
        """An account ID alone must not authorize access to confidential journals."""
        self.client.logout()
        url = reverse('nz_quick_login')
        response = self.client.post(url, {'account_id': self.nz_account.id})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login'))
        self.assertNotIn('_auth_user_id', self.client.session)

    @patch('apps.journals.services.nz_service.NZService.verify_session')
    def test_nz_login_view_cookie_mode(self, mock_verify):
        """Вхід через nz.ua за кукою PHPSESSID створює/знаходить акаунт та авторизує в системі."""
        self.client.logout()
        mock_verify.return_value = (True, "Коваленко Олена Миколаївна")

        url = reverse('nz_login')
        response = self.client.post(url, {
            'auth_method': 'cookie',
            'phpsessid': 'new_valid_session_cookie',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('portal:nz_journals_list'))
