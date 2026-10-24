"""Synthetic migration and credential tests; no network or production database."""
import copy
import uuid

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.journals.models import (GradeAPIConnection, GradeStudentMapping, Grade,
    NetworkProfile, NetworkSelection, ServiceConnection, ServiceEndpoint)
from apps.journals.secret_fields import PREFIX
from apps.journals.services.connection_migration import migrate_legacy_connections
from tests import test_journal_workflows as fixtures


class CentralConnectionModelTests(TestCase):
    setUp = fixtures.JournalWorkflowsTests.setUp

    def binding(self, **kwargs):
        values = dict(journal=self.journal, name='Синтетичні тести', source='schooltest',
            contract_version=2, url='http://10.44.1.13:8000/api/v2/journal/grades/',
            api_key='synthetic-central-key', server_group='Синтетичний сервер', location_tag='Дім',
            source_class_id='7', source_subject_id='2',
            roster_cache={'instance_id': '11111111-1111-4111-8111-111111111111',
                'source': 'schooltest', 'schema_version': 2, 'classes': [], 'subjects': [], 'students': []})
        values.update(kwargs)
        return GradeAPIConnection.objects.create(**values)

    def raw(self, model, pk, field):
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT {field} FROM {model._meta.db_table} WHERE id = %s', [pk])
            return cursor.fetchone()[0]

    def test_old_and_central_keys_are_encrypted_and_masked(self):
        binding = self.binding(api_key='short')
        self.assertTrue(self.raw(GradeAPIConnection, binding.pk, 'api_key').startswith(PREFIX))
        binding.refresh_from_db()
        self.assertEqual(binding.api_key, 'short')
        self.assertNotIn('short', binding.masked_key)
        migrate_legacy_connections(apps)
        binding.refresh_from_db()
        service = binding.service_connection
        self.assertEqual(service.token, 'short')
        self.assertNotIn('short', self.raw(ServiceConnection, service.pk, 'token'))

    def test_migration_groups_verified_addresses_preserving_business_ids(self):
        first = self.binding()
        second = self.binding(url='http://10.44.0.13:8000/api/v2/journal/grades/', location_tag='Школа')
        mapping = GradeStudentMapping.objects.create(connection=first, student=self.student, external_id='41')
        grade = Grade.objects.create(student=self.student, lesson=self.l1, value='11', created_by=self.teacher)
        migrate_legacy_connections(apps)
        for binding in (first, second):
            binding.refresh_from_db()
            self.assertEqual(binding.source_class_id, '7')
        self.assertEqual(first.service_connection_id, second.service_connection_id)
        self.assertEqual(ServiceConnection.objects.count(), 1)
        self.assertEqual(NetworkProfile.objects.count(), 2)
        self.assertEqual(ServiceEndpoint.objects.count(), 2)
        self.assertEqual(GradeStudentMapping.objects.get(pk=mapping.pk).student_id, self.student.pk)
        self.assertEqual(Grade.objects.get(pk=grade.pk).value, '11')
        migrate_legacy_connections(apps)
        self.assertEqual(ServiceEndpoint.objects.count(), 2)
        self.assertEqual(ServiceConnection.objects.count(), 1)

    def test_same_label_different_instance_or_key_is_not_merged(self):
        self.binding()
        self.binding(roster_cache={'instance_id': str(uuid.uuid4())})
        self.binding(api_key='synthetic-other-key')
        migrate_legacy_connections(apps)
        self.assertEqual(ServiceConnection.objects.count(), 3)

    def test_disabled_legacy_address_is_not_enabled_by_migration(self):
        binding = self.binding(enabled=False)
        migrate_legacy_connections(apps)
        binding.refresh_from_db()
        self.assertFalse(binding.enabled)
        self.assertFalse(binding.service_connection.enabled)
        self.assertFalse(binding.service_connection.endpoints.get().enabled)

    def test_owner_scoping_keeps_same_source_and_key_separate(self):
        first = self.binding()
        other = User.objects.create_user(username='other_connections_teacher', school=self.school,
            role=User.Role.TEACHER)
        journal = copy.copy(self.journal)
        journal.pk = None
        journal._state.adding = True
        from apps.curriculum.models import Subject
        journal.subject = Subject.objects.create(school=self.school, name='Інший синтетичний предмет')
        journal.teacher = other
        journal.nz_journal_id = 'synthetic-other-journal'
        journal.save()
        second = self.binding(journal=journal)
        migrate_legacy_connections(apps)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertNotEqual(first.service_connection_id, second.service_connection_id)
        self.assertEqual(second.service_connection.owner_id, other.pk)

    def test_explicit_default_and_custom_prefix_survive_migration(self):
        binding = self.binding(url='https://schoolserver.local:8443/testing/api/v2/journal/grades/',
            is_default_for_group=True)
        migrate_legacy_connections(apps)
        endpoint = ServiceEndpoint.objects.select_related('connection', 'profile').get()
        self.assertEqual(endpoint.api_url('grades'), binding.url)
        self.assertEqual(endpoint.api_url('ping'), 'https://schoolserver.local:8443/testing/api/v2/ping')
        self.assertEqual(NetworkSelection.objects.get(owner=self.teacher).last_profile_id, endpoint.profile_id)

    def test_cross_owner_profile_and_selection_are_rejected(self):
        self.binding(); migrate_legacy_connections(apps)
        other = User.objects.create_user(username='other_profile_owner')
        profile = NetworkProfile.objects.create(owner=other, name='Інша мережа', host='10.44.0.13')
        endpoint = ServiceEndpoint(connection=ServiceConnection.objects.get(), profile=profile)
        with self.assertRaises(ValidationError): endpoint.full_clean()
        selection = NetworkSelection(owner=self.teacher, selected_profile=profile)
        with self.assertRaises(ValidationError): selection.full_clean()

    def test_invalid_hosts_are_rejected_but_lan_and_hostname_are_allowed(self):
        for host in ('localhost', '127.0.0.1', '169.254.169.254', 'user:pass@10.44.0.13', '10.44.0.13/path'):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                NetworkProfile(owner=self.teacher, name='Мережа', host=host).full_clean()
        for host in ('10.44.0.13', 'schoolserver.local', 'schooltest-api'):
            NetworkProfile(owner=self.teacher, name='Мережа', host=host).full_clean()

    def test_token_rotation_supports_secret_key_fallback_and_reencrypts(self):
        with override_settings(SECRET_KEY='synthetic-old-key'):
            binding = self.binding()
        with override_settings(SECRET_KEY='synthetic-new-key', SECRET_KEY_FALLBACKS=['synthetic-old-key']):
            binding.refresh_from_db()
            binding.save(update_fields=['api_key'])
        with override_settings(SECRET_KEY='synthetic-new-key', SECRET_KEY_FALLBACKS=[]):
            binding.refresh_from_db()
            self.assertEqual(binding.api_key, 'synthetic-central-key')

    def test_ciphertext_tampering_is_reported_without_secret(self):
        binding = self.binding()
        with connection.cursor() as cursor:
            cursor.execute('UPDATE journals_gradeapiconnection SET api_key = %s WHERE id = %s',
                [PREFIX + 'synthetic-tampered-value', binding.pk])
        with self.assertRaises(ValidationError) as error:
            binding.refresh_from_db()
        self.assertNotIn('synthetic-tampered-value', str(error.exception))


class LegacyConnectionMigrationTests(TransactionTestCase):
    def tearDown(self):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_real_historical_schema_migration_preserves_and_can_restore_token(self):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        old_target = [('journals', '0019_gradelessonassignment')]
        new_target = [('journals', '0022_migrate_legacy_service_connections')]
        executor.migrate(old_target)
        fixtures.JournalWorkflowsTests.setUp(self)
        old_apps = executor.loader.project_state(old_target).apps
        Binding = old_apps.get_model('journals', 'GradeAPIConnection')
        binding = Binding.objects.create(journal_id=self.journal.pk, name='Історичне підключення',
            source='schooltest', contract_version=2, source_class_id='7', source_subject_id='2',
            url='http://10.44.1.13:8000/api/v2/journal/grades/', api_key='synthetic-legacy-secret',
            roster_cache={'instance_id': '11111111-1111-4111-8111-111111111111'})
        GradeStudentMapping.objects.create(connection_id=binding.pk, external_id='41', student=self.student)
        grade = Grade.objects.create(student=self.student, lesson=self.l1, value='11', created_by=self.teacher)
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        migrated = GradeAPIConnection.objects.get(pk=binding.pk)
        self.assertEqual(migrated.service_connection.token, 'synthetic-legacy-secret')
        self.assertEqual(migrated.source_class_id, '7')
        self.assertEqual(migrated.student_mappings.get().student_id, self.student.pk)
        self.assertEqual(Grade.objects.get(pk=grade.pk).value, '11')
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_key FROM journals_gradeapiconnection WHERE id = %s', [binding.pk])
            self.assertTrue(cursor.fetchone()[0].startswith(PREFIX))
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_key FROM journals_gradeapiconnection WHERE id = %s', [binding.pk])
            self.assertEqual(cursor.fetchone()[0], 'synthetic-legacy-secret')
