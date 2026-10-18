import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.journals.models import GradeStudentMapping, ExtraStudent, GradePullLog, Grade, RosterState
from apps.journals.services.extra_students import match_student, merge_extra
from apps.journals.services.external_grades import ExternalAPIError
from apps.journals.services.roster_transfer import roster_request, send_journal_roster
from school_sync.journal_roster import validate
from tests import test_simple_exchange as exchange


class SyncDiagnosticsTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection

    def confirm_roster(self, connection, native_id=41):
        payload, refs = roster_request(connection)
        ref = next(ref for ref, student in refs.items() if student.pk == self.student.pk)
        connection.roster_report = {'schema_version': 2, 'source': connection.source,
            'instance_id': connection.roster_cache['instance_id'], 'class_id': 7,
            'mappings': [{'journal_id': ref, 'local_id': native_id}]}
        connection.save(update_fields=['roster_report'])
        return ref

    def test_result_alias_cannot_replace_confirmed_roster_id_even_if_mapping_was_lost(self):
        connection = self.connection(mappings=True)
        ref = self.confirm_roster(connection)
        connection.student_mappings.all().delete()
        GradeStudentMapping.objects.create(connection=connection, student=self.student, external_id='unmapped:12')
        GradeStudentMapping.objects.create(connection=connection, student=self.student, external_id='99')
        payload, _ = roster_request(connection)
        validate(payload)
        self.assertEqual(next(r['local_id'] for r in payload['students'] if r['id'] == ref), '41')
        self.assertEqual(connection.student_mappings.count(), 2)
        self.assertFalse(Grade.objects.exists())

    def test_unconfirmed_result_alias_is_never_sent_as_native_id(self):
        connection = self.connection()
        GradeStudentMapping.objects.create(connection=connection, student=self.student, external_id='unmapped:12')
        payload, refs = roster_request(connection)
        validate(payload)
        ref = next(ref for ref, student in refs.items() if student.pk == self.student.pk)
        self.assertIsNone(next(r['local_id'] for r in payload['students'] if r['id'] == ref))
        self.journal.ignored_students.add(self.student)
        payload, refs = roster_request(connection)
        self.assertNotIn(self.student, refs.values())

    def test_ignored_pupil_with_lost_mapping_uses_confirmed_roster_id_and_inactive_status(self):
        connection = self.connection(mappings=True)
        ref = self.confirm_roster(connection)
        connection.student_mappings.all().delete()
        self.journal.ignored_students.add(self.student)
        payload, _ = roster_request(connection)
        row = next(r for r in payload['students'] if r['id'] == ref)
        self.assertEqual(row['local_id'], '41')
        self.assertFalse(row['active'])
        self.assertTrue(row['ignored'])

    def test_multiple_unconfirmed_numeric_ids_fail_before_network_and_log_reason(self):
        connection = self.connection(mappings=True)
        GradeStudentMapping.objects.create(connection=connection, student=self.student, external_id='99')
        with patch('apps.journals.services.roster_transfer.requests.Session') as network:
            with self.assertRaises(ExternalAPIError):
                send_journal_roster(connection, self.teacher)
        network.assert_not_called()
        error = GradePullLog.objects.get().errors[0]
        self.assertEqual(error['code'], 'roster_validation_error')
        self.assertIsNone(error['http_status'])

    def test_matching_and_merging_extra_aliases_preserve_canonical_mapping(self):
        connection = self.connection(mappings=True)
        self.confirm_roster(connection)
        extra = ExtraStudent.objects.create(connection=connection, external_key='unmapped:12', name='Вигаданий Профіль')
        match_student(extra, self.student, self.teacher)
        alias = ExtraStudent.objects.create(connection=connection, external_key='unmapped:13', name='Вигаданий Псевдонім')
        merge_extra(alias, extra, self.teacher)
        self.assertEqual(set(connection.student_mappings.values_list('external_id', flat=True)), {'41', 'unmapped:12', 'unmapped:13'})

    def test_download_is_read_only_and_excludes_sensitive_and_unrecognized_fields(self):
        connection = self.connection(mappings=True)
        self.confirm_roster(connection)
        sentinel = 'PRIVATE_NAME_KEY_HOST_PAYLOAD'
        connection.name = connection.api_key = sentinel
        connection.save()
        GradePullLog.objects.create(connection=connection, user=self.teacher,
            counts={'operation': 'roster', 'errors': 1, 'api_key': sentinel, 'added': sentinel},
            errors=[{'message': 'HTTP 400 '+sentinel, 'row': {'name': sentinel}, 'code': sentinel}])
        before = (RosterState.objects.get(pk=1).data, GradePullLog.objects.count())
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
            response = self.client.get(reverse('portal:integration_diagnostics', args=[self.journal.pk]))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        for private in (sentinel, self.student.last_name, self.teacher.username, connection.url, str(self.student.nz_student_id)):
            self.assertNotIn(private, text)
        self.assertEqual(response['Cache-Control'], 'no-store')
        log = response.json()['journals'][0]['connections'][0]['recent_calls'][0]
        self.assertEqual(log['errors'], [{'code': 'roster_http_error', 'http_status': 400}])
        self.assertNotIn('added', log['counts'])
        self.assertEqual((RosterState.objects.get(pk=1).data, GradePullLog.objects.count()), before)

    def test_anonymous_and_other_teacher_cannot_download_report(self):
        url = reverse('portal:integration_diagnostics', args=[self.journal.pk])
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        other = User.objects.create_user(username='other_synthetic', role=User.Role.TEACHER, school=self.school)
        self.client.force_login(other)
        self.assertIn(self.client.get(url).status_code, (403, 404))

    def test_administrative_audit_covers_all_students_without_network(self):
        self.connection()
        out = io.StringIO()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
            call_command('audit_integrations', stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report['totals']['students'], 2)
        self.assertEqual(report['totals']['classes'], 1)
        self.assertEqual(report['totals']['enrollment_school_mismatches'], 0)
        self.assertEqual(report['network_requests'], 0)

    def test_quick_send_returns_actionable_json_error_and_fresh_timestamp(self):
        connection = self.connection()
        url = reverse('portal:quick_send_roster', args=[self.journal.pk, connection.pk])
        with patch('apps.portal.views_connection_setup.send_journal_roster', side_effect=ExternalAPIError('Перевірте відповідність учня.')):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Перевірте відповідність учня.', response.json()['message'])
        def succeed(current, user):
            type(current).objects.filter(pk=current.pk).update(roster_sent_at=timezone.now())
            return {'counts': {'created': 0, 'updated': 0, 'needs_review': 1, 'conflicts': 0}}
        with patch('apps.portal.views_connection_setup.send_journal_roster', side_effect=succeed):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['sent_at'])
        self.assertEqual(response.json()['level'], 'warning')
