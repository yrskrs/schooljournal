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
from apps.curriculum.models import LessonTime, WeeklyScheduleEntry
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

    def test_lesson_form_payload_uses_calendar_sequence_number_and_automatic_next_homework(self):
        # Lesson has daily lesson_number=6 (e.g. 6th period of the day), but calendar sequence is 1
        self.l1.lesson_number = 6
        self.l1.homework = 'Домашнє завдання'
        self.l1.save()
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task', text='''<form>
        <input name="_csrf" value="synthetic-csrf">
        <textarea name="OsvitaScheduleReal[lesson_topic]">Стара тема</textarea>
        <textarea name="OsvitaScheduleReal[hometask]">Старе ДЗ</textarea>
        <input name="OsvitaScheduleReal[lesson_number]" value="6">
        <select name="Observed[hometask_date]"><option value="2026-09-05">5 вересня</option><option value="2026-10-09">9 жовтня</option></select>
        </form>''')
        with patch.object(service.session, 'get', return_value=response):
            payload = service._lesson_form_payload(self.l1, response.url)
        # Number is set to calendar plan sequence (1), not daily period (6)
        self.assertEqual(payload['OsvitaScheduleReal[lesson_number]'], '1')
        # Homework due date is automatically set to next lesson (self.l2 on 2026-10-09)
        self.assertEqual(payload['Observed[hometask_date]'], '2026-10-09')

        # When linked to a calendar plan topic, it uses the plan's topic_number
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=5, title='Планова тема')
        self.l1.linked_plan_topic = plan
        self.l1.save()
        with patch.object(service.session, 'get', return_value=response):
            payload_plan = service._lesson_form_payload(self.l1, response.url)
        self.assertEqual(payload_plan['OsvitaScheduleReal[lesson_number]'], '5')

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

    def test_parse_buzzers_from_html_and_sync_lesson_times(self):
        service = NZService()
        sample_html = """
        <select id="osvitaschedulereal-buzzer_id" name="OsvitaScheduleReal[buzzer_id]">
            <option value=""></option>
            <option value="101">1 урок (08:35 - 09:20)</option>
            <option value="102">2 урок (09:30 - 10:15)</option>
            <option value="103">3 урок (10:30 — 11:15)</option>
        </select>
        <table class="point-table">
            <thead>
                <tr>
                    <td class="pt-point">
                        <a href="/journal/add-edit-lesson?journal=7001&schedule=501">1</a>
                        <span class="pt-month">Вер.</span>
                    </td>
                    <td class="pt-point">
                        <a href="/journal/add-edit-lesson?journal=7001&schedule=502">4</a>
                    </td>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td class="pt-theme" data-student-id="9001">Тестовий Учень</td>
                    <td class="pt-point"><input value="10" data-mark-id="1"></td>
                    <td class="pt-point"><input value="11" data-mark-id="2"></td>
                </tr>
            </tbody>
        </table>
        <form action="/journal/add-edit-lesson?journal=7001&schedule=501">
            <select name="OsvitaScheduleReal[buzzer_id]">
                <option value="102" selected>2 урок (09:30 - 10:15)</option>
            </select>
        </form>
        """
        parsed_buzzers = service._parse_buzzers(sample_html)
        self.assertEqual(len(parsed_buzzers), 3)
        self.assertEqual(parsed_buzzers['101']['number'], 1)
        self.assertEqual(parsed_buzzers['101']['start_time'], dt.time(8, 35))
        self.assertEqual(parsed_buzzers['101']['end_time'], dt.time(9, 20))
        self.assertEqual(parsed_buzzers['103']['number'], 3)
        self.assertEqual(parsed_buzzers['103']['start_time'], dt.time(10, 30))

        # Import to local journal
        stats = service.import_journal_to_local('7001', self.journal, html_content=sample_html, user=self.teacher)
        self.assertEqual(stats['lesson_times_synced'], 3)
        self.assertEqual(LessonTime.objects.filter(school=self.school).count(), 3)
        lt1 = LessonTime.objects.get(school=self.school, number=1)
        self.assertEqual(lt1.start_time, dt.time(8, 35))
        self.assertEqual(lt1.end_time, dt.time(9, 20))

        # Lesson 501 had the embedded form with selected buzzer 102 (number 2, 09:30)
        l501 = self.journal.lessons.get(nz_schedule_id='501')
        self.assertEqual(l501.lesson_number, 2)
        self.assertEqual(l501.lesson_time, dt.time(9, 30))

        # Timetable check
        from apps.curriculum.timetable import build_week_schedule
        days, rows = build_week_schedule(self.school, [self.journal], dt.date(2026, 8, 31))
        # The lesson should be in row with 09:30, NOT in "Без часу"
        time_rows = [r for r in rows if r['time'] == dt.time(9, 30)]
        self.assertEqual(len(time_rows), 1)
        t_cell_items = [it for c in time_rows[0]['cells'] for it in c['items']]
        self.assertTrue(any(it['kind'] == 'recorded' for it in t_cell_items))

    def test_weekly_schedule_fallback_and_auto_alignment(self):
        # Configure weekly schedule for Tuesday (weekday 1) -> 3rd lesson
        lt3 = LessonTime.objects.create(school=self.school, number=3, start_time=dt.time(10, 30), end_time=dt.time(11, 15))
        WeeklyScheduleEntry.objects.create(
            journal=self.journal,
            weekday=1,  # Tuesday
            lesson_time=lt3,
            start_date=dt.date(2026, 9, 1),
            end_date=dt.date(2027, 6, 30)
        )
        service = NZService()
        html = """
        <table class="point-table">
            <thead>
                <tr>
                    <td class="pt-point">
                        <a href="/journal/add-edit-lesson?journal=7001&schedule=601">1</a>
                        <span class="pt-month">Вер.</span>
                    </td>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td class="pt-theme" data-student-id="9001">Тестовий Учень</td>
                    <td class="pt-point"><input value="12" data-mark-id="3"></td>
                </tr>
            </tbody>
        </table>
        """
        # 1 Sep 2026 is Tuesday (weekday 1)
        service.import_journal_to_local('7001', self.journal, html_content=html, user=self.teacher)
        l601 = self.journal.lessons.get(nz_schedule_id='601')
        self.assertEqual(l601.date, dt.date(2026, 9, 1))
        self.assertEqual(l601.lesson_number, 3)
        self.assertEqual(l601.lesson_time, dt.time(10, 30))

    def test_real_journal_9g_html_parsing(self):
        real_file = Path('/home/flinux/Документи/allWork/schooljournal/nz.uz/Журнал 9-Г _ Нові знання.html')
        if not real_file.exists():
            return
        service = NZService()
        html = real_file.read_text(encoding='utf-8')
        data = service.fetch_journal_data('14904615', html_content=html)
        self.assertIn('buzzers', data)
        self.assertEqual(len(data['buzzers']), 8)
        self.assertEqual(data['buzzers']['9788288']['number'], 1)
        self.assertEqual(data['buzzers']['9788288']['start_time'], dt.time(8, 35))
        self.assertEqual(data['buzzers']['9788299']['number'], 8)
        self.assertEqual(data['buzzers']['9788299']['start_time'], dt.time(15, 20))
        self.assertEqual(len(data['lessons']), 9)

    def test_online_journal_5_column_layout_homework_and_topic_parsing(self):
        html = """<!doctype html><html><head><meta name="csrf-token" content="token"></head><body>
        <select id="semester"><option selected>I семестр 2026/2027</option></select>
        <table class="point-table" id="journalList">
            <thead>
                <tr>
                    <td>#</td><td class="pt-theme">ПІБ</td>
                    <td id="cell-0-1" class="column-1 pt-point">
                        <span><a href="/journal/add-edit-lesson?schedule=2001&amp;journal=7001">7</a><span class="pt-month">Вер.</span></span>
                    </td>
                    <td id="cell-0-2" class="column-2 pt-point">
                        <span><a href="/journal/add-edit-lesson?schedule=2002&amp;journal=7001">14</a><span class="pt-month">Вер.</span></span>
                    </td>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>1</td><td class="pt-theme" data-student-id="9001">Учень Тест</td>
                    <td class="pt-point"><input class="mark-cell" data-mark-id="10" value="10"></td>
                    <td class="pt-point"><input class="mark-cell" data-mark-id="11" value="11"></td>
                </tr>
            </tbody>
        </table>
        <div class="homework-table">
            <div class="homework-container">
                <div class="homework-row homework-row--header">
                    <div class="homework__item">№</div>
                    <div class="homework__item">Дата</div>
                    <div class="homework__item">Зміст</div>
                    <div class="homework__item">Домашнє завдання</div>
                    <div class="homework__item">Заміна</div>
                </div>
                <div class="homework-row">
                    <div class="homework__item"><a href="/journal/add-edit-home-task?schedule=2001&amp;journal=7001">1</a></div>
                    <div class="homework__item">7 вересня</div>
                    <div class="homework__item">Інформація та повідомлення</div>
                    <div class="homework__item">на 14 вересня Опрацювати §1, впр. 2</div>
                    <div class="homework__item"></div>
                </div>
                <div class="homework-row">
                    <div class="homework__item"><a href="/journal/add-edit-home-task?schedule=2002&amp;journal=7001">2</a></div>
                    <div class="homework__item">14 вересня</div>
                    <div class="homework__item">Інформаційні процеси</div>
                    <div class="homework__item">на 21.09: Тест на платформі</div>
                    <div class="homework__item">Заміна вчителя</div>
                </div>
            </div>
        </div>
        </body></html>"""
        service = NZService()
        data = service.fetch_journal_data('7001', html_content=html)
        self.assertEqual(len(data['lessons']), 2)
        l1, l2 = data['lessons']
        self.assertEqual(l1['topic'], 'Інформація та повідомлення')
        self.assertEqual(l1['homework'], 'Опрацювати §1, впр. 2')
        self.assertEqual(l1['homework_due_date'], dt.date(2026, 9, 14))
        self.assertEqual(l2['topic'], 'Інформаційні процеси')
        self.assertEqual(l2['homework'], 'Тест на платформі')
        self.assertEqual(l2['homework_due_date'], dt.date(2026, 9, 21))

        # Test importing to local journal
        service.import_journal_to_local('7001', self.journal, html_content=html, user=self.teacher)
        les1 = self.journal.lessons.get(nz_schedule_id='2001')
        self.assertEqual(les1.topic, 'Інформація та повідомлення')
        self.assertEqual(les1.homework, 'Опрацювати §1, впр. 2')
        self.assertEqual(les1.homework_due_date, dt.date(2026, 9, 14))
        les2 = self.journal.lessons.get(nz_schedule_id='2002')
        self.assertEqual(les2.topic, 'Інформаційні процеси')
        self.assertEqual(les2.homework, 'Тест на платформі')
        self.assertEqual(les2.homework_due_date, dt.date(2026, 9, 21))

    def test_sync_journal_updates_empty_local_homework_and_topic(self):
        # Local lesson created with empty topic and empty homework
        self.l1.topic = ''
        self.l1.homework = ''
        self.l1.is_synced_to_nz = False
        self.l1.nz_schedule_id = '3001'
        self.l1.save()

        html = """<!doctype html><html><head><meta name="csrf-token" content="token"></head><body>
        <select id="semester"><option selected>I семестр 2026/2027</option></select>
        <table class="point-table" id="journalList">
            <thead>
                <tr>
                    <td>#</td><td class="pt-theme">ПІБ</td>
                    <td id="cell-0-1" class="column-1 pt-point">
                        <span><a href="/journal/add-edit-lesson?schedule=3001&amp;journal=7001">5</a><span class="pt-month">Вер.</span></span>
                    </td>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>1</td><td class="pt-theme" data-student-id="9001">Учень</td>
                    <td class="pt-point"><input class="mark-cell" data-mark-id="10" value="10"></td>
                </tr>
            </tbody>
        </table>
        <div class="homework-row homework-row--header">
            <div class="homework__item">№</div><div class="homework__item">Дата</div>
            <div class="homework__item">Зміст</div><div class="homework__item">Домашнє завдання</div>
            <div class="homework__item">Заміна</div>
        </div>
        <div class="homework-row">
            <div class="homework__item">1</div>
            <div class="homework__item">5 вересня</div>
            <div class="homework__item">Оновлена тема з nz.ua</div>
            <div class="homework__item">на 12 вересня Домашнє з сайту</div>
            <div class="homework__item"></div>
        </div>
        </body></html>"""

        service = NZService()
        service.import_journal_to_local('7001', self.journal, html_content=html, replace=False, user=self.teacher)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.topic, 'Оновлена тема з nz.ua')
        self.assertEqual(self.l1.homework, 'Домашнє з сайту')
        self.assertEqual(self.l1.homework_due_date, dt.date(2026, 9, 12))

