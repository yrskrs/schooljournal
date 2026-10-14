import json
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.urls import reverse
from apps.journals.models import GradeAPIConnection, GradeStudentMapping, Grade, ExtraStudent, APIInboxResult, NZSyncLog
from apps.curriculum.models import ClassEnrollment
from apps.journals.services.connection_setup import validate_roster
from apps.journals.services.roster_transfer import roster_request, send_journal_roster
from apps.journals.services.external_grades import import_grade_records
from tests import test_journal_workflows as workflows


class SimpleExchangeTests(TestCase):
    setUp = workflows.JournalWorkflowsTests.setUp

    def connection(self, mappings=False):
        cache = validate_roster({'schema_version': 2, 'source': 'schooltest',
            'instance_id': '11111111-1111-4111-8111-111111111111', 'capabilities': ['journal_roster_import'],
            'classes': [{'id': 7, 'name': self.group.name}], 'subjects': [{'id': 3, 'name': self.subject.name}],
            'students': [{'id': 41, 'class_id': 7, 'name': self.student.get_full_name_ukr()}]}, 'schooltest')
        connection = GradeAPIConnection.objects.create(journal=self.journal, name='Синтетичні тести', source='schooltest', contract_version=2,
            source_class_id='7', source_subject_id='3', url='http://schooltest-api:8000/api/v2/journal/grades/', api_key='synthetic-key', roster_cache=cache)
        if mappings: GradeStudentMapping.objects.create(connection=connection, external_id='41', student=self.student)
        return connection

    def row(self, **changes):
        data = dict(source='schooltest', id='schooltest:unmapped:12', student_id=None, student_key='unmapped:12',
            student_name='Вигаданий У.', class_id=7, subject_id=3, work_id=9, title='Синтетичний тест',
            points=8, max_points=12, value='8', grading_scale=12, date=self.l1.date.isoformat(), status='final',
            updated_at='2026-10-11T10:00:00+03:00', attempt_id=12, is_ai=False, approved_by_teacher=True)
        data.update(changes)
        return data

    def pull(self, connection, row):
        return import_grade_records(connection, [row], self.teacher, self.year.start_date, self.year.end_date)

    def test_extra_result_is_saved_without_enrollment_or_grade_and_repeat_has_no_duplicates(self):
        connection = self.connection()
        before = ClassEnrollment.objects.count()
        self.assertEqual(self.pull(connection, self.row()).counts['errors'], 0)
        self.pull(connection, self.row())
        self.assertEqual(ExtraStudent.objects.count(), 1); self.assertEqual(APIInboxResult.objects.count(), 1)
        self.assertFalse(Grade.objects.exists()); self.assertEqual(ClassEnrollment.objects.count(), before)
        self.assertContains(self.client.get(reverse('portal:api_inbox', args=[self.journal.pk])), 'Вигаданий У.')

    def test_typo_match_requires_teacher_confirmation_and_does_not_import_automatically(self):
        connection = self.connection(); self.pull(connection, self.row())
        extra = ExtraStudent.objects.get(); url = reverse('portal:api_inbox', args=[self.journal.pk])
        response = self.client.post(url, {'action': 'match', 'extra_id': extra.pk, 'version': extra.updated_at.isoformat(), 'student': self.student.pk})
        self.assertEqual(response.status_code, 302); self.assertFalse(Grade.objects.exists())
        self.assertEqual(self.pull(connection, self.row()).counts['added'], 1)
        self.assertEqual(Grade.objects.get().student_id, self.student.pk)
        self.assertEqual(ExtraStudent.objects.get().name, 'Вигаданий У.')
        self.assertEqual(self.pull(connection, self.row()).counts['unchanged'], 1)

    def test_unusable_mark_and_missing_lesson_remain_available_for_manual_decision(self):
        connection = self.connection(mappings=True)
        row = self.row(student_id=41, student_key='41', value='8.5')
        self.pull(connection, row)
        self.assertIn('цілим', APIInboxResult.objects.get().reason)
        self.assertFalse(Grade.objects.exists())
        row.update(value='8', date='2026-09-03', updated_at='2026-10-11T11:00:00+03:00', id='other-work')
        self.pull(connection, row)
        self.assertEqual(APIInboxResult.objects.filter(resolved=False).count(), 2)

    def test_saved_result_can_be_applied_by_explicit_teacher_button(self):
        connection = self.connection(); self.pull(connection, self.row())
        extra = ExtraStudent.objects.get(); extra.matched_student = self.student; extra.save()
        result = APIInboxResult.objects.get()
        url = reverse('portal:api_inbox', args=[self.journal.pk])
        response = self.client.post(url, {'action': 'apply_saved', 'result_id': result.pk,
            'version': result.updated_at.isoformat(), 'lesson': self.l1.pk})
        self.assertEqual(response.status_code, 302); self.assertEqual(Grade.objects.get().value, '8')
        result.refresh_from_db(); self.assertTrue(result.resolved)

    def test_anonymous_cannot_see_or_match_extra_students(self):
        connection = self.connection(); self.pull(connection, self.row())
        self.client.logout(); url = reverse('portal:api_inbox', args=[self.journal.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, {'action': 'match'}).status_code, 302)

    def test_roster_payload_uses_stable_ids_without_nz_account(self):
        connection = self.connection()
        first, refs = roster_request(connection); second, _ = roster_request(connection)
        self.assertEqual(first, second)
        self.assertTrue(refs); self.assertNotIn('nz_student_id', first['students'][0])
        self.assertFalse(Grade.objects.exists())

    def test_ignored_new_pupil_is_absent_but_previous_mapping_is_sent_inactive(self):
        connection = self.connection()
        self.journal.ignored_students.add(self.student)
        payload, refs = roster_request(connection)
        self.assertNotIn(self.student, refs.values())
        GradeStudentMapping.objects.create(connection=connection, student=self.student, external_id='41')
        payload, refs = roster_request(connection)
        ref = next(ref for ref, student in refs.items() if student.pk == self.student.pk)
        self.assertFalse(next(row for row in payload['students'] if row['id'] == ref)['active'])
        self.assertTrue(next(row for row in payload['students'] if row['id'] == ref)['ignored'])
        self.journal.ignored_students.remove(self.student)
        restored, _ = roster_request(connection)
        self.assertTrue(next(row for row in restored['students'] if row['id'] == ref)['active'])

    def test_ignore_updates_connected_rosters_and_offline_keeps_local_decision(self):
        from apps.journals.services.external_grades import ExternalAPIError
        connection = self.connection(mappings=True)
        url = reverse('portal:toggle_student_sync_ignore', args=[self.journal.pk, self.student.pk])
        with patch('apps.journals.services.roster_transfer.send_journal_roster', return_value={'counts': {}}) as send:
            response = self.client.post(url, HTTP_ACCEPT='application/json')
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].pk, connection.pk)
        self.assertTrue(response.json()['is_ignored'])
        with patch('apps.journals.services.roster_transfer.send_journal_roster', side_effect=ExternalAPIError('Джерело недоступне')):
            response = self.client.post(url, HTTP_ACCEPT='application/json')
        self.assertFalse(response.json()['is_ignored'])
        self.assertTrue(response.json()['warnings'])
        self.assertFalse(self.journal.ignored_students.filter(pk=self.student.pk).exists())

    def test_first_modern_connection_is_saved_before_context_and_transfer(self):
        old = self.connection()
        cache = old.roster_cache
        old.delete()
        url = reverse('portal:connect_source', args=[self.journal.pk])
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=cache), \
                patch('apps.portal.views_connection_setup.send_journal_roster', return_value={}) as transfer:
            response = self.client.post(url, {'action': 'connect', 'source': 'schooltest',
                'site_url': 'http://192.168.10.25:8000', 'api_key': 'synthetic-key'})
        self.assertEqual(response.status_code, 302, (str(response.context['setup_form'].errors) + str(list(response.context['messages']))) if response.context else '')
        connection = GradeAPIConnection.objects.get()
        self.assertEqual(connection.source_class_id, '7')
        self.assertEqual(connection.source_subject_id, '3')
        self.assertEqual(transfer.call_args.args[0].pk, connection.pk)
        self.assertContains(self.client.get(response.url), 'value="send_roster"')
        self.assertFalse(Grade.objects.exists())

    def test_grade_sync_retries_roster_before_reading_grades_on_both_routes(self):
        self.connection(mappings=True)
        self.journal.ignored_students.add(self.student)
        connection = GradeAPIConnection.objects.get()
        paths = [reverse('portal:pull_source_grades', args=[self.journal.pk, 'schooltest']),
                 reverse('portal:pull_external_grades', args=[self.journal.pk, connection.pk])]
        events = []
        for path in paths:
            events.clear()
            with patch('apps.portal.views_integrations.send_journal_roster',
                       side_effect=lambda *args: events.append('roster') or {'counts': {}}), \
                 patch('apps.portal.views_integrations.fetch_grade_payload',
                       side_effect=lambda *args: events.append('grades') or []):
                response = self.client.post(path, {'date_from': self.year.start_date, 'date_to': self.year.end_date})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(events, ['roster', 'grades'])

    def test_failed_roster_retry_is_visible_and_does_not_block_grade_read(self):
        from apps.journals.services.external_grades import ExternalAPIError
        connection = self.connection(mappings=True)
        path = reverse('portal:pull_external_grades', args=[self.journal.pk, connection.pk])
        with patch('apps.portal.views_integrations.send_journal_roster', side_effect=ExternalAPIError('Синтетично недоступний')), \
             patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[]) as grades:
            response = self.client.post(path, {'date_from': self.year.start_date, 'date_to': self.year.end_date}, follow=True)
        grades.assert_called_once()
        self.assertContains(response, 'список учнів не синхронізовано')

    def test_ignore_acknowledgement_rejects_active_missing_or_omitted_mapping(self):
        from apps.journals.services.external_grades import ExternalAPIError
        connection = self.connection(mappings=True)
        connection.roster_cache['capabilities'].append('journal_roster_ignore')
        connection.save()
        self.journal.ignored_students.add(self.student)
        payload, refs = roster_request(connection)
        ref = next(ref for ref, student in refs.items() if student == self.student)
        mapping = {'journal_id': ref, 'local_id': 41, 'name': 'Вигаданий Учень'}
        for rows in [[{**mapping, 'active': True}], [mapping], []]:
            report = {'schema_version': 2, 'source': 'schooltest', 'instance_id': connection.roster_cache['instance_id'],
                      'class_id': 7, 'subject_id': 3, 'counts': {}, 'mappings': rows}
            response = MagicMock(); response.status_code = 200
            response.iter_content.return_value = [json.dumps(report).encode()]
            response.__enter__.return_value = response
            with patch('apps.journals.services.roster_transfer.validate_api_url'), \
                 patch('apps.journals.services.roster_transfer.requests.Session') as session:
                session.return_value.__enter__.return_value.post.return_value = response
                with self.assertRaises(ExternalAPIError):
                    send_journal_roster(connection, self.teacher)
            connection.refresh_from_db()
            self.assertIsNone(connection.roster_sent_at)
            self.assertEqual(len(connection.roster_cache['students']), 1)

    def test_roster_transfer_saves_returned_native_ids_and_never_marks(self):
        connection = self.connection()
        payload, refs = roster_request(connection)
        report = {'schema_version': 2, 'source': 'schooltest', 'instance_id': connection.roster_cache['instance_id'],
            'class_id': 7, 'subject_id': 3, 'counts': {'created': len(refs), 'updated': 0, 'unchanged': 0, 'needs_review': 0, 'conflicts': 0},
            'mappings': [{'journal_id': ref, 'local_id': 100+i, 'name': s.get_full_name_ukr()} for i,(ref,s) in enumerate(refs.items())],
            'needs_review': [], 'conflicts': []}
        response = MagicMock(); response.status_code=200; response.iter_content.return_value=[json.dumps(report).encode()]
        response.__enter__.return_value=response
        with patch('apps.journals.services.roster_transfer.validate_api_url'), patch('apps.journals.services.roster_transfer.requests.Session') as session:
            session.return_value.__enter__.return_value.post.return_value=response
            send_journal_roster(connection, self.teacher)
        connection.refresh_from_db(); self.assertTrue(connection.enabled)
        self.assertEqual(connection.student_mappings.count(), len(refs)); self.assertFalse(Grade.objects.exists())

    def test_failed_nz_export_has_visible_persistent_report_without_network_call(self):
        with patch('apps.portal.views_nz.NZService.from_account') as remote:
            response = self.client.post(reverse('portal:nz_sync_execute', args=[self.journal.pk]))
        self.assertEqual(response.status_code, 400); remote.assert_not_called()
        log = NZSyncLog.objects.get(); self.assertEqual(log.status, NZSyncLog.Status.FAILED)
        self.assertIn('history_url', response.json())
        self.assertContains(self.client.get(reverse('portal:nz_sync_history', args=[self.journal.pk])), log.error_message)
