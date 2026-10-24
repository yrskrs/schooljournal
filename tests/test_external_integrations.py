import datetime as dt
import hashlib
from unittest.mock import patch, MagicMock

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.personal import ensure_personal_account
from apps.curriculum.models import ClassEnrollment, Student, StudentClass
from apps.journals.models import Journal, Grade, Lesson, Attendance, NZAccount, ExternalGrade, StudentAPIGrant, GradeAPIConnection
from apps.journals.services.assessment import calculate
from apps.journals.services.external_grades import import_grade_records, review_external_grade, fetch_grade_payload, validate_api_url, ExternalAPIError
from apps.journals.services.nz_service import NZService
from apps.portal.forms_integrations import GradeAPIConnectionForm
from apps.portal.permissions import has_journal_access, get_user_journals
from tests import test_journal_workflows as workflows


class ExternalIntegrationTests(TestCase):
    def setUp(self):
        workflows.JournalWorkflowsTests.setUp(self)
        self.connection = GradeAPIConnection.objects.create(journal=self.journal, name='Вигаданий сервер робіт',
            url='http://192.168.1.13:8080/api/grades')
        self.start = self.year.start_date
        self.end = self.year.end_date

    def url(self, name, **kwargs):
        return reverse('portal:' + name, kwargs={'journal_id': self.journal.pk, **kwargs})

    def row(self, **changes):
        return {'id': 'synthetic-work-1', 'student_id': self.student.pk, 'date': self.l1.date.isoformat(),
            'value': 10, 'is_ai': False, 'approved_by_teacher': True, **changes}

    def pull(self, *rows):
        return import_grade_records(self.connection, list(rows) or [self.row()], self.teacher, self.start, self.end)

    def record(self):
        return ExternalGrade.objects.get(connection=self.connection)

    def review(self, action='approve', replace=False, version=None):
        record = self.record()
        return review_external_grade(record.pk, self.journal, self.teacher, action,
            version or record.updated_at.isoformat(), replace=replace)

    def personal(self):
        account = NZAccount.objects.create(user=self.teacher, login='synthetic-nz-id', is_active=True, phpsessid='synthetic-cookie')
        account = ensure_personal_account(account)
        self.journal.refresh_from_db()
        return account

    def test_settings_and_review_pages_render(self):
        self.assertEqual(self.client.get(self.url('journal_integrations')).status_code, 200)
        self.assertEqual(self.client.get(self.url('external_grade_review')).status_code, 200)

    def test_new_nz_login_creates_independent_personal_profile_without_admin(self):
        self.client.logout()
        with patch.object(NZService, 'login', return_value=(True, 'ok')), patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            response = self.client.post(reverse('nz_login'), {'auth_method': 'credentials', 'login': 'new-nz-synthetic', 'password': 'synthetic'})
        user = User.objects.get(pk=self.client.session['_auth_user_id'])
        self.assertTrue(user.is_nz_personal)
        self.assertFalse(user.has_usable_password())
        self.assertNotEqual(user.school_id, self.school.pk)
        self.assertEqual(response.url, reverse('portal:nz_journals_list'))
        self.assertFalse(has_journal_access(user, self.journal))

    def test_repeat_nz_login_reuses_profile(self):
        self.client.logout()
        with patch.object(NZService, 'login', return_value=(True, 'ok')), patch.object(NZService, 'verify_session', return_value=(True, 'Вигаданий Вчитель')):
            for i in range(2):
                self.client.post(reverse('nz_login'), {'auth_method': 'credentials', 'login': 'new-nz-synthetic', 'password': 'synthetic'})
                if i == 0:
                    first = self.client.session['_auth_user_id']
                self.assertEqual(self.client.session['_auth_user_id'], first)
                self.client.logout()
        self.assertEqual(NZAccount.objects.filter(login='new-nz-synthetic').count(), 1)

    def test_existing_account_gets_personal_identity_preserving_records_and_local_access(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        account = self.personal()
        self.assertTrue(account.user.is_nz_personal)
        self.assertEqual(self.journal.teacher_id, account.user_id)
        self.assertEqual(self.journal.local_profile_id, self.teacher.pk)
        self.assertTrue(has_journal_access(self.teacher, self.journal, write=True))
        self.assertTrue(has_journal_access(account.user, self.journal, write=True))
        self.assertEqual(Grade.objects.get(lesson=self.l1, student=self.student).value, '8')
        self.assertEqual(ensure_personal_account(account).user_id, account.user_id)

    def test_unlink_returns_to_personal_and_removes_local_access(self):
        account = self.personal()
        self.client.post(self.url('journal_integrations'), {'action': 'unlink_profile'})
        self.journal.refresh_from_db()
        self.assertIsNone(self.journal.local_profile_id)
        self.assertTrue(has_journal_access(account.user, self.journal, write=True))
        self.assertFalse(has_journal_access(self.teacher, self.journal))
        self.assertEqual(self.client.get(self.url('journal_detail')).status_code, 403)
        self.assertEqual(self.journal.lessons.count(), 3)

    def test_personal_owner_can_link_only_with_local_password(self):
        account = self.personal()
        self.journal.local_profile = None; self.journal.save()
        self.client.force_login(account.user)
        response = self.client.post(self.url('journal_integrations'), {'action': 'link_profile', 'username': self.teacher.username, 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        self.journal.refresh_from_db(); self.assertIsNone(self.journal.local_profile_id)
        self.client.post(self.url('journal_integrations'), {'action': 'link_profile', 'username': self.teacher.username, 'password': 'synthetic-test'})
        self.journal.refresh_from_db(); self.assertEqual(self.journal.local_profile_id, self.teacher.pk)
        self.assertTrue(has_journal_access(self.teacher, self.journal, write=True))

    def test_linked_profile_cannot_reassign_link_to_another_user(self):
        self.personal()
        self.assertEqual(self.client.post(self.url('journal_integrations'), {'action': 'link_profile'}).status_code, 403)

    def test_other_school_teacher_cannot_access_settings_or_records(self):
        outsider = User.objects.create_user(username='outsider', school=None)
        self.client.force_login(outsider)
        for name in ('journal_integrations', 'external_grade_review'):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403)

    def test_linked_journal_visible_in_dashboard(self):
        account = self.personal()
        self.assertIn(self.journal, get_user_journals(self.teacher))
        self.assertIn(self.journal, get_user_journals(account.user))
        self.assertContains(self.client.get(reverse('portal:teacher_dashboard')), 'Тестовий предмет')

    def test_linked_local_profile_uses_owner_nz_account_for_import(self):
        self.personal()
        stats = {'students_matched': 0, 'students_created': 0, 'lessons_synced': 0, 'grades_synced': 0, 'attendances_synced': 0}
        with patch.object(NZService, 'import_journal_to_local', return_value=stats) as remote:
            response = self.client.post(self.url('nz_journal_import_for_journal'), {'format': 'json'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        remote.assert_called_once()

    def test_shared_class_import_cannot_take_another_teachers_journal(self):
        account = NZAccount.objects.create(user=self.teacher, is_active=True)
        other = User.objects.create_user(username='same-school-other', school=self.school)
        self.journal.teacher = other; self.journal.save()
        with patch.object(NZService, 'import_journal_to_local') as remote:
            response = self.client.post(reverse('portal:nz_journal_import'), {'nz_journal_id': 'new-remote',
                'class_name': self.group.name, 'subject_name': self.subject.name})
        self.assertEqual(response.status_code, 403)
        remote.assert_not_called()
        self.journal.refresh_from_db(); self.assertEqual(self.journal.teacher_id, other.pk)

    def test_api_is_disabled_and_rejects_session_without_key(self):
        response = self.client.get(self.url('student_list_api'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def token(self, fields=None):
        raw = 'synthetic-api-secret'
        StudentAPIGrant.objects.create(journal=self.journal, enabled=True, fields=fields or ['id', 'name'],
            token_hash=hashlib.sha256(raw.encode()).hexdigest())
        return raw

    def test_api_provides_only_allowed_fields_and_active_enrollments(self):
        token = self.token(['id', 'nz_student_id'])
        enrollment = ClassEnrollment.objects.get(student=self.empty_student, student_class=self.group)
        enrollment.is_active = False; enrollment.save()
        self.client.logout()
        response = self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['students'], [{'id': self.student.pk, 'nz_student_id': 'synthetic-student'}])
        self.assertNotIn('name', response.json()['students'][0])

    def test_wrong_token_and_query_token_are_rejected(self):
        token = self.token()
        self.assertEqual(self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer wrong').status_code, 401)
        self.assertEqual(self.client.get(self.url('student_list_api'), {'token': token}).status_code, 401)

    def test_student_api_excludes_ignored_pupils_and_restores_same_id(self):
        token = self.token()
        self.journal.ignored_students.add(self.student)
        response = self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual([row['id'] for row in response.json()['students']], [self.empty_student.pk])
        self.journal.ignored_students.remove(self.student)
        response = self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual({row['id'] for row in response.json()['students']}, {self.student.pk, self.empty_student.pk})

    def test_token_cannot_access_other_journal(self):
        token = self.token()
        self.assertEqual(self.client.get(reverse('portal:student_list_api', kwargs={'journal_id': 999999}), HTTP_AUTHORIZATION='Bearer ' + token).status_code, 401)

    def test_expired_key_is_rejected(self):
        token = self.token()
        StudentAPIGrant.objects.filter(journal=self.journal).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
        self.assertEqual(self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + token).status_code, 401)

    def test_rotation_shows_key_once_and_revokes_previous(self):
        self.client.post(self.url('journal_integrations'), {'action': 'student_api', 'enabled': 'on', 'fields': ['id']})
        self.client.post(self.url('journal_integrations'), {'action': 'rotate_key'})
        first = self.client.get(self.url('journal_integrations')).context['new_api_key']
        self.assertTrue(first)
        self.assertNotEqual(StudentAPIGrant.objects.get(journal=self.journal).token_hash, first)
        self.assertEqual(self.client.get(self.url('journal_integrations')).context['new_api_key'], '')
        self.assertEqual(self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + first).status_code, 200)
        self.client.post(self.url('journal_integrations'), {'action': 'rotate_key'})
        self.assertEqual(self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + first).status_code, 401)

    def test_revoke_disables_api(self):
        token = self.token()
        self.client.post(self.url('journal_integrations'), {'action': 'revoke_key'})
        self.assertEqual(self.client.get(self.url('student_list_api'), HTTP_AUTHORIZATION='Bearer ' + token).status_code, 401)

    def test_api_fields_reject_unoffered_personal_fields(self):
        response = self.client.post(self.url('journal_integrations'), {'action': 'student_api', 'enabled': 'on', 'fields': ['phone']})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StudentAPIGrant.objects.exists())

    def test_human_grade_imports_with_origin(self):
        log = self.pull()
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.assertEqual((grade.value, grade.is_approved, grade.is_synced_to_nz), ('10', True, False))
        self.assertEqual(grade.import_record.connection_id, self.connection.pk)
        self.assertEqual(log.counts['applied'], 1)

    def test_unapproved_ai_is_visible_but_not_in_average_or_assessment(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.assertFalse(grade.is_approved)
        self.assertIsNone(grade.numeric_value)
        rows, fingerprint, lessons = calculate(self.journal, [self.l1], 'current', 'nearest')
        self.assertEqual(rows[0]['value'], '')
        response = self.client.get(self.url('journal_detail'))
        self.assertEqual(response.context['student_averages'][self.student.pk], '-')
        self.assertContains(response, 'ШІ · попередня')
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertIsNone(soup.select_one(f'td[data-lesson-id="{self.l1.pk}"][data-student-id="{self.student.pk}"] input'))

    def test_ai_confirmed_on_external_server_can_be_final(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=True))
        self.assertTrue(Grade.objects.get(lesson=self.l1, student=self.student).is_approved)

    def test_missing_ai_metadata_is_conservatively_tentative(self):
        row = self.row(approved_by_teacher=False); del row['is_ai']
        self.pull(row)
        self.assertFalse(Grade.objects.get(lesson=self.l1, student=self.student).is_approved)

    def test_non_boolean_ai_flags_are_not_treated_as_approval(self):
        log = self.pull(self.row(is_ai='false', approved_by_teacher='true'))
        self.assertEqual(log.counts['errors'], 1)
        self.assertFalse(Grade.objects.exists())

    def test_repeat_pull_does_not_duplicate(self):
        self.pull(); log = self.pull()
        self.assertEqual(log.counts['unchanged'], 1)
        self.assertEqual(Grade.objects.count(), 1)
        self.assertEqual(ExternalGrade.objects.count(), 1)

    def test_teacher_approval_preserves_on_repeat_pull(self):
        row = self.row(is_ai=True, approved_by_teacher=False)
        self.pull(row); self.review(); self.pull(row)
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.assertTrue(grade.is_approved)
        self.assertEqual(grade.numeric_value, 10)

    def test_teacher_rejection_preserves_on_repeat_pull(self):
        row = self.row(is_ai=True, approved_by_teacher=False)
        self.pull(row); self.review('reject'); self.pull(row)
        self.assertFalse(Grade.objects.exists())
        self.assertEqual(self.record().status, 'REJECTED')

    def test_changed_ai_payload_requires_new_review(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        self.pull(self.row(value=11, is_ai=True, approved_by_teacher=False))
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.assertEqual(grade.value, '11'); self.assertFalse(grade.is_approved)

    def test_changed_final_payload_preserves_final_mark_as_conflict(self):
        self.pull(); self.pull(self.row(value=11))
        self.assertEqual(Grade.objects.get(lesson=self.l1, student=self.student).value, '10')
        self.assertEqual(self.record().status, 'CONFLICT')

    def test_existing_manual_mark_and_manual_clear_are_preserved(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        log = self.pull()
        grade.refresh_from_db(); self.assertEqual(grade.value, '8')
        self.assertEqual(log.counts['conflicts'], 1)
        grade.value = ''; grade.save()
        log = self.pull(self.row(value=11))
        grade.refresh_from_db(); self.assertEqual(grade.value, '')
        self.assertEqual(log.counts['conflicts'], 1)

    def test_identical_manual_mark_is_unchanged_without_taking_ownership(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='10',
            is_approved=True, is_synced_to_nz=True, created_by=self.teacher)
        before = (grade.import_record_id, grade.created_by_id, grade.is_synced_to_nz)
        for _ in range(2):
            log = self.pull()
            self.assertEqual((log.counts['conflicts'], log.counts['unchanged']), (0, 1))
        grade.refresh_from_db()
        self.assertEqual(before, (grade.import_record_id, grade.created_by_id, grade.is_synced_to_nz))
        self.assertEqual(self.record().status, 'APPLIED')

    def test_old_conflict_resolves_when_teacher_has_entered_same_mark(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_approved=True)
        self.pull()
        self.assertEqual(self.record().status, 'CONFLICT')
        grade.value = '10'; grade.save()
        self.assertEqual(self.pull().counts['unchanged'], 1)
        self.assertEqual(self.record().status, 'APPLIED')

    def test_identical_mark_with_absence_remains_a_conflict(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='10', is_approved=True)
        Attendance.objects.create(lesson=self.l1, student=self.student, status='ABSENT_UNEXCUSED')
        self.assertEqual(self.pull().counts['conflicts'], 1)

    def test_attendance_conflict_is_preserved(self):
        Attendance.objects.create(lesson=self.l1, student=self.student, status='ABSENT_UNEXCUSED')
        self.assertEqual(self.pull().counts['conflicts'], 1)
        self.assertFalse(Grade.objects.exists())

    def test_replace_conflict_requires_explicit_checkbox(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        self.pull()
        with self.assertRaises(ExternalAPIError):
            self.review()
        self.review(replace=True)
        self.assertEqual(Grade.objects.get(lesson=self.l1, student=self.student).value, '10')

    def test_replace_attendance_clears_it_only_after_approval(self):
        Attendance.objects.create(lesson=self.l1, student=self.student, status='ABSENT_UNEXCUSED')
        self.pull(); self.review(replace=True)
        self.assertEqual(Attendance.objects.get(lesson=self.l1, student=self.student).status, 'PRESENT')

    def test_stale_review_is_rejected(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        version = self.record().updated_at.isoformat()
        self.pull(self.row(value=11, is_ai=True, approved_by_teacher=False))
        with self.assertRaises(ExternalAPIError):
            self.review(version=version)

    def test_review_form_version_works_in_kyiv_timezone(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        response = self.client.get(self.url('external_grade_review'))
        soup = BeautifulSoup(response.content, 'html.parser')
        version = soup.select_one('input[name="version"]')['value']
        self.client.post(self.url('external_grade_review'), {'record_id': self.record().pk, 'action': 'approve', 'version': version})
        self.assertTrue(Grade.objects.get(lesson=self.l1, student=self.student).is_approved)

    def test_pending_grade_cannot_be_made_final_by_regular_cell_post(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        response = self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': self.student.pk, 'value': '10'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Grade.objects.get(lesson=self.l1, student=self.student).is_approved)

    def test_pending_ai_is_excluded_from_preview_dry_run_and_real_push(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        self.l3.is_synced_to_nz = True; self.l3.save()
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        self.assertEqual(service.get_unsynced_changes(self.journal)['grades_count'], 0)
        self.assertEqual(service.get_unsynced_changes(self.journal)['blocked_grades_count'], 1)
        with patch.object(service.session, 'get') as get, patch.object(service.session, 'post') as post:
            self.assertEqual(service.push_journal_changes(self.journal, dry_run=True).items_count, 0)
            self.assertEqual(service.push_journal_changes(self.journal).items_count, 0)
        get.assert_not_called(); post.assert_not_called()

    def test_final_ai_becomes_eligible_only_after_review(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False)); self.review()
        service = NZService()
        self.assertEqual(service.get_unsynced_changes(self.journal)['grades_count'], 1)

    def test_last_upload_gate_blocks_unapproved_snapshot(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.l3.is_synced_to_nz = True; self.l3.save()
        service = NZService(); service.csrf_token = 'synthetic-csrf'
        with patch.object(service, '_pending_cells', return_value=[(grade, None, grade, grade.value)]), patch.object(service.session, 'post') as post:
            result = service.push_journal_changes(self.journal)
        self.assertEqual(result.error_count, 1)
        post.assert_not_called()

    def test_linked_local_profile_can_preview_and_dry_run_owners_journal(self):
        self.personal()
        self.pull()
        preview = self.client.get(self.url('nz_sync_preview'), {'format': 'json'}).json()
        response = self.client.post(self.url('nz_sync_execute'), {'preview_token': preview['preview_token'], 'dry_run': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')

    def test_invalid_values_and_dates_and_unknown_students_are_reported(self):
        rows = [self.row(id='bad1', value=13), self.row(id='bad2', date='bad'), self.row(id='bad3', student_id=999999),
            self.row(id='bad4', date='2025-09-01'), self.row(id='bad5', value=True)]
        self.assertEqual(self.pull(*rows).counts['errors'], 5)
        self.assertFalse(Grade.objects.exists())

    def test_duplicate_external_id_in_response_is_rejected(self):
        log = self.pull(self.row(), self.row(value=11))
        self.assertEqual(log.counts['errors'], 1)
        self.assertEqual(Grade.objects.count(), 1)

    def test_ambiguous_date_requires_number_and_never_creates_lesson(self):
        Lesson.objects.create(journal=self.journal, date=self.l1.date, topic='Другий урок', lesson_number=2)
        self.assertEqual(self.pull().counts['errors'], 1)
        self.assertEqual(self.pull(self.row(lesson_number=1)).counts['applied'], 1)
        self.assertEqual(self.journal.lessons.count(), 4)

    def test_source_cannot_move_existing_work_to_another_student_or_lesson(self):
        self.pull()
        self.assertEqual(self.pull(self.row(student_id=self.empty_student.pk)).counts['errors'], 1)
        self.assertEqual(self.pull(self.row(date=self.l2.date.isoformat())).counts['errors'], 1)
        self.assertEqual(Grade.objects.count(), 1)

    def test_nz_identifier_and_nested_json_fields_are_supported(self):
        self.connection.student_identifier = 'nz_student_id'; self.connection.student_key = 'student.nz_id'
        row = self.row(); row['student'] = {'nz_id': self.student.nz_student_id}
        self.assertEqual(self.pull(row).counts['applied'], 1)

    def test_unknown_lesson_and_summary_column_are_not_imported(self):
        self.assertEqual(self.pull(self.row(date='2026-09-03')).counts['errors'], 1)
        self.l1.lesson_type = 'THEMATIC'; self.l1.save()
        self.assertEqual(self.pull().counts['errors'], 1)

    def test_api_url_allows_lan_and_rejects_service_addresses(self):
        validate_api_url(self.connection.url)
        for url in ['http://127.0.0.1/api', 'http://169.254.169.254/api', 'file:///etc/passwd', 'http://user:password@192.168.1.13/api', 'http://localhost/api']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_api_url(url)

    def test_saved_outgoing_key_not_exposed_or_erased_on_edit(self):
        self.connection.api_key = 'synthetic-outgoing-secret'; self.connection.save()
        response = self.client.get(reverse('portal:connection_legacy_edit', args=[self.connection.pk]))
        self.assertNotContains(response, 'synthetic-outgoing-secret')
        data = {name: getattr(self.connection, name) for name in GradeAPIConnectionForm.Meta.fields}
        data['api_key'] = ''
        form = GradeAPIConnectionForm(data, instance=self.connection)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['api_key'], 'synthetic-outgoing-secret')

    def test_http_request_uses_bearer_and_window_and_rejects_redirect(self):
        self.connection.api_key = 'synthetic-key'
        session = MagicMock()
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [b'{"grades":[]}']
        session.get.return_value.__enter__.return_value = response
        with patch('apps.journals.services.external_grades.requests.Session') as factory:
            factory.return_value.__enter__.return_value = session
            self.assertEqual(fetch_grade_payload(self.connection, self.start, self.end), [])
        kwargs = session.get.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-key')
        self.assertEqual(kwargs['params']['date_from'], self.start.isoformat())
        response.status_code = 302
        with patch('apps.journals.services.external_grades.requests.Session') as factory, self.assertRaises(ExternalAPIError):
            factory.return_value.__enter__.return_value = session
            fetch_grade_payload(self.connection, self.start, self.end)

    def test_pull_view_runs_import_only_for_current_journal(self):
        with patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[self.row()]):
            response = self.client.post(self.url('pull_external_grades', connection_id=self.connection.pk), {'date_from': self.start.isoformat(), 'date_to': self.end.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Grade.objects.count(), 1)

    def test_review_is_journal_scoped(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        record = self.record()
        other_class = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Інший клас')
        other = Journal.objects.create(school=self.school, academic_year=self.year, student_class=other_class, subject=self.subject, teacher=self.teacher)
        response = self.client.post(reverse('portal:external_grade_review', kwargs={'journal_id': other.pk}),
            {'record_id': record.pk, 'action': 'approve', 'version': record.updated_at.isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_archived_journal_cannot_pull_or_review(self):
        self.pull(self.row(is_ai=True, approved_by_teacher=False))
        self.journal.is_archived = True; self.journal.save()
        self.assertEqual(self.client.post(self.url('external_grade_review'), {'record_id': self.record().pk}).status_code, 403)
        self.assertEqual(self.client.post(self.url('pull_external_grades', connection_id=self.connection.pk)).status_code, 403)
