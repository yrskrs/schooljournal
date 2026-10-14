import json
from datetime import date, timedelta
from django.test import TestCase, Client
from django.urls import reverse

from apps.accounts.models import User, TeacherProfile
from apps.curriculum.models import AcademicYear, StudentClass, Subject, Student, ClassEnrollment, TeacherAssignment
from apps.journals.models import Journal, Lesson, Grade, Attendance, ExtraStudent, APIInboxResult, GradeAPIConnection
from apps.journals.services.nz_service import NZService


class NewJournalFeaturesTests(TestCase):
    def setUp(self):
        from apps.schools.models import School
        self.school = School.objects.create(name='Тестова гімназія')
        self.year = AcademicYear.objects.create(
            school=self.school,
            name='2026/2027',
            start_date=date(2026, 9, 1),
            end_date=date(2027, 6, 30),
            is_current=True
        )
        self.student_class = StudentClass.objects.create(
            school=self.school, academic_year=self.year, name='9-А', grade_level=9, letter='А'
        )
        self.subject = Subject.objects.create(school=self.school, name='Інформатика')
        self.teacher = User.objects.create_user(
            username='teacher1', password='password123', role=User.Role.TEACHER, school=self.school,
            last_name='Коваленко', first_name='Іван'
        )
        self.journal = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=self.student_class,
            subject=self.subject, teacher=self.teacher, nz_journal_id='1001'
        )
        self.student1 = Student.objects.create(
            school=self.school, last_name='Шевченко', first_name='Тарас', nz_student_id='2001'
        )
        self.student2 = Student.objects.create(
            school=self.school, last_name='Франко', first_name='Іван', nz_student_id='2002'
        )
        ClassEnrollment.objects.create(student=self.student1, student_class=self.student_class, is_active=True)
        ClassEnrollment.objects.create(student=self.student2, student_class=self.student_class, is_active=True)

        self.lesson = Lesson.objects.create(
            journal=self.journal, date=date(2026, 9, 15), lesson_number=1, topic='Бази даних',
            nz_schedule_id='3001', is_synced_to_nz=True
        )

        self.client = Client()
        self.client.login(username='teacher1', password='password123')

    def test_ignore_and_unignore_api_inbox_result_by_date(self):
        """1. Тест кнопки 'Ігнорувати запис/оцінку саме за цю дату' та запис в ігноровані."""
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, name='Тести', source='schooltest', url='https://example.com/api', api_key='k123',
            source_class_id='c1', source_subject_id='s1'
        )
        extra = ExtraStudent.objects.create(connection=conn, external_key='u99', name='Позакласний Петро')
        result = APIInboxResult.objects.create(
            connection=conn, external_id='res-date-1', extra_student=extra,
            payload={'date': '2026-09-15', 'value': '10', 'title': 'Тест №1', 'student_name': 'Позакласний Петро'}
        )

        url = reverse('portal:api_inbox', args=[self.journal.pk])
        # Перевіряємо що результат показується у черзі рішень
        resp = self.client.get(url)
        self.assertContains(resp, 'Позакласний Петро')
        self.assertContains(resp, 'Ігнорувати за 2026-09-15')

        # Ігноруємо цей запис за дату
        resp = self.client.post(url, {'action': 'ignore_result', 'result_id': result.pk})
        self.assertEqual(resp.status_code, 302)

        result.refresh_from_db()
        self.assertTrue(result.is_ignored)
        self.assertTrue(result.resolved)
        self.assertIsNotNone(result.ignored_at)

        # Перевіряємо що запис перейшов у список ігнорованих і не показується в активній черзі
        resp = self.client.get(url)
        self.assertContains(resp, 'Ігноровані записи та оцінки (1)')
        self.assertContains(resp, 'Відновити')
        self.assertEqual(list(resp.context['extra_page']), [])
        self.assertEqual(list(resp.context['result_page']), [])
        self.assertNotContains(resp, 'value="match"')

        # Відновлюємо запис з ігнорованих
        resp = self.client.post(url, {'action': 'unignore_result', 'result_id': result.pk})
        self.assertEqual(resp.status_code, 302)

        result.refresh_from_db()
        self.assertFalse(result.is_ignored)
        self.assertFalse(result.resolved)
        self.assertIsNone(result.ignored_at)

    def test_keep_extracurricular_creates_journal_and_records_grade(self):
        """При виборі 'Залишити позакласним' створюється окремий журнал, підтягуються теми й записуються оцінки."""
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, name='Завдання', source='schoolwork', url='https://example.com/api', api_key='k456',
            source_class_id='c1', source_subject_id='s1'
        )
        extra = ExtraStudent.objects.create(connection=conn, external_key='u77', name='Остапчук Дмитро')
        APIInboxResult.objects.create(
            connection=conn, external_id='res-pk-1', extra_student=extra,
            payload={'date': '2026-09-20', 'value': '11', 'title': 'Практична робота'}
        )

        url = reverse('portal:api_inbox', args=[self.journal.pk])
        resp = self.client.post(url, {
            'action': 'match',
            'extra_id': extra.pk,
            'version': extra.updated_at.isoformat(),
            'student': ''
        })
        self.assertEqual(resp.status_code, 302)

        extra.refresh_from_db()
        self.assertTrue(extra.is_separate)
        self.assertIsNotNone(extra.separate_student_id)
        self.assertIsNotNone(extra.separate_journal_id)
        self.assertEqual(extra.separate_journal.student_class.letter, 'ПК')
        self.assertEqual(extra.separate_journal.nz_journal_id, '')

        # Перевіряємо що оцінку записано в окремий журнал
        grade = Grade.objects.filter(student=extra.separate_student).first()
        self.assertIsNotNone(grade)
        self.assertEqual(grade.value, '11')
        self.assertEqual(grade.lesson.journal_id, extra.separate_journal_id)
        self.assertEqual(grade.lesson.date.isoformat(), '2026-09-20')

        result = APIInboxResult.objects.get(extra_student=extra)
        self.assertTrue(result.resolved)
        self.assertEqual(result.payload['value'], '11')
        self.assertContains(self.client.get(url), 'Залишено позакласним')

    def test_ignore_nz_change_excludes_it_from_delta(self):
        """3. Тест: кнопка 'Ігнорувати запис' у синхронізації з nz.ua виключає запис із дельти."""
        grade = Grade.objects.create(
            lesson=self.lesson, student=self.student1, value='10', is_synced_to_nz=False
        )
        service = NZService()
        unsynced = service.get_unsynced_changes(self.journal)
        self.assertEqual(unsynced['total_count'], 1)
        self.assertEqual(unsynced['changes'][0]['object_id'], grade.id)

        # Викликаємо endpoint ігнорування запису
        ignore_url = reverse('portal:nz_ignore_change', args=[self.journal.pk])
        resp = self.client.post(ignore_url, {
            'item_type': 'grade',
            'object_id': grade.id
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')

        grade.refresh_from_db()
        self.assertTrue(grade.is_ignored_for_nz)
        self.assertTrue(grade.is_synced_to_nz)

        # Перевіряємо що запис більше не з'являється у дельті для вивантаження
        unsynced_after = service.get_unsynced_changes(self.journal)
        self.assertEqual(unsynced_after['total_count'], 0)

    def test_reorder_class_blocks_in_teacher_dashboard(self):
        """4. Тест: зміна послідовності блоків класів у 'Мої навчальні журнали'."""
        class_8 = StudentClass.objects.create(
            school=self.school, academic_year=self.year, name='8-Б', grade_level=8, letter='Б'
        )
        journal_8 = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=class_8,
            subject=self.subject, teacher=self.teacher
        )

        TeacherAssignment.objects.create(
            school=self.school, teacher=self.teacher, student_class=self.student_class,
            subject=self.subject, academic_year=self.year
        )
        TeacherAssignment.objects.create(
            school=self.school, teacher=self.teacher, student_class=class_8,
            subject=self.subject, academic_year=self.year
        )

        dash_url = reverse('portal:teacher_dashboard')
        resp = self.client.get(dash_url)
        self.assertEqual(resp.status_code, 200)
        # Спочатку стандартне сортування: 8-Б перед 9-А
        blocks = resp.context['class_blocks']
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]['student_class'].name, '8-Б')
        self.assertEqual(blocks[1]['student_class'].name, '9-А')

        # Вчитель міняє порядок блоків класів: ставимо 9-А першим
        order_url = reverse('portal:update_class_order')
        resp_post = self.client.post(order_url, json.dumps({
            'class_ids': [self.student_class.id, class_8.id]
        }), content_type='application/json')
        self.assertEqual(resp_post.status_code, 200)

        # Перевіряємо збереження в TeacherProfile
        profile = TeacherProfile.objects.get(user=self.teacher)
        self.assertEqual(profile.class_order, [self.student_class.id, class_8.id])

        # Перевіряємо що дашборд повертає блоки класів у новому порядку
        resp_after = self.client.get(dash_url)
        blocks_after = resp_after.context['class_blocks']
        self.assertEqual(blocks_after[0]['student_class'].name, '9-А')
        self.assertEqual(blocks_after[1]['student_class'].name, '8-Б')

    def test_drag_and_drop_journal_tiles_reordering(self):
        """Тест: перенесення (drag & drop) журналів-плиток на дашборді."""
        class_8 = StudentClass.objects.create(
            school=self.school, academic_year=self.year, name='8-Б', grade_level=8, letter='Б'
        )
        journal_8 = Journal.objects.create(
            school=self.school, academic_year=self.year, student_class=class_8,
            subject=self.subject, teacher=self.teacher
        )
        TeacherAssignment.objects.create(
            school=self.school, teacher=self.teacher, student_class=self.student_class,
            subject=self.subject, academic_year=self.year
        )
        TeacherAssignment.objects.create(
            school=self.school, teacher=self.teacher, student_class=class_8,
            subject=self.subject, academic_year=self.year
        )

        dash_url = reverse('portal:teacher_dashboard')
        resp = self.client.get(dash_url)
        self.assertEqual(resp.status_code, 200)
        # Перевіряємо розмітку плитками та підтримку drag & drop
        self.assertContains(resp, 'id="journalsGrid"')
        self.assertContains(resp, 'journal-tile-card')
        self.assertContains(resp, 'draggable="true"')

        # Змінюємо порядок перетягуванням (journal_8 першим, self.journal другим)
        reorder_url = reverse('portal:update_journal_order')
        resp_post = self.client.post(reorder_url, json.dumps({
            'journal_ids': [journal_8.id, self.journal.id]
        }), content_type='application/json')
        self.assertEqual(resp_post.status_code, 200)

        profile = TeacherProfile.objects.get(user=self.teacher)
        self.assertEqual(profile.journal_order, [journal_8.id, self.journal.id])

        # Перевіряємо що в контексті дашборду журнали розташовані у новому порядку
        resp_after = self.client.get(dash_url)
        journals_after = resp_after.context['my_journals']
        self.assertEqual(journals_after[0].id, journal_8.id)
        self.assertEqual(journals_after[1].id, self.journal.id)

    def test_student_number_action_menu_in_journal_detail(self):
        """5. Тест: пункт 🚫 Ігнорувати перенесено до номерації учня."""
        detail_url = reverse('portal:journal_detail', args=[self.journal.pk])
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)

        # Номер містить інтерактивну кнопку меню
        self.assertContains(resp, 'student-number-btn')
        self.assertContains(resp, 'toggleStudentActionMenu')
        self.assertContains(resp, 'Ігноровано (сімейна форма)')
        self.assertContains(resp, 'role="menuitemcheckbox" aria-checked="false"')

    def test_extracurricular_journal_workflow(self):
        """Тест повного життєвого циклу позакласного журналу: створення, підтягування тем, дашборд, перегляд та блокування nz.ua."""
        # 1. Створюємо позакласного учня та результат у вхідних
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, name='Тести', source='schooltest', url='https://example.com/api', api_key='k123',
            source_class_id='c1', source_subject_id='s1'
        )
        extra = ExtraStudent.objects.create(connection=conn, external_key='u88', name='Сидоренко Василь')
        APIInboxResult.objects.create(
            connection=conn, external_id='res-extra-1', extra_student=extra,
            payload={'date': '2026-09-15', 'value': '12', 'title': 'Тест з баз даних'}
        )

        inbox_url = reverse('portal:api_inbox', args=[self.journal.pk])
        # Вчитель натискає "Залишити позакласним"
        resp = self.client.post(inbox_url, {
            'action': 'keep_separate',
            'extra_id': extra.pk,
            'version': extra.updated_at.isoformat(),
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        extra.refresh_from_db()
        self.assertIsNotNone(extra.separate_journal_id)
        extra_journal = extra.separate_journal

        # 2. Перевіряємо що тема уроку підтягнулася з основного журналу (2026-09-15 -> 'Бази даних')
        extra_lesson = extra_journal.lessons.filter(date=date(2026, 9, 15)).first()
        self.assertIsNotNone(extra_lesson)
        self.assertEqual(extra_lesson.topic, 'Бази даних')

        # Оцінку записано
        grade = Grade.objects.filter(lesson=extra_lesson, student=extra.separate_student).first()
        self.assertIsNotNone(grade)
        self.assertEqual(grade.value, '12')

        # 3. Додаємо новий урок до основного журналу та перевіряємо кнопку "Підтягнути теми"
        new_main_lesson = Lesson.objects.create(
            journal=self.journal, date=date(2026, 9, 22), lesson_number=1, topic='Алгоритми пошуку'
        )
        sync_url = reverse('portal:sync_extra_topics', args=[extra_journal.pk])
        sync_resp = self.client.post(sync_url, follow=True)
        self.assertEqual(sync_resp.status_code, 200)
        self.assertTrue(extra_journal.lessons.filter(date=date(2026, 9, 22), topic='Алгоритми пошуку').exists())

        # 4. Перевіряємо що позакласний журнал показується у дашборді вчителя у блоці extra_journals
        dash_resp = self.client.get(reverse('portal:teacher_dashboard'))
        self.assertIn(extra_journal, dash_resp.context['extra_journals'])
        self.assertContains(dash_resp, 'Журнали позакласних учнів')
        self.assertContains(dash_resp, extra_journal.student_class.name)

        # 5. Перевіряємо що екран journal_detail для позакласного журналу відкривається успішно (200), не редіректить
        detail_resp = self.client.get(reverse('portal:journal_detail', args=[extra_journal.pk]))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, 'Журнал позакласних учнів')
        self.assertContains(detail_resp, 'Сидоренко Василь')
        self.assertContains(detail_resp, 'Бази даних')
        self.assertContains(detail_resp, 'Алгоритми пошуку')
        self.assertContains(detail_resp, '12')
        self.assertContains(detail_resp, 'Локальний (не відправляється на nz.ua)')

        # 6. Перевіряємо що відправлення в nz.ua заблоковано для цього журналу
        nz_resp = self.client.post(reverse('portal:nz_sync_execute', args=[extra_journal.pk]))
        self.assertEqual(nz_resp.status_code, 400)
        self.assertIn('не відправляється на nz.ua', nz_resp.json()['message'])

