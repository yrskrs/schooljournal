import json
from unittest.mock import patch, MagicMock, ANY

from django.test import TestCase, Client, override_settings
from django.conf import settings
from django.urls import reverse
from apps.accounts.models import User
from apps.curriculum.models import Student, ClassEnrollment, StudentClass
from apps.journals.models import Journal, GradeAPIConnection, GradeStudentMapping, Grade, ExternalGrade, AuditLog, StudentAPIGrant
from apps.journals.services.connection_setup import roster_version, validate_roster, source_urls
from apps.journals.services.external_grades import ExternalAPIError, import_grade_records
from tests import test_journal_workflows as workflows


class ConnectionSetupTests(TestCase):
    def test_new_connection_form_reuses_verified_instance_after_ip_change(self):
        self.payload['instance_id'] = '11111111-1111-4111-8111-111111111111'
        connection = self.connect()
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            response = self.client.post(self.url, {'action': 'connect', 'source': 'schooltest',
                'site_url': 'http://10.25.4.21:8000', 'api_key': 'synthetic-new-key'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(GradeAPIConnection.objects.count(), 1)
        connection.refresh_from_db()
        self.assertEqual(connection.url, 'http://10.25.4.21:8000/api/v2/journal/grades/')

    def test_refresh_cannot_replace_a_previously_confirmed_server(self):
        self.payload['instance_id'] = '11111111-1111-4111-8111-111111111111'
        connection = self.connect(); self.context(connection); connection.refresh_from_db(); self.confirm(connection)
        other = {**self.payload, 'instance_id': '22222222-2222-4222-8222-222222222222'}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=other):
            response = self.client.post(self.url, {'action': 'refresh', 'connection_id': connection.pk})
        self.assertContains(response, 'За цією адресою інший сервіс')
        connection.refresh_from_db()
        self.assertEqual(connection.roster_cache, self.payload)
        self.assertEqual(connection.student_mappings.get().student, self.student)
        self.assertTrue(connection.enabled)

    @override_settings(ALLOWED_HOSTS=['testserver'], CSRF_TRUSTED_ORIGINS=[], ALLOW_PRIVATE_NETWORK_HOSTS=True)
    def test_new_lan_ip_accepts_own_origin_csrf_and_rejects_foreign_origin(self):
        client = Client(enforce_csrf_checks=True, HTTP_HOST='10.35.2.7:8085')
        client.force_login(self.teacher)
        self.assertEqual(client.get(reverse('portal:connect_service')).status_code, 200)
        data = {'action': 'connect', 'source': 'schooltest', 'site_url': 'http://192.168.8.25:8000',
            'api_key': 'synthetic-ip-key', 'csrfmiddlewaretoken': client.cookies[settings.CSRF_COOKIE_NAME].value}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload) as remote:
            self.assertEqual(client.post(self.url, data, HTTP_ORIGIN='http://10.35.2.7:8085').status_code, 302)
            remote.assert_called_once()
            remote.reset_mock()
            self.assertEqual(client.post(self.url, data, HTTP_ORIGIN='http://evil.example').status_code, 403)
            remote.assert_not_called()

    def test_plain_key_and_ip_address_are_visible_and_used_directly(self):
        self.assertRedirects(self.client.get(self.url), reverse('portal:connections_matching') + f'?journal={self.journal.pk}')
        page = self.client.get(reverse('portal:connect_service'))
        self.assertContains(page, 'Адреса, IP або hostname')
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload) as remote:
            response = self.client.post(self.url, {'action': 'connect', 'source': 'schooltest',
                'site_url': 'http://10.25.4.21:8000', 'api_key': '  synthetic-ip-key  '})
        self.assertEqual(response.status_code, 302)
        remote.assert_called_once_with('http://10.25.4.21:8000/api/v2/journal/grades/', 'synthetic-ip-key', 'schooltest')

    def test_old_json_in_key_field_respects_explicit_new_ip(self):
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload) as remote:
            response = self.client.post(self.url, {'action': 'connect', 'source': 'schooltest',
                'site_url': 'http://172.20.5.18:8000', 'api_key': json.dumps({'source': 'schooltest',
                    'site_url': 'http://schooltest-api:8000', 'api_key': 'synthetic-old-key'})})
        self.assertEqual(response.status_code, 302)
        remote.assert_called_once_with('http://172.20.5.18:8000/api/v2/journal/grades/', 'synthetic-old-key', 'schooltest')

    def test_ip_change_keeps_connection_pupil_mapping_and_manual_grade(self):
        self.payload['instance_id'] = '11111111-1111-4111-8111-111111111111'
        connection = self.connect(); self.context(connection); connection.refresh_from_db(); self.confirm(connection)
        Grade.objects.create(lesson=self.l1, student=self.student, value='11', created_by=self.teacher)
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            response = self.client.post(self.url, {'action': 'connect', 'connection_id': connection.pk,
                'source': 'schooltest', 'site_url': 'http://10.25.4.21:8000', 'api_key': 'synthetic-new-key'})
        self.assertEqual(response.status_code, 302)
        connection.refresh_from_db()
        self.assertEqual(connection.url, 'http://10.25.4.21:8000/api/v2/journal/grades/')
        self.assertEqual(GradeAPIConnection.objects.count(), 1)
        self.assertEqual(connection.student_mappings.get().student, self.student)
        self.assertTrue(connection.enabled)
        self.assertEqual(Grade.objects.get().value, '11')

    def test_other_server_at_new_ip_cannot_take_over_existing_history(self):
        self.payload['instance_id'] = '11111111-1111-4111-8111-111111111111'
        connection = self.connect()
        other = {**self.payload, 'instance_id': '22222222-2222-4222-8222-222222222222'}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=other):
            response = self.client.post(self.url, {'action': 'connect', 'connection_id': connection.pk,
                'source': 'schooltest', 'site_url': 'http://10.25.4.21:8000', 'api_key': 'synthetic-other-key'})
        self.assertContains(response, 'За цією адресою інший сервіс')
        previous_url = connection.url
        connection.refresh_from_db()
        self.assertEqual(connection.url, previous_url)
        self.assertEqual(connection.api_key, 'synthetic-private-export-key')

    def test_valid_legacy_session_migrates_without_second_login(self):
        original = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.client.cookies['sessionid'] = original
        del self.client.cookies[settings.SESSION_COOKIE_NAME]
        response = self.client.get(reverse('portal:connections'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user, self.teacher)
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME].value, original)
        self.client.cookies['sessionid'] = 'foreign-service-session-that-is-not-ours'
        self.assertEqual(self.client.get(reverse('portal:connections')).status_code, 200)
        outsider = Client(); outsider.cookies['sessionid'] = 'foreign-service-session-that-is-not-ours'
        self.assertEqual(outsider.get(self.url).status_code, 302)

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

    def test_connection_key_name_and_display_properties(self):
        payload_with_caps = {**self.payload, 'capabilities': ['journal_roster_import']}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=payload_with_caps), \
             patch('apps.portal.views_connection_setup.send_journal_roster', return_value={'counts': {'created': 1, 'updated': 0, 'needs_review': 0, 'conflicts': 0}}):
            resp = self.client.post(self.url, {
                'action': 'connect',
                'source': 'schooltest',
                'key_name': 'Мій унікальний тестовий ключ',
                'site_url': 'http://10.25.4.21:8000',
                'api_key': 'abc123secretkey',
            })
            self.assertEqual(resp.status_code, 302)
        conn = GradeAPIConnection.objects.get(journal=self.journal)
        self.assertEqual(conn.name, 'Мій унікальний тестовий ключ')
        self.assertEqual(conn.masked_key, '••••••')
        self.assertEqual(conn.capabilities_description, 'може отримувати та передавати дані')
        self.assertIn('Мій унікальний тестовий ключ', conn.key_display_name)
        self.assertIn('може отримувати та передавати дані', conn.key_display_name)

    def test_apply_to_all_classes_connects_all_teacher_journals(self):
        group2 = StudentClass.objects.create(school=self.school, academic_year=self.year, name='8-Б клас')
        journal2 = Journal.objects.create(school=self.school, academic_year=self.year, student_class=group2, subject=self.subject, teacher=self.teacher)

        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload), \
             patch('apps.portal.views_connection_setup.send_journal_roster', return_value={'counts': {'created': 1, 'updated': 0, 'needs_review': 0, 'conflicts': 0}}):
            resp = self.client.post(self.url, {
                'action': 'connect',
                'source': 'schooltest',
                'key_name': 'Ключ для всіх',
                'site_url': 'http://10.25.4.21:8000',
                'api_key': 'sharedkey12345',
                'apply_to_all_classes': '1',
            })
            self.assertEqual(resp.status_code, 302)

        # Both journals should have connection
        conn1 = GradeAPIConnection.objects.filter(journal=self.journal).first()
        conn2 = GradeAPIConnection.objects.filter(journal=journal2).first()
        self.assertIsNotNone(conn1)
        self.assertIsNotNone(conn2)
        self.assertEqual(conn1.api_key, 'sharedkey12345')
        self.assertEqual(conn2.api_key, 'sharedkey12345')
        self.assertEqual(conn2.name, 'Ключ для всіх')

    def test_delete_source_connection(self):
        conn = self.connect()
        conn_pk = conn.pk
        resp = self.client.post(reverse('portal:delete_source_connection', kwargs={
            'journal_id': self.journal.pk,
            'connection_id': conn_pk
        }))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn_pk).exists())
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.Action.DELETE, target_model='GradeAPIConnection', target_id=str(conn_pk)
        ).exists())

    def test_delete_connection_from_connect_source_preserves_grades(self):
        conn = self.connect()
        self.context(conn); conn.refresh_from_db(); self.confirm(conn)
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='12', created_by=self.teacher)
        conn_pk = conn.pk
        resp = self.client.post(self.url, {
            'action': 'delete_connection',
            'connection_id': conn_pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn_pk).exists())
        # Ensure Grade was NOT destroyed
        grade.refresh_from_db()
        self.assertEqual(grade.value, '12')

    def test_delete_saved_key_single_and_all(self):
        group2 = StudentClass.objects.create(school=self.school, academic_year=self.year, name='9-А клас')
        journal2 = Journal.objects.create(school=self.school, academic_year=self.year, student_class=group2, subject=self.subject, teacher=self.teacher)

        conn1 = self.connect()
        conn2 = GradeAPIConnection.objects.create(
            journal=journal2, source=conn1.source, url=conn1.url, api_key=conn1.api_key, name=conn1.name, contract_version=2
        )

        url2 = reverse('portal:connect_source', kwargs={'journal_id': journal2.pk})
        # Delete only single saved key (conn1) from available list
        resp = self.client.post(url2, {
            'action': 'delete_saved_key',
            'reuse_connection': conn1.pk,
            'delete_all_matching': '0',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn1.pk).exists())
        self.assertTrue(GradeAPIConnection.objects.filter(pk=conn2.pk).exists())

        # Now test delete_all_matching
        conn3 = GradeAPIConnection.objects.create(
            journal=self.journal, source=conn2.source, url=conn2.url, api_key=conn2.api_key, name=conn2.name, contract_version=2
        )
        resp2 = self.client.post(self.url, {
            'action': 'delete_saved_key',
            'reuse_connection': conn2.pk,
            'delete_all_matching': '1',
        })
        self.assertEqual(resp2.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn2.pk).exists())
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn3.pk).exists())

    def test_teacher_api_keys_view_and_bulk_deletion(self):
        group2 = StudentClass.objects.create(school=self.school, academic_year=self.year, name='10-А клас')
        journal2 = Journal.objects.create(school=self.school, academic_year=self.year, student_class=group2, subject=self.subject, teacher=self.teacher)

        conn1 = self.connect()
        conn2 = GradeAPIConnection.objects.create(
            journal=journal2, source=conn1.source, url=conn1.url, api_key=conn1.api_key, name='Загальний ключ', contract_version=2
        )
        StudentAPIGrant.objects.create(journal=self.journal, enabled=True, fields=['id', 'name'])

        keys_url = reverse('portal:teacher_api_keys')
        self.assertRedirects(self.client.get(keys_url), reverse('portal:connections'))
        resp = self.client.get(reverse('portal:connections'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Підключення')
        self.assertNotContains(resp, conn1.api_key)
        self.assertContains(resp, self.group.name)
        self.assertContains(resp, '10-А клас')

        # Test single delete from api_keys page
        del_url = reverse('portal:delete_api_key_group')
        resp_single = self.client.post(del_url, {
            'connection_id': conn2.pk,
            'delete_all': '0',
        })
        self.assertEqual(resp_single.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn2.pk).exists())
        self.assertTrue(GradeAPIConnection.objects.filter(pk=conn1.pk).exists())

        # Test bulk delete
        resp_bulk = self.client.post(del_url, {
            'connection_id': conn1.pk,
            'delete_all': '1',
        })
        self.assertEqual(resp_bulk.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn1.pk).exists())

    def test_delete_connection_from_journal_integrations(self):
        conn = self.connect()
        conn_pk = conn.pk
        int_url = reverse('portal:journal_integrations', kwargs={'journal_id': self.journal.pk})
        resp = self.client.post(int_url, {
            'action': 'delete_connection',
            'connection_id': conn_pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn_pk).exists())
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.Action.DELETE, target_model='GradeAPIConnection', target_id=str(conn_pk)
        ).exists())

    def test_test_source_connection_success_and_failure(self):
        conn = self.connect()
        test_url = reverse('portal:test_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        # Success JSON AJAX test
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            resp = self.client.post(test_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['success'])
            self.assertEqual(data['status'], 'ok')
            self.assertIn('успішний', data['message'].lower())

        # Failure JSON AJAX test with ExternalAPIError
        with patch('apps.portal.views_connection_setup.fetch_source_roster', side_effect=ExternalAPIError('Сервер не відповідає (Connection refused)')):
            resp = self.client.post(test_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            self.assertFalse(data['success'])
            self.assertEqual(data['status'], 'error')
            self.assertIn('refused', data['message'].lower())
            self.assertTrue(len(data['hint']) > 0)

        # Standard POST redirect test
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            resp = self.client.post(test_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'успішний')

    def test_toggle_source_connection(self):
        conn = self.connect()
        self.context(conn)
        conn.refresh_from_db()
        self.confirm(conn)
        conn.refresh_from_db()
        self.assertTrue(conn.enabled)
        toggle_url = reverse('portal:toggle_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        # Toggle off
        resp = self.client.post(toggle_url, follow=True)
        self.assertEqual(resp.status_code, 200)
        conn.refresh_from_db()
        self.assertFalse(conn.enabled)
        self.assertContains(resp, 'тимчасово вимкнено')

        # Toggle back on
        resp2 = self.client.post(toggle_url, follow=True)
        self.assertEqual(resp2.status_code, 200)
        conn.refresh_from_db()
        self.assertTrue(conn.enabled)
        self.assertContains(resp2, 'увімкнено')

    def test_edit_source_connection(self):
        conn = self.connect()
        edit_url = reverse('portal:edit_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        # Edit connection URL and name
        post_data = {
            'name': 'Оновлений SchoolTest',
            'site_url': 'http://10.25.4.99:8080',
            'api_key': 'brand-new-secret-key',
            'enabled': 'on',
        }
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=self.payload):
            resp = self.client.post(edit_url, post_data, follow=True)
            self.assertEqual(resp.status_code, 200)
            conn.refresh_from_db()
            self.assertEqual(conn.name, 'Оновлений SchoolTest')
            self.assertEqual(conn.url, 'http://10.25.4.99:8080/api/v2/journal/grades/')
            self.assertEqual(conn.api_key, 'brand-new-secret-key')
            self.assertTrue(conn.enabled)
            self.assertContains(resp, 'Налаштування підключення')
            self.assertContains(resp, 'збережено')

    def test_quick_send_roster(self):
        conn = self.connect()
        self.context(conn)
        conn.refresh_from_db()
        self.confirm(conn)

        quick_url = reverse('portal:quick_send_roster', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})
        mock_result = {'counts': {'created': 2, 'updated': 0, 'ignored': 0, 'needs_review': 0, 'conflicts': 0}}
        with patch('apps.portal.views_connection_setup.send_journal_roster', return_value=mock_result) as mock_send:
            resp = self.client.post(quick_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            mock_send.assert_called_once()
            self.assertContains(resp, 'Список учнів успішно передано')
            self.assertContains(resp, 'додано 2')

    def test_journal_detail_and_integrations_page_render_api_bars_and_tabs(self):
        conn = self.connect()
        detail_url = reverse('portal:journal_detail', kwargs={'journal_id': self.journal.pk})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'journal-api-bar')
        self.assertContains(resp, 'api-status-chip')
        self.assertContains(resp, 'Центр API')

        int_url = reverse('portal:journal_integrations', kwargs={'journal_id': self.journal.pk})
        resp_int = self.client.get(int_url)
        self.assertEqual(resp_int.status_code, 200)
        self.assertContains(resp_int, 'Центр керування API та синхронізації')
        self.assertContains(resp_int, 'Підключені API')
        self.assertContains(resp_int, 'Синхронізація списків')
        self.assertContains(resp_int, 'Діагностика та помилки')

    def test_server_grouping_and_location_editing(self):
        conn = self.connect()
        edit_url = reverse('portal:edit_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})
        resp = self.client.post(edit_url, {
            'name': 'Домашній сервер тестів',
            'site_url': 'http://192.168.1.12:8080',
            'api_key': '',
            'enabled': 'on',
            'server_group': 'Сервер тестування',
            'location_tag': '🏠 Дім',
            'is_default_for_group': 'on',
            'source_class_id': '',
            'source_subject_id': '',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        conn.refresh_from_db()
        self.assertEqual(conn.server_group, 'Сервер тестування')
        self.assertEqual(conn.location_tag, '🏠 Дім')
        self.assertTrue(conn.is_default_for_group)
        self.assertEqual(conn.group_display, 'Сервер тестування')
        self.assertEqual(conn.location_display, '🏠 Дім')
        self.assertIn('192.168.1.12:8080', conn.host_display)

    def test_quick_group_connections_view(self):
        conn1 = self.connect()
        conn2 = GradeAPIConnection.objects.create(
            journal=self.journal, source='schooltest', contract_version=2,
            name='Шкільні тести', url='http://192.168.0.50:8080/api/v2/journal/grades/',
            api_key='key-2', enabled=True
        )
        group_url = reverse('portal:quick_group_connections', kwargs={'journal_id': self.journal.pk})
        resp = self.client.post(group_url, {
            'group_name': 'Сервер тестування',
            'connection_ids': [conn1.pk, conn2.pk],
            'active_connection_id': conn1.pk,
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        conn1.refresh_from_db()
        conn2.refresh_from_db()
        self.assertEqual(conn1.server_group, 'Сервер тестування')
        self.assertEqual(conn2.server_group, 'Сервер тестування')
        self.assertTrue(conn1.is_default_for_group)
        self.assertFalse(conn2.is_default_for_group)

    def test_set_active_connection_view(self):
        conn = self.connect()
        conn.server_group = 'Сервер тестування'
        conn.save()
        set_active_url = reverse('portal:set_active_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})
        resp = self.client.post(set_active_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        conn.refresh_from_db()
        self.assertTrue(conn.is_default_for_group)

    def test_pull_source_grades_with_grouped_failover(self):
        # conn1 is offline (e.g. school IP when at home)
        conn1 = GradeAPIConnection.objects.create(
            journal=self.journal, source='schooltest', contract_version=2,
            name='Шкільні тести', url='http://192.168.0.50:8080/api/v2/journal/grades/',
            api_key='key-school', enabled=True, server_group='Сервер тестування',
            location_tag='🏫 Школа', source_class_id='1', source_subject_id='1'
        )
        # conn2 is online (e.g. home IP)
        conn2 = GradeAPIConnection.objects.create(
            journal=self.journal, source='schooltest', contract_version=2,
            name='Домашні тести', url='http://192.168.1.12:8080/api/v2/journal/grades/',
            api_key='key-home', enabled=True, server_group='Сервер тестування',
            location_tag='🏠 Дім', is_default_for_group=True,
            source_class_id='1', source_subject_id='1'
        )

        pull_url = reverse('portal:pull_source_grades', kwargs={'journal_id': self.journal.pk, 'source': 'schooltest'})

        # Mock reachability: conn1 offline, conn2 online
        def fake_probe(conn, timeout=1.5):
            if conn.pk == conn1.pk:
                return False, 'Тайм-аут'
            return True, 'В мережі'

        mock_payload = []
        with patch('apps.portal.views_integrations.find_online_connection_in_group', return_value=(conn2, {conn1.pk: (False, 'Тайм-аут'), conn2.pk: (True, 'В мережі')})), \
             patch('apps.portal.views_integrations._reconcile_source_roster') as mock_reconcile, \
             patch('apps.portal.views_integrations.fetch_grade_payload', return_value=mock_payload), \
             patch('apps.portal.views_integrations.import_grade_records') as mock_import:

            mock_log = MagicMock()
            mock_log.counts = {'added': 2, 'updated': 1, 'unchanged': 0, 'ignored': 0, 'conflicts': 0, 'errors': 0}
            mock_import.return_value = mock_log

            resp = self.client.post(pull_url, {
                'date_from': self.journal.academic_year.start_date.isoformat(),
                'date_to': self.journal.academic_year.end_date.isoformat(),
            }, follow=True)

            self.assertEqual(resp.status_code, 200)
            # Reconcile was called for conn2 (online), not conn1 (offline)
            mock_reconcile.assert_called_once_with(ANY, conn2)
            # Success message is displayed for conn2 / group
            self.assertContains(resp, 'Сервер тестування')
            self.assertContains(resp, 'додано 2')
            # NO false error for conn1!
            self.assertNotContains(resp, 'Джерело недоступне')

    def test_toast_container_rendered_in_base_template(self):
        resp = self.client.get(reverse('portal:teacher_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'toast-container')
        self.assertContains(resp, 'window.showToast')

    def test_multiple_source_pupils_mapped_to_same_student(self):
        payload = dict(self.payload)
        payload['students'] = [
            {'id': '41', 'class_id': '7', 'name': self.student.get_full_name_ukr()},
            {'id': '42', 'class_id': '7', 'name': self.student.get_full_name_ukr() + ' (дублікат)'},
        ]
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=payload):
            code = json.dumps({'source': 'schooltest', 'site_url': 'http://schooltest-api:8000', 'api_key': 'synthetic-private-export-key'})
            self.client.post(self.url, {'action': 'connect', 'connection_code': code})
            conn = GradeAPIConnection.objects.get(journal=self.journal)
            self.context(conn)
            conn.refresh_from_db()

            mapping_data = {
                'action': 'students',
                'connection_id': conn.pk,
                'version': roster_version(conn),
                'pupil_41': str(self.student.pk),
                'pupil_42': str(self.student.pk),
                'confirm': 'on',
            }
            resp = self.client.post(self.url, mapping_data, follow=True)
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, 'Різних учнів джерела не можна пов’язати')

            # Check both mappings exist in DB pointing to the same student
            mappings = list(conn.student_mappings.filter(student=self.student))
            self.assertEqual(len(mappings), 2)
            mapped_external_ids = {m.external_id for m in mappings}
            self.assertEqual(mapped_external_ids, {'41', '42'})

    def test_test_source_connection_auto_detects_source(self):
        # Create connection as schoolwork
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, source='schoolwork', contract_version=2,
            name='тести дім дім', url='http://192.168.1.13:8000/api/v2/journal/grades/',
            api_key='secret-key', enabled=True
        )
        test_url = reverse('portal:test_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        # Server actually returns source='schooltest'
        test_payload = dict(self.payload)
        test_payload['source'] = 'schooltest'

        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=test_payload):
            resp = self.client.post(test_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['success'])
            self.assertIn('автоматично оновлено на «Сайт тестування»', data['message'])

            conn.refresh_from_db()
            self.assertEqual(conn.source, 'schooltest')

    def test_test_source_connection_hint_does_not_false_trigger_on_pidklyuchennya(self):
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, source='schoolwork', contract_version=2,
            name='тест', url='http://192.168.1.13:8000/api/v2/journal/grades/',
            api_key='secret-key', enabled=True
        )
        test_url = reverse('portal:test_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        err_text = 'Сайт не підтримує це підключення. Оновіть його з GitHub; збережені дані журналу залишаються.'
        with patch('apps.portal.views_connection_setup.fetch_source_roster', side_effect=ExternalAPIError(err_text)):
            resp = self.client.post(test_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            # Hint should NOT falsely say the site rejected the API key!
            self.assertNotIn('відхилив ключ', data['message'])

    def test_edit_source_connection_change_source(self):
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, source='schoolwork', contract_version=2,
            name='Сервер', url='http://192.168.1.13:8000/api/v2/journal/grades/',
            api_key='secret-key', enabled=True
        )
        edit_url = reverse('portal:edit_source_connection', kwargs={'journal_id': self.journal.pk, 'connection_id': conn.pk})

        resp = self.client.post(edit_url, {
            'name': 'Сервер',
            'source': 'schooltest',
            'site_url': 'http://192.168.1.13:8000',
            'enabled': 'on',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        conn.refresh_from_db()
        self.assertEqual(conn.source, 'schooltest')

    def test_delete_connection_with_external_grades_and_inbox_results_preserves_grades(self):
        """
        Видалення підключення, з якого імпортувалися оцінки (ExternalGrade)
        та є записи ExtraStudent/APIInboxResult, має відбуватися успішно без 500,
        а виставлені оцінки журналу (Grade) залишаються збереженими.
        """
        from apps.journals.models import ExternalGrade, APIInboxResult, ExtraStudent, Grade, Lesson
        conn = GradeAPIConnection.objects.create(
            journal=self.journal, source='schooltest', contract_version=2,
            name='Домашні тести', url='http://192.168.1.13:8080/api/v2/journal/grades/',
            api_key='secret-key', enabled=True
        )

        lesson = Lesson.objects.create(
            journal=self.journal, date='2026-10-15', topic='Тестова тема'
        )
        ext_grade = ExternalGrade.objects.create(
            connection=conn, external_id='ext-123', student=self.student,
            lesson=lesson, value='11', status=ExternalGrade.Status.APPLIED
        )
        grade = Grade.objects.create(
            student=self.student, lesson=lesson, value='11', import_record=ext_grade
        )

        extra = ExtraStudent.objects.create(
            connection=conn, external_key='extra-1', name='Окремий Учень'
        )
        APIInboxResult.objects.create(
            connection=conn, external_id='inbox-1', extra_student=extra,
            payload={'score': 10}
        )

        grades_before = Grade.objects.filter(lesson__journal=self.journal).count()
        self.assertEqual(grades_before, 1)

        resp = self.client.post(
            reverse('portal:delete_source_connection', args=[self.journal.pk, conn.pk]),
            follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(GradeAPIConnection.objects.filter(pk=conn.pk).exists())

        grades_after = Grade.objects.filter(lesson__journal=self.journal).count()
        self.assertEqual(grades_after, 1)
        grade.refresh_from_db()
        self.assertEqual(grade.value, '11')
        self.assertIsNone(grade.import_record)




