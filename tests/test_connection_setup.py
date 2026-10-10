import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from apps.accounts.models import User
from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import GradeAPIConnection, GradeStudentMapping, Grade, ExternalGrade
from apps.journals.services.connection_setup import roster_version, validate_roster, source_urls
from apps.journals.services.external_grades import ExternalAPIError, import_grade_records
from tests import test_journal_workflows as workflows


class ConnectionSetupTests(TestCase):
    def setUp(self):
        workflows.JournalWorkflowsTests.setUp(self)
        self.url = reverse('portal:connect_source', kwargs={'journal_id': self.journal.pk})
        self.payload = validate_roster({'schema_version': 2, 'source': 'schooltest',
            'classes': [{'id': 7, 'name': self.group.name}], 'subjects': [{'id': 3, 'name': self.subject.name}],
            'students': [{'id': 41, 'class_id': 7, 'name': self.student.get_full_name_ukr()}]}, 'schooltest')

    def connect(self):
        code = json.dumps({'source': 'schooltest', 'site_url': 'http://schooltest-api:8000', 'api_key': 'synthetic-private-export-key'})
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            response = self.client.post(self.url, {'action': 'connect', 'connection_code': code})
        self.assertEqual(response.status_code, 302)
        return GradeAPIConnection.objects.get(journal=self.journal)

    def test_previous_release_can_insert_connections_after_additive_migration(self):
        connection = self.connect()
        from django.db import connection as database
        # Reproduce an older ORM INSERT that does not mention the new columns.
        columns = [field.column for field in GradeAPIConnection._meta.concrete_fields
                   if not field.primary_key and field.name not in ('roster_cache', 'roster_checked_at')]
        names = ', '.join(database.ops.quote_name(column) for column in columns)
        table = database.ops.quote_name(GradeAPIConnection._meta.db_table)
        with database.cursor() as cursor:
            cursor.execute(f'INSERT INTO {table} ({names}) SELECT {names} FROM {table} WHERE id = %s', [connection.pk])
        added = GradeAPIConnection.objects.latest('pk')
        self.assertEqual(added.roster_cache, {})
        self.assertIsNone(added.roster_checked_at)

    def context(self, connection):
        return self.client.post(self.url, {'action': 'context', 'connection_id': connection.pk,
            'version': roster_version(connection), 'source_class_id': '7', 'source_subject_id': '3'})

    def confirm(self, connection, **extra):
        return self.client.post(self.url, {'action': 'students', 'connection_id': connection.pk,
            'version': roster_version(connection), 'confirm': 'on', 'pupil_41': str(self.student.pk), **extra})

    def test_code_fetches_persistent_names_without_importing_grades(self):
        connection = self.connect()
        self.assertFalse(connection.enabled)
        self.assertEqual(connection.roster_cache, self.payload)
        self.assertIsNotNone(connection.roster_checked_at)
        page = self.client.get(self.url, {'connection': connection.pk})
        self.assertContains(page, self.group.name)
        self.assertNotContains(page, connection.api_key)
        self.assertFalse(Grade.objects.exists())
        self.assertEqual(page['Cache-Control'], 'no-store')

    def test_repeat_connect_reuses_connection_and_existing_history(self):
        connection = self.connect()
        self.context(connection); connection.refresh_from_db(); self.confirm(connection)
        self.connect()
        self.assertEqual(GradeAPIConnection.objects.count(), 1)
        self.assertEqual(GradeStudentMapping.objects.count(), 1)
        self.assertFalse(Grade.objects.exists())

    def test_get_and_suggestions_do_not_commit_identity(self):
        connection = self.connect()
        connection.enabled = True
        connection.save(update_fields=['enabled'])
        self.context(connection); connection.refresh_from_db()
        self.assertFalse(connection.enabled)
        page = self.client.get(self.url, {'connection': connection.pk})
        self.assertEqual(page.context['mapping_form'].fields['pupil_41'].initial, str(self.student.pk))
        self.assertFalse(connection.student_mappings.exists())
        self.confirm(connection, confirm='')
        self.assertFalse(connection.student_mappings.exists())
        self.confirm(connection)
        self.assertEqual(connection.student_mappings.get().student, self.student)
        connection.refresh_from_db(); self.assertTrue(connection.enabled)
        self.assertFalse(Grade.objects.exists())

    def test_twins_are_not_preselected_or_merged_by_name(self):
        twin = Student.objects.create(school=self.school, first_name=self.student.first_name, last_name=self.student.last_name)
        ClassEnrollment.objects.create(student=twin, student_class=self.group, is_active=True)
        connection = self.connect(); self.context(connection); connection.refresh_from_db()
        page = self.client.get(self.url, {'connection': connection.pk})
        self.assertEqual(page.context['mapping_form'].fields['pupil_41'].initial, '')
        self.confirm(connection, pupil_41=str(twin.pk))
        self.assertEqual(connection.student_mappings.get().student, twin)

    def test_wrong_class_and_foreign_student_choices_are_rejected(self):
        connection = self.connect()
        response = self.client.post(self.url, {'action': 'context', 'connection_id': connection.pk,
            'version': roster_version(connection), 'source_class_id': '999', 'source_subject_id': '3'})
        self.assertEqual(response.status_code, 200)
        connection.refresh_from_db(); self.assertEqual(connection.source_class_id, '')
        self.context(connection); connection.refresh_from_db()
        outsider = Student.objects.create(school=self.school, first_name='Інший', last_name='Вигаданий')
        self.confirm(connection, pupil_41=str(outsider.pk))
        self.assertFalse(connection.student_mappings.exists())

    def test_teacher_may_explicitly_create_new_student_without_merging_existing_name(self):
        connection = self.connect(); self.context(connection); connection.refresh_from_db()
        self.confirm(connection, pupil_41='new')
        pupil = connection.student_mappings.get().student
        self.assertNotEqual(pupil.pk, self.student.pk)
        self.assertEqual(pupil.get_full_name_ukr(), self.student.get_full_name_ukr())
        self.assertTrue(pupil.enrollments.filter(student_class=self.group, is_active=True).exists())

    def test_network_failure_keeps_cache_mappings_manual_grades_and_key(self):
        connection = self.connect(); self.context(connection); connection.refresh_from_db(); self.confirm(connection)
        Grade.objects.create(lesson=self.l1, student=self.student, value='11', created_by=self.teacher)
        with patch('apps.portal.views_connection_setup.fetch_source_roster', side_effect=ExternalAPIError('Недоступний сайт')):
            self.client.post(self.url, {'action': 'refresh', 'connection_id': connection.pk})
        connection.refresh_from_db()
        self.assertEqual(connection.roster_cache, self.payload)
        self.assertEqual(connection.api_key, 'synthetic-private-export-key')
        self.assertEqual(Grade.objects.get().value, '11')
        self.assertEqual(connection.student_mappings.count(), 1)

    def test_stale_context_or_mapping_decision_is_rejected(self):
        connection = self.connect(); before = roster_version(connection)
        self.context(connection); connection.refresh_from_db()
        response = self.client.post(self.url, {'action': 'students', 'connection_id': connection.pk,
            'version': before, 'confirm': 'on', 'pupil_41': str(self.student.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(connection.student_mappings.exists())

    def test_scope_and_existing_import_cannot_be_reassigned(self):
        connection = self.connect(); self.context(connection); connection.refresh_from_db(); self.confirm(connection)
        row = dict(source='schooltest', id='synthetic-import', student_id=41, class_id=7, subject_id=3, work_id=9,
            title='Вигаданий тест', points=8, max_points=12, value='8', grading_scale=12, date=self.l1.date.isoformat(),
            status='final', updated_at='2026-10-10T12:00:00+03:00', is_ai=False, approved_by_teacher=True)
        connection.refresh_from_db()
        import_grade_records(connection, [row], self.teacher, self.year.start_date, self.year.end_date)
        response = self.confirm(connection, pupil_41=str(self.empty_student.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(connection.student_mappings.get().student, self.student)
        self.assertEqual(Grade.objects.get().value, '8')

    def test_access_denied_before_source_request(self):
        other = User.objects.create_user(username='other-setup', role=User.Role.TEACHER, school=self.school)
        self.client.force_login(other)
        with patch('apps.portal.views_connection_setup.fetch_source_roster') as remote:
            self.assertEqual(self.client.post(self.url, {'action': 'connect'}).status_code, 403)
            remote.assert_not_called()

    def test_old_roster_contract_empty_response_and_invalid_records(self):
        self.assertEqual(source_urls('http://schooltest-api:8000/api/v2/journal/grades/')[1], 'http://schooltest-api:8000/api/v2/journal/roster/')
        empty = {'schema_version': 2, 'source': 'schooltest', 'classes': [], 'subjects': [], 'students': []}
        self.assertEqual(validate_roster(empty, 'schooltest')['classes'], [])
        for changes in ({'schema_version': 1}, {'source': 'schoolwork'}, {'classes': None},
            {'classes': [{'id': True, 'name': 'Клас'}]}, {'students': [{'id': 41, 'class_id': 999, 'name': 'Вигаданий Учень'}]}):
            with self.assertRaises(ExternalAPIError): validate_roster({**self.payload, **changes}, 'schooltest')
