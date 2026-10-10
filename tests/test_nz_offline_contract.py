"""Offline integration and privacy tests, exclusively with invented data."""
import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core import signing
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.nz_trust import COOKIE
from apps.journals.models import Lesson, Grade, Attendance, NZAccount, LessonPlanTopic
from apps.journals.services.nz_service import NZService, NZServiceError, NZAuthError
from tests import test_journal_workflows as fixtures


class NZOfflineContractTests(TestCase):
    setUp = fixtures.JournalWorkflowsTests.setUp
    url = fixtures.JournalWorkflowsTests.url
    lesson_post = fixtures.JournalWorkflowsTests.lesson_post
    assessment_post = fixtures.JournalWorkflowsTests.assessment_post

    def account(self):
        return NZAccount.objects.create(user=self.teacher, login='synthetic-nz-login', phpsessid='synthetic-session', is_active=True)

    def token(self):
        return self.client.get(self.url('nz_sync_preview'), {'format': 'json'}).json()['preview_token']

    def test_local_work_and_preview_never_contact_nz(self):
        self.account()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Local editing must not make network requests')) as network:
            self.assertEqual(self.client.get(self.url('journal_detail')).status_code, 200)
            self.assertEqual(self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': self.student.pk, 'value': '9'}).status_code, 200)
            self.assertEqual(self.client.post(self.url('lesson_edit', lesson_id=self.l1.pk), self.lesson_post(date='2026-09-05', homework='Вигадане ДЗ', homework_target=f'lesson:{self.l2.pk}')).status_code, 302)
            self.assertEqual(self.client.post(self.url('lesson_create'), self.lesson_post(date='2026-10-10')).status_code, 302)
            self.assertEqual(self.client.post(self.url('lesson_swap'), {'first_id': self.l1.pk, 'second_id': self.l2.pk}).status_code, 302)
            plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Вигадана майбутня тема')
            self.assertEqual(self.client.post(self.url('plan_topic_move', topic_id=plan.pk), {'planned_date': '2026-11-02'}).status_code, 302)
            response = self.client.post(self.url('assessment_calculate'), self.assessment_post())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.post(self.url('assessment_calculate'), self.assessment_post(action='save', preview_token=response.context['preview_token'])).status_code, 302)
            self.assertEqual(self.client.get(self.url('nz_sync_preview')).status_code, 200)
            self.assertEqual(self.client.post(self.url('nz_sync_execute'), {'dry_run': '1', 'preview_token': self.token()}).status_code, 200)
            network.assert_not_called()

    def test_saved_journal_schema_parses_due_date_separately(self):
        service = NZService()
        html = (Path(__file__).parent / 'fixtures/nz_journal_synthetic.html').read_text()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/index', text=html)
        with patch.object(service.session, 'get', return_value=response), patch.object(service.session, 'post') as post:
            data = service.fetch_journal_data('7001')
            first, second = data['lessons']
            self.assertEqual(first['homework'], 'Вигадане ДЗ')
            self.assertEqual(first['homework_due_date'], dt.date(2026, 9, 14))
            self.assertEqual(first['topic'], 'Вигадана тема A Продовження')
            self.assertEqual(second['homework'], '')
            self.assertEqual(second['homework_due_date'], dt.date(2026, 9, 21))
            service.import_journal_to_local('7001', self.journal, user=self.teacher)
            lesson = self.journal.lessons.get(nz_schedule_id='1002')
            self.assertEqual(lesson.homework_due_date, dt.date(2026, 9, 21))
            self.assertEqual(lesson.grades.get().value, 'П/П')
            self.assertFalse(lesson.attendances.exists())
            post.assert_not_called()

    def test_imported_due_date_survives_local_edit(self):
        self.l1.homework_due_date = dt.date(2026, 9, 14); self.l1.save()
        self.client.post(self.url('lesson_edit', lesson_id=self.l1.pk), self.lesson_post(date='2026-09-05', homework_target='date:2026-09-14'))
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.homework_due_date, dt.date(2026, 9, 14))
        self.assertIn('14.09.2026', self.l1.get_homework_target_label())

    def test_migration_normalizes_only_unchanged_imported_homework(self):
        import importlib
        from django.apps import apps
        migration = importlib.import_module('apps.journals.migrations.0005_lesson_homework_due_date')
        self.l1.homework = self.l1.nz_last_synced_homework = 'на 14 вересня Вигадане ДЗ'
        self.l1.is_synced_to_nz = False; self.l1.save()
        self.l2.homework = 'на 19 жовтня Свідомо змінений текст'
        self.l2.nz_last_synced_homework = 'на 19 жовтня Початковий текст'; self.l2.save()
        migration.normalize_imported_homework(apps, None)
        self.l1.refresh_from_db(); self.l2.refresh_from_db()
        self.assertEqual(self.l1.homework, 'Вигадане ДЗ')
        self.assertEqual(self.l1.homework_due_date, dt.date(2026, 9, 14))
        self.assertFalse(self.l1.is_synced_to_nz)
        self.assertEqual(self.l2.homework, 'на 19 жовтня Свідомо змінений текст')
        self.assertIsNone(self.l2.homework_due_date)

    def test_actual_mark_response_confirms_only_exact_value(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"markId":14,"markValue":"9","comment":""}')
        with patch.object(service.session, 'post', return_value=response) as post, patch('apps.journals.services.nz_service.time.sleep'):
            service.push_journal_changes(self.journal)
        grade.refresh_from_db()
        self.assertTrue(grade.is_synced_to_nz)
        payload = post.call_args.kwargs['data']
        self.assertEqual(payload, {'_csrf': 'synthetic-csrf', 'schedule': 'synthetic-a', 'student': 'synthetic-student', 'mark': 14, 'lessonDate': '', 'comment': ''})

    def test_mismatched_partial_or_error_mark_ack_is_rejected(self):
        for data in [{'markId': 13, 'markValue': '8'}, {'markId': 14}, {'markValue': '9'},
                     {'markId': 14, 'markValue': '9', 'message': 'Вигадана помилка'}, {'success': False}, {'error': 'failed'}]:
            with self.subTest(data=data):
                response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text=json.dumps(data))
                self.assertFalse(NZService._response_saved(response, expected_mark=14, expected_value='9'))

    def test_clear_and_attendance_ack(self):
        for data, mark, value in [({'markId': None, 'markValue': ''}, 0, ''), ({'markId': 1, 'markValue': 'Н'}, 1, 'Н'), ({'markId': 33, 'markValue': 'Н/З'}, 33, 'НЕЗАР')]:
            response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text=json.dumps(data))
            self.assertTrue(NZService._response_saved(response, expected_mark=mark, expected_value=value))

    def test_lateness_is_never_sent_as_mark_30(self):
        Attendance.objects.create(lesson=self.l1, student=self.student, status='LATE')
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        with patch.object(service.session, 'post') as post:
            service.push_journal_changes(self.journal)
        post.assert_not_called()
        preview = service.get_unsynced_changes(self.journal)
        self.assertFalse(next(c for c in preview['changes'] if c['type'] == 'attendance')['can_sync'])
        self.assertEqual(service.MARK_MAP_TO_NZ['НЕЗАР'], 33)
        self.assertEqual(service.MARK_MAP_FROM_NZ[30], ('grade', 'П/П'))

    def test_homework_uses_date_option_from_actual_form(self):
        self.l1.homework_due_lesson = self.l2
        self.l1.homework = 'Вигадане ДЗ'
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task', text='''<form>
        <input name="_csrf" value="synthetic-fresh-csrf">
        <textarea name="OsvitaScheduleReal[lesson_topic]">Стара вигадана тема</textarea>
        <textarea name="OsvitaScheduleReal[hometask]">Старе вигадане ДЗ</textarea>
        <input name="Observed[unchanged]" value="synthetic-keep">
        <select name="Observed[hometask_date]"><option selected value="old">Старий урок</option><option value="2026-10-09">9 жовтня</option></select>
        </form>''')
        with patch.object(service.session, 'get', return_value=response):
            payload = service._lesson_form_payload(self.l1, response.url)
        self.assertEqual(payload['Observed[hometask_date]'], '2026-10-09')
        self.assertEqual(payload['Observed[unchanged]'], 'synthetic-keep')
        self.assertEqual(payload['_csrf'], 'synthetic-fresh-csrf')
        self.assertEqual(payload['OsvitaScheduleReal[hometask]'], 'Вигадане ДЗ')

    def test_missing_or_ambiguous_homework_form_does_not_post(self):
        self.l1.is_synced_to_nz = False; self.l1.homework_due_lesson = self.l2; self.l1.save()
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task', text='<form><input name="unknown"></form>')
        with patch.object(service.session, 'get', return_value=response), patch.object(service.session, 'post') as post, patch('apps.journals.services.nz_service.time.sleep'):
            log = service.push_journal_changes(self.journal)
        self.assertGreater(log.error_count, 0)
        post.assert_not_called()
        self.l1.refresh_from_db(); self.assertFalse(self.l1.is_synced_to_nz)

    def test_ambiguous_date_options_are_rejected(self):
        self.l1.homework_due_lesson = self.l2
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task', text='<select name="Observed[hometask_date]"><option value="a">9 жовтня</option><option value="b">9 жовтня</option></select>')
        service = NZService()
        with patch.object(service.session, 'get', return_value=response), self.assertRaises(NZServiceError):
            service._homework_target_fields(self.l1, response.url)

    def test_nz_homework_date_option_includes_current_day(self):
        self.l1.homework_due_date = self.l1.date
        self.l1.homework = 'Вигадане ДЗ на поточний день'
        Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=2, nz_schedule_id='synthetic-same-day')
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task',
            text='<select name="Observed[hometask_date]"><option value="2026-09-05">5 вересня</option><option value="2026-10-09">9 жовтня</option></select>')
        service = NZService()
        with patch.object(service.session, 'get', return_value=response), patch.object(service.session, 'post') as post:
            self.assertEqual(service._homework_target_fields(self.l1, response.url), {'Observed[hometask_date]': '2026-09-05'})
            post.assert_not_called()

    def test_upload_requires_unchanged_preview_and_post(self):
        self.account()
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        with patch('requests.sessions.Session.request') as network:
            self.assertEqual(self.client.get(self.url('nz_sync_execute')).status_code, 405)
            self.assertEqual(self.client.post(self.url('nz_sync_execute'), {}).status_code, 409)
            token = self.token()
            grade.value = '10'; grade.save()
            self.assertEqual(self.client.post(self.url('nz_sync_execute'), {'preview_token': token}).status_code, 409)
            network.assert_not_called()

    def test_invalid_session_blocks_all_outbound_writes(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/login', text='LoginForm')
        with patch.object(service.session, 'get', return_value=response), patch.object(service.session, 'post') as post, self.assertRaises(NZAuthError):
            service.push_journal_changes(self.journal)
        post.assert_not_called()

    def test_anonymous_login_page_never_lists_saved_accounts(self):
        self.account(); self.client.logout()
        response = self.client.get(reverse('login'))
        self.assertEqual(response.context['saved_nz_accounts'], [])
        self.assertNotContains(response, 'synthetic-nz-login')

    def test_explicit_upload_uses_reviewed_grade(self):
        self.account()
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"markId":14,"markValue":"9"}')
        token = self.token()
        with patch.object(NZService, 'from_account', return_value=service), patch.object(service.session, 'post', return_value=response) as post, patch('apps.journals.services.nz_service.time.sleep'):
            result = self.client.post(self.url('nz_sync_execute'), {'preview_token': token})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['success_count'], 1)
        self.assertEqual(post.call_count, 1)
        grade.refresh_from_db(); self.assertTrue(grade.is_synced_to_nz)

    def test_changes_added_during_upload_wait_for_next_upload(self):
        self.l1.is_synced_to_nz = False; self.l1.save()
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task', text='{"success":true}')
        def form_payload(*args):
            Grade.objects.create(lesson=self.l2, student=self.student, value='10')
            return {'_csrf': 'synthetic-csrf'}
        with patch.object(service, '_lesson_form_payload', side_effect=form_payload), patch.object(service.session, 'post', return_value=response) as post, patch('apps.journals.services.nz_service.time.sleep'):
            service.push_journal_changes(self.journal)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(Grade.objects.get(lesson=self.l2, student=self.student).is_synced_to_nz)

    def test_changed_next_homework_lesson_invalidates_preview(self):
        self.l1.homework_for_next_lesson = True; self.l1.is_synced_to_nz = False; self.l1.save()
        service = NZService()
        old = service.pending_changes_fingerprint(self.journal)
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 12), lesson_number=1, nz_schedule_id='synthetic-new-online', is_synced_to_nz=True)
        self.assertNotEqual(old, service.pending_changes_fingerprint(self.journal))

    def test_verified_device_shortcut_and_logout_revocation(self):
        account = self.account(); self.client.logout()
        with patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            response = self.client.post(reverse('nz_login'), {'auth_method': 'cookie', 'phpsessid': 'synthetic-session', 'remember_device': '1'})
        self.assertIn(COOKIE, response.cookies)
        self.assertTrue(response.cookies[COOKIE]['httponly'])
        self.assertNotIn('synthetic-session', response.cookies[COOKIE].value)
        self.client.logout()
        self.client.cookies[COOKIE] = response.cookies[COOKIE].value
        self.assertEqual(list(self.client.get(reverse('login')).context['saved_nz_accounts']), [account])
        self.assertEqual(self.client.post(reverse('nz_quick_login'), {'account_id': account.pk}).url, reverse('portal:teacher_dashboard'))
        self.client.get(reverse('logout'))
        self.assertEqual(self.client.post(reverse('nz_quick_login'), {'account_id': account.pk}).url, reverse('login'))

    def test_device_token_cannot_select_another_account(self):
        account = self.account(); self.client.logout()
        with patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            response = self.client.post(reverse('nz_login'), {'auth_method': 'cookie', 'phpsessid': 'synthetic-session', 'remember_device': '1'})
        self.client.logout()
        self.client.cookies[COOKIE] = response.cookies[COOKIE].value
        self.assertEqual(self.client.post(reverse('nz_quick_login'), {'account_id': account.pk + 100}).url, reverse('login'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_name_or_username_collision_does_not_grant_local_identity(self):
        self.teacher.first_name = 'Вчитель'; self.teacher.last_name = 'Вигаданий'; self.teacher.save()
        self.client.logout()
        with patch.object(NZService, 'login', return_value=(True, 'ok')), patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            self.client.post(reverse('nz_login'), {'auth_method': 'credentials', 'login': self.teacher.username, 'password': 'synthetic-password'})
        new_user = User.objects.get(pk=self.client.session['_auth_user_id'])
        self.assertNotEqual(new_user.pk, self.teacher.pk)
        self.assertNotEqual(new_user.school_id, self.teacher.school_id)
        self.assertEqual(self.client.get(self.url('journal_detail')).status_code, 403)

    def test_failed_session_verification_does_not_authenticate(self):
        self.account(); self.client.logout()
        with patch.object(NZService, 'login', return_value=(True, 'ok')), patch.object(NZService, 'verify_session', return_value=(False, 'expired')):
            response = self.client.post(reverse('nz_login'), {'auth_method': 'credentials', 'login': 'synthetic-nz-login', 'password': 'synthetic-password'})
        self.assertEqual(response.url, reverse('login'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_session_requires_authenticated_nz_markup(self):
        service = NZService()
        for html, expected in [('<html>Вигадана сторінка технічних робіт</html>', False),
                               ('<form>LoginForm</form>', False),
                               ('<a href="/logout">Вихід</a>', True)]:
            response = MagicMock(status_code=200, url='https://nz.ua/journal/list', text=html)
            with self.subTest(expected=expected), patch.object(service.session, 'get', return_value=response):
                self.assertEqual(service.verify_session()[0], expected)

    def test_changed_journal_markup_does_not_report_successful_pull(self):
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/index', text='<html>Вигадана невідома розмітка</html>')
        with patch.object(service.session, 'get', return_value=response), self.assertRaises(NZServiceError):
            service.import_journal_to_local('synthetic-journal', self.journal)
        self.journal.refresh_from_db()
        self.assertIsNone(self.journal.nz_last_sync_at)
        self.assertEqual(self.journal.lessons.count(), 3)

    def test_login_page_received_by_read_is_rejected(self):
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/index', text='<form>LoginForm</form>')
        with patch.object(service.session, 'get', return_value=response):
            for read in [lambda: service.fetch_journal_data('synthetic-journal'), service.fetch_journal_list]:
                with self.assertRaises(NZAuthError):
                    read()

    def test_password_is_not_trimmed_or_stored(self):
        account = self.account(); self.client.logout()
        with patch.object(NZService, 'login', return_value=(True, 'ok')) as login, patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            self.client.post(reverse('nz_login'), {'auth_method': 'credentials', 'login': account.login, 'password': ' synthetic-password '})
        login.assert_called_once_with(account.login, ' synthetic-password ')
        account.refresh_from_db()
        self.assertNotIn('synthetic-password', str(account.cookies))
