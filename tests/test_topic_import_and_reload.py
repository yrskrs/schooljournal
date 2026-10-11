"""Synthetic regressions for lesson identity, round trips and local journal replacement."""
import datetime as dt
import io
from unittest.mock import patch

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.curriculum.models import Subject, StudentClass, ClassEnrollment
from apps.importers.services.export_service import ExportService
from apps.importers.services.topic_service import TopicImportService
from apps.journals.models import (
    Journal, Lesson, LessonPlanTopic, Grade, Attendance, AuditLog, SummaryGrade, NZAccount,
    GradeAPIConnection, GradeStudentMapping, ExternalGrade, ExtraStudent, APIInboxResult,
)
from apps.journals.services.journal_lifecycle import delete_local_journal
from apps.journals.services.nz_service import NZService, NZServiceError
from tests import test_journal_workflows as fixtures


class TopicImportAndReloadTests(TestCase):
    setUp = fixtures.JournalWorkflowsTests.setUp
    url = fixtures.JournalWorkflowsTests.url

    def row(self, **kwargs):
        return dict(topic_number='1', title='Оновлена тема', date='2026-09-05',
                    homework='', notes='', lesson_type='ORDINARY', **kwargs)

    def preview(self, rows, **kwargs):
        return TopicImportService.validate_and_preview(rows, self.journal, **kwargs)

    def apply(self, rows, **kwargs):
        preview = self.preview(rows, target=kwargs.get('target', 'lessons_and_plan'))
        return TopicImportService.execute_import(preview['preview_data'], self.journal, user=self.teacher, **kwargs)

    def other_journal(self):
        subject = Subject.objects.create(school=self.school, name='Інший вигаданий предмет')
        return Journal.objects.create(school=self.school, academic_year=self.year,
            student_class=self.group, subject=subject, teacher=self.teacher)

    def account(self):
        return NZAccount.objects.create(user=self.teacher, phpsessid='synthetic', is_active=True)

    def remote(self):
        return {'students': [{'nz_student_id': self.student.nz_student_id, 'name': 'Вигаданий Учень'}],
                'lessons': [{'nz_schedule_id': 'synthetic-a', 'date': self.l1.date, 'lesson_number': 5,
                             'topic': 'Онлайн-тема', 'homework': 'Онлайн-ДЗ'}],
                'marks': [{'nz_schedule_id': 'synthetic-a', 'date': self.l1.date, 'lesson_number': 5,
                           'nz_student_id': self.student.nz_student_id, 'student_name': 'Вигаданий Учень', 'value': '9'}]}

    def test_old_template_updates_nonfirst_lesson_and_preserves_identity_marks(self):
        self.l1.lesson_number = 5
        self.l1.lesson_time = dt.time(12, 30)
        self.l1.save()
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='10')
        attendance = Attendance.objects.create(lesson=self.l1, student=self.empty_student, status='ABSENT_EXCUSED')
        initial = self.journal.lessons.count()
        result = self.apply([self.row()])
        self.assertEqual(result['updated_lessons'], 1)
        self.assertEqual(result['added_lessons'], 0)
        self.assertEqual(self.journal.lessons.count(), initial)
        self.l1.refresh_from_db()
        self.assertEqual((self.l1.lesson_number, self.l1.lesson_time, self.l1.nz_schedule_id), (5, dt.time(12, 30), 'synthetic-a'))
        self.assertEqual(self.l1.topic, 'Оновлена тема')
        grade.refresh_from_db(); attendance.refresh_from_db()
        self.assertEqual(grade.value, '10')
        self.assertEqual(attendance.status, 'ABSENT_EXCUSED')

    def test_export_roundtrip_updates_two_lessons_same_day_and_keeps_dated_future_plan(self):
        self.l1.lesson_number = 5; self.l1.save()
        second = Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=6, topic='Друга тема')
        future = LessonPlanTopic.objects.create(journal=self.journal, topic_number=20, title='Майбутня тема', planned_date=dt.date(2026, 12, 1))
        for fmt in ('xlsx', 'csv'):
            with self.subTest(fmt=fmt):
                content, _ = ExportService.export_topics(self.journal, file_format=fmt)
                rows = TopicImportService.parse_file(io.BytesIO(content), 'plan.' + fmt)
                self.assertEqual(len(rows), 5)
                selected = next(r for r in rows if r['lesson_id'] == str(second.pk))
                selected['title'] = 'Змінена друга тема'
                preview = self.preview(rows)
                self.assertEqual(preview['error_rows'], 0, preview['errors'])
                result = TopicImportService.execute_import(preview['preview_data'], self.journal)
                self.assertEqual(result['added_lessons'], 0)
                self.assertEqual(self.journal.lessons.count(), 4)
                second.refresh_from_db(); future.refresh_from_db()
                self.assertEqual(second.topic, 'Змінена друга тема')
                self.assertFalse(future.is_conducted)
                self.assertFalse(self.journal.lessons.filter(date=future.planned_date).exists())
                self.apply(rows)
                self.assertEqual(self.journal.lessons.count(), 4)

    def test_ambiguous_date_is_rejected_instead_of_updating_wrong_lesson(self):
        Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=2, topic='Другий')
        preview = self.preview([self.row()])
        self.assertEqual(preview['error_rows'], 1)
        result = TopicImportService.execute_import(preview['preview_data'], self.journal)
        self.assertEqual(result['skipped'], 1)
        self.l1.refresh_from_db(); self.assertEqual(self.l1.topic, 'Тема A')

    def test_daily_number_disambiguates_existing_and_new_lessons(self):
        second = Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=2, topic='Другий')
        self.apply([self.row(day_lesson_number='2')])
        second.refresh_from_db(); self.l1.refresh_from_db()
        self.assertEqual(second.topic, 'Оновлена тема')
        self.assertEqual(self.l1.topic, 'Тема A')
        self.apply([dict(self.row(day_lesson_number='3'), topic_number='2')])
        self.assertEqual(self.journal.lessons.filter(date=self.l1.date).count(), 3)

    def test_blanks_clear_fields_but_noop_keeps_online_confirmation(self):
        self.l1.homework = 'Старе ДЗ'; self.l1.notes = 'Примітка'; self.l1.save()
        self.apply([self.row(lesson_id=str(self.l1.pk))])
        self.l1.refresh_from_db()
        self.assertEqual((self.l1.homework, self.l1.notes), ('', ''))
        self.l1.is_synced_to_nz = True; self.l1.save()
        content, _ = ExportService.export_topics(self.journal)
        rows = TopicImportService.parse_file(io.BytesIO(content), 'plan.xlsx')
        self.apply(rows)
        self.l1.refresh_from_db(); self.assertTrue(self.l1.is_synced_to_nz)

    def test_plan_only_and_plan_record_never_create_lessons(self):
        future = dict(self.row(), date='2026-12-01')
        self.apply([future], target='plan_only')
        self.assertFalse(self.journal.lessons.filter(date=dt.date(2026, 12, 1)).exists())
        plan = self.journal.plan_topics.get()
        self.assertFalse(plan.is_conducted)
        self.apply([dict(future, record_kind='PLAN')])
        self.assertFalse(self.journal.lessons.filter(date=plan.planned_date).exists())

    def test_duplicate_rows_and_outdated_year_are_rejected(self):
        preview = self.preview([self.row(), self.row()])
        self.assertEqual(preview['valid_rows'], 0)
        self.apply([self.row(), self.row()])
        self.assertFalse(self.journal.plan_topics.exists())
        self.assertEqual(self.preview([dict(self.row(), date='2024-09-02')])['error_rows'], 1)

    def test_foreign_ids_and_changed_lesson_date_are_rejected(self):
        other = self.other_journal()
        foreign = Lesson.objects.create(journal=other, date=self.l1.date, topic='Чужий урок')
        for row in [self.row(lesson_id=str(foreign.pk)), self.row(journal_id=str(other.pk)),
                    dict(self.row(lesson_id=str(self.l1.pk)), date='2026-09-06')]:
            self.assertEqual(self.preview([row])['error_rows'], 1)

    def test_modes_skip_whole_existing_row_and_update_only_does_not_create_plan(self):
        self.apply([self.row()], mode='add_only')
        self.assertFalse(self.journal.plan_topics.exists())
        self.l1.refresh_from_db(); self.assertEqual(self.l1.topic, 'Тема A')
        self.apply([self.row()], mode='update_only')
        self.assertFalse(self.journal.plan_topics.exists())
        self.l1.refresh_from_db(); self.assertEqual(self.l1.topic, 'Оновлена тема')
        self.apply([dict(self.row(), date='2026-12-01')], mode='update_only')
        self.assertEqual(self.journal.lessons.count(), 3)

    def test_stale_preview_does_not_create_another_lesson(self):
        preview = self.preview([dict(self.row(), date='2026-12-01')])
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 12, 1), topic='Створений після перегляду')
        with self.assertRaises(ValueError):
            TopicImportService.execute_import(preview['preview_data'], self.journal)
        self.assertFalse(self.journal.plan_topics.exists())

    def test_preview_is_bound_to_journal_and_failed_upload_clears_it(self):
        other = self.other_journal()
        content, _ = ExportService.export_topics(self.journal, file_format='csv')
        self.client.post(self.url('import_topics'), {'action': 'upload_file', 'file': SimpleUploadedFile('plan.csv', content)})
        response = self.client.post(reverse('portal:import_topics', args=[other.pk]), {'action': 'confirm_import'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(other.plan_topics.exists())
        self.client.post(self.url('import_topics'), {'action': 'upload_file'})
        self.assertNotIn('topic_import_preview', self.client.session)

    def test_journal_template_contains_current_lessons_and_generic_template_is_empty(self):
        response = self.client.get(reverse('portal:download_template', args=['topics', 'xlsx']), {'journal_id': self.journal.pk})
        rows = TopicImportService.parse_file(io.BytesIO(response.content), 'template.xlsx')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['lesson_id'], str(self.l1.pk))
        content, _ = TopicImportService.generate_template()
        self.assertEqual(TopicImportService.parse_file(io.BytesIO(content), 'empty.xlsx'), [])

    def test_excel_dates_and_semicolon_csv(self):
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(['№ уроку', 'Тема уроку', 'Дата уроку'])
        ws.append([1, 'Тема', dt.datetime(2026, 9, 5)])
        output = io.BytesIO(); wb.save(output); output.seek(0)
        self.assertEqual(TopicImportService.parse_file(output, 'plan.xlsx')[0]['date'], '2026-09-05')
        rows = TopicImportService.parse_file(io.BytesIO('№ уроку;Тема уроку;Дата уроку\n1;Тема;2026-09-05\n'.encode()), 'plan.csv')
        self.assertTrue(self.preview(rows)['preview_data'][0]['exists_lesson'])

    def test_replace_removes_duplicates_and_local_content_but_keeps_journal_and_connections(self):
        connection = GradeAPIConnection.objects.create(journal=self.journal, name='Тести', url='http://synthetic.invalid')
        mapping = GradeStudentMapping.objects.create(connection=connection, external_id='test-student', student=self.student)
        Grade.objects.create(lesson=self.l1, student=self.student, value='11')
        Attendance.objects.create(lesson=self.l2, student=self.student, status='LATE')
        LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Локальний план')
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='ANNUAL', value='10')
        self.journal.ignored_students.add(self.student)
        with patch.object(NZService, 'fetch_journal_data', return_value=self.remote()), patch('requests.sessions.Session.request') as network:
            stats = NZService().import_journal_to_local('synthetic-journal', self.journal, user=self.teacher, replace=True)
            network.assert_not_called()
        self.assertTrue(stats['replaced'])
        self.assertEqual(self.journal.lessons.count(), 1)
        lesson = self.journal.lessons.get()
        self.assertEqual((lesson.topic, lesson.homework, lesson.lesson_number), ('Онлайн-тема', 'Онлайн-ДЗ', 5))
        self.assertEqual(lesson.grades.get().value, '9')
        self.assertFalse(self.journal.plan_topics.exists())
        self.assertFalse(self.journal.summary_grades.exists())
        self.assertFalse(self.journal.ignored_students.exists())
        self.assertTrue(GradeStudentMapping.objects.filter(pk=mapping.pk).exists())
        self.assertTrue(Journal.objects.filter(pk=self.journal.pk).exists())
        self.assertTrue(AuditLog.objects.filter(target_model='Journal', target_id=str(self.journal.pk), action='IMPORT').exists())

    def test_merge_preserves_local_changes_while_replace_uses_online_data(self):
        self.l1.topic = 'Локальна тема'; self.l1.is_synced_to_nz = False; self.l1.save()
        with patch.object(NZService, 'fetch_journal_data', return_value=self.remote()):
            NZService().import_journal_to_local('synthetic-journal', self.journal)
        self.l1.refresh_from_db(); self.assertEqual(self.l1.topic, 'Локальна тема')
        self.assertEqual(self.journal.lessons.count(), 3)

    def test_failed_or_empty_download_and_failure_after_cleanup_roll_back(self):
        for failure in (NZServiceError('Вигаданий збій'), None):
            kwargs = {'side_effect': failure} if failure else {'return_value': {'students': [], 'lessons': [], 'marks': []}}
            with patch.object(NZService, 'fetch_journal_data', **kwargs), self.assertRaises(NZServiceError):
                NZService().import_journal_to_local('synthetic-journal', self.journal, replace=True)
            self.assertEqual(self.journal.lessons.count(), 3)
        with patch.object(NZService, 'fetch_journal_data', return_value=self.remote()), patch.object(Lesson.objects, 'create', side_effect=RuntimeError('Вигаданий збій запису')), self.assertRaises(RuntimeError):
            NZService().import_journal_to_local('synthetic-journal', self.journal, replace=True)
        self.assertEqual(self.journal.lessons.count(), 3)
        self.assertTrue(Lesson.objects.filter(pk=self.l1.pk).exists())

    def test_replace_requires_confirmation_owner_and_same_remote_journal(self):
        self.account()
        with patch.object(NZService, 'fetch_journal_data') as fetch:
            self.assertEqual(self.client.post(self.url('nz_journal_import_for_journal'), {'import_mode': 'replace'}).status_code, 400)
            self.assertEqual(self.client.post(self.url('nz_journal_import_for_journal'), {'import_mode': 'replace', 'confirm_replace': 'on', 'nz_journal_id': 'different'}).status_code, 400)
            substitute = User.objects.create_user(username='substitute', school=self.school, role=User.Role.TEACHER)
            self.journal.allows_override_teacher = True; self.journal.save()
            self.client.force_login(substitute)
            self.assertEqual(self.client.post(self.url('nz_journal_import_for_journal'), {'import_mode': 'replace', 'confirm_replace': 'on'}).status_code, 403)
            self.assertEqual(self.client.get(self.url('journal_delete')).status_code, 403)
            fetch.assert_not_called()

    def test_replace_endpoint_and_cleanup_pages(self):
        self.account()
        self.assertContains(self.client.get(self.url('journal_detail')), 'Завантажити заново з nz.ua')
        self.assertContains(self.client.get(self.url('nz_journal_reload')), 'confirm_replace')
        self.assertContains(self.client.get(self.url('journal_delete')), 'confirm_delete')
        with patch.object(NZService, 'fetch_journal_data', return_value=self.remote()):
            response = self.client.post(self.url('nz_journal_import_for_journal'), {'import_mode': 'replace', 'confirm_replace': 'on', 'format': 'json'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['stats']['replaced'])

    def test_delete_is_local_and_handles_protected_inbox_and_external_records(self):
        other = self.other_journal()
        other_lesson = Lesson.objects.create(journal=other, date=self.l1.date, topic='Інший урок')
        other_grade = Grade.objects.create(lesson=other_lesson, student=self.student, value='8')
        connection = GradeAPIConnection.objects.create(journal=self.journal, name='Тести', url='http://synthetic.invalid')
        extra = ExtraStudent.objects.create(connection=connection, external_key='extra', name='Вигаданий')
        APIInboxResult.objects.create(connection=connection, extra_student=extra, external_id='result')
        ExternalGrade.objects.create(connection=connection, lesson=self.l1, student=self.student,
            external_id='grade', value='9', fingerprint='synthetic')
        with patch('requests.sessions.Session.request') as network:
            response = self.client.post(self.url('journal_delete'), {'confirm_delete': 'on'})
            network.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Journal.objects.filter(pk=self.journal.pk).exists())
        self.assertTrue(Grade.objects.filter(pk=other_grade.pk).exists())
        self.assertTrue(ClassEnrollment.objects.filter(student=self.student, student_class=self.group).exists())
        self.assertTrue(AuditLog.objects.filter(action='DELETE', target_model='Journal', target_id=str(self.journal.pk)).exists())

    def test_delete_get_and_unconfirmed_post_leave_content_unchanged(self):
        self.client.get(self.url('journal_delete'))
        self.client.post(self.url('journal_delete'), {})
        self.assertEqual(self.journal.lessons.count(), 3)
        self.journal.is_archived = True; self.journal.save()
        self.assertEqual(self.client.post(self.url('journal_delete'), {'confirm_delete': 'on'}).status_code, 403)

    def test_separate_journal_dependencies_prevent_deletion(self):
        parent = self.other_journal()
        connection = GradeAPIConnection.objects.create(journal=parent, name='Тести', url='http://synthetic.invalid')
        ExtraStudent.objects.create(connection=connection, external_key='extra', name='Вигаданий', separate_journal=self.journal)
        with self.assertRaises(ValueError):
            delete_local_journal(self.journal, self.teacher)
        self.assertTrue(Journal.objects.filter(pk=self.journal.pk).exists())
