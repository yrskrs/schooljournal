"""Synthetic endpoint failover, identity, privacy and deadline regressions."""
import json
import time
import uuid
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from apps.accounts.models import User
from apps.journals.models import (GradeAPIConnection, NetworkProfile, NetworkSelection,
    ServiceConnection, ServiceEndpoint)
from apps.journals.services import network_connections as net
from apps.journals.services.connection_setup import exchange_with_fallback
from apps.journals.services.network_profiles import ensure_automatic_profiles
from tests import test_journal_workflows as fixtures


class NetworkProfileTests(TestCase):
    def setUp(self):
        fixtures.JournalWorkflowsTests.setUp(self)
        self.service = ServiceConnection(owner=self.teacher, source='schooltest', name='Синтетичні тести',
            instance_id=uuid.UUID('11111111-1111-4111-8111-111111111111'), identity_key='synthetic-service')
        self.service.set_token('synthetic-source-key'); self.service.save()
        self.home = NetworkProfile.objects.create(owner=self.teacher, name='Дім', host='10.44.1.13', schooltest_port=8000)
        self.school = NetworkProfile.objects.create(owner=self.teacher, name='Школа', host='10.44.0.13', schooltest_port=8000)
        self.home_endpoint = ServiceEndpoint.objects.create(connection=self.service, profile=self.home)
        self.school_endpoint = ServiceEndpoint.objects.create(connection=self.service, profile=self.school)
        self.service.last_endpoint = self.home_endpoint; self.service.save(update_fields=['last_endpoint'])
        self.selection = NetworkSelection.objects.create(owner=self.teacher, last_profile=self.home)
        self.binding = GradeAPIConnection.objects.create(journal=self.journal, name='Тести', source='schooltest',
            contract_version=2, service_connection=self.service, source_class_id='7', source_subject_id='2',
            url='http://10.44.1.13:8000/api/v2/journal/grades/', api_key='synthetic-legacy-key')

    def ping(self, **changes):
        return {'source': 'schooltest', 'instance_id': str(self.service.instance_id), 'version': '1.7.0', **changes}

    def info(self, **changes):
        return {**self.ping(), 'schema_version': 2,
            'permissions': {'export': True, 'roster_import': True}, **changes}

    def test_auto_finds_school_and_remembers_it_without_changing_binding_url(self):
        def probe(url, token=None, **kwargs):
            if '10.44.1.13' in url: raise net.ConnectionFailure('Недоступний синтетичний дім')
            self.assertIsNone(token)
            return 200, self.ping()
        original = self.binding.url
        with patch.object(net, 'get_small_json', side_effect=probe):
            net.prepare_binding(self.binding)
        self.assertEqual(net.transport_credentials(self.binding),
            ('http://10.44.0.13:8000/api/v2/journal/grades/', 'synthetic-source-key'))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.url, original)
        self.selection.refresh_from_db(); self.service.refresh_from_db()
        self.assertEqual(self.selection.last_profile_id, self.school.pk)
        self.assertEqual(self.service.last_endpoint_id, self.school_endpoint.pk)
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())) as probe:
            net.prepare_binding(self.binding)
        self.assertIn('10.44.0.13', probe.call_args.args[0])

    def test_manual_home_does_not_silently_switch_to_school(self):
        self.selection.selected_profile = self.home; self.selection.save()
        with patch.object(net, 'get_small_json', side_effect=net.ConnectionFailure('Дім недоступний')) as probe:
            with self.assertRaises(net.ConnectionFailure): net.prepare_binding(self.binding)
        self.assertEqual(probe.call_count, 1)
        self.assertIn('10.44.1.13', probe.call_args.args[0])

    def test_wrong_instance_or_source_never_receives_token_or_exchange(self):
        self.selection.selected_profile = self.home; self.selection.save()
        for payload in (self.ping(instance_id=str(uuid.uuid4())), self.ping(source='schoolwork')):
            callback = Mock()
            with patch.object(net, 'get_small_json', return_value=(200, payload)) as probe:
                with self.assertRaises(net.ConnectionFailure):
                    exchange_with_fallback([self.binding], callback)
            callback.assert_not_called()
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(probe.call_args.args, ('http://10.44.1.13:8000/api/v2/ping',))

    def test_actual_request_failure_tries_another_verified_endpoint(self):
        attempted = []
        def exchange(binding):
            url, token = net.transport_credentials(binding)
            attempted.append(url)
            if '10.44.1.13' in url: raise net.api_failure(503, 'schooltest')
            return {'synthetic': 'result'}
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())):
            selected, result = exchange_with_fallback([self.binding], exchange)
        self.assertEqual(selected.pk, self.binding.pk)
        self.assertEqual(result, {'synthetic': 'result'})
        self.assertEqual(len(attempted), 2)
        self.assertIn('10.44.0.13', attempted[-1])

    def test_authentication_error_is_not_retried_across_networks(self):
        callback = Mock(side_effect=net.api_failure(401, 'schooltest'))
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())):
            with self.assertRaises(net.ConnectionFailure): exchange_with_fallback([self.binding], callback)
        self.assertEqual(callback.call_count, 1)

    def test_key_status_distinguishes_401_and_403_without_secret(self):
        for status, phrase in ((401, 'Ключ не прийнято'), (403, 'немає права')):
            with patch.object(net, 'get_small_json', side_effect=[(200, self.ping()), (status, None)]):
                with self.assertRaises(net.ConnectionFailure) as error: net.check_service(self.service)
            self.assertIn(phrase, str(error.exception))
            self.assertNotIn('synthetic-source-key', str(error.exception))
            self.service.refresh_from_db()
            self.assertEqual(self.service.token_status, 'unauthorized' if status == 401 else 'forbidden')
        self.assertIn('списків', str(net.api_failure(403, 'schooltest', 'roster_import')))

    def test_check_reports_valid_key_with_limited_rights(self):
        with patch.object(net, 'get_small_json', side_effect=[(200, self.ping()),
                (200, self.info(permissions={'export': True, 'roster_import': False}))]) as probe:
            result = net.check_service(self.service)
        self.assertFalse(result['permissions']['roster_import'])
        self.assertEqual(probe.call_args.args[1], 'synthetic-source-key')
        self.service.refresh_from_db(); self.assertEqual(self.service.token_status, 'accepted')

    def test_auth_response_changed_identity_or_invalid_rights_is_rejected(self):
        for data in (self.info(instance_id=str(uuid.uuid4())), self.info(permissions={'export': 'yes', 'roster_import': True})):
            with patch.object(net, 'get_small_json', side_effect=[(200, self.ping()), (200, data)]):
                with self.assertRaises(net.ConnectionFailure): net.check_service(self.service)
            self.service.refresh_from_db()
            self.assertNotEqual(self.service.token_status, 'accepted')
            self.assertTrue(self.service.last_error)

    @override_settings(LOCAL_SERVER_PROFILE_ENABLED=False)
    def test_current_journal_host_profile_handles_network_change(self):
        profile = ensure_automatic_profiles(self.teacher)[0]
        self.selection.selected_profile = profile; self.selection.save()
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())):
            for host in ('10.44.1.13', '10.44.0.13', 'schoolserver.local'):
                token = net.current_journal_host.set(host)
                try:
                    net.prepare_binding(self.binding)
                    self.assertEqual(net.transport_credentials(self.binding)[0],
                        f'http://{host}:8000/api/v2/journal/grades/')
                finally: net.current_journal_host.reset(token)
        profile.refresh_from_db(); self.assertEqual(profile.host, '')

    @override_settings(LOCAL_SERVER_PROFILE_ENABLED=True)
    def test_automatic_profiles_are_idempotent_and_keep_disabled_settings(self):
        with patch.object(net, 'get_small_json') as probe:
            first = ensure_automatic_profiles(self.teacher)
            first[-1].enabled = False; first[-1].save()
            second = ensure_automatic_profiles(self.teacher)
        self.assertEqual([p.pk for p in first], [p.pk for p in second])
        self.assertFalse(second[-1].enabled)
        self.assertEqual(ServiceEndpoint.objects.count(), 4)
        probe.assert_not_called()

    @override_settings(LOCAL_SERVER_PROFILE_ENABLED=False)
    def test_automatic_host_keeps_custom_source_port_and_path(self):
        self.home_endpoint.port_override = 9080
        self.home_endpoint.path_prefix = '/synthetic-tests'
        self.home_endpoint.save()
        profile = ensure_automatic_profiles(self.teacher)[0]
        endpoint = self.service.endpoints.get(profile=profile)
        self.assertEqual(endpoint.api_url('grades', '10.44.0.13'),
            'http://10.44.0.13:9080/synthetic-tests/api/v2/journal/grades/')

    def test_foreign_owner_binding_is_blocked_before_network(self):
        other = User.objects.create_user(username='foreign-connection-owner')
        self.service.owner = other; self.service.save()
        with patch.object(net, 'get_small_json') as probe:
            with self.assertRaises(net.ConnectionFailure): net.prepare_binding(self.binding)
        probe.assert_not_called()

    def test_disabled_binding_and_endpoint_are_not_used(self):
        self.binding.enabled = False
        with patch.object(net, 'get_small_json') as probe:
            with self.assertRaises(net.ConnectionFailure): net.prepare_binding(self.binding)
        probe.assert_not_called()
        self.binding.enabled = True
        self.home_endpoint.enabled = False; self.home_endpoint.save()
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())) as probe:
            net.prepare_binding(self.binding)
        self.assertIn('10.44.0.13', probe.call_args.args[0])

    def test_roster_transfer_uses_new_address_and_keeps_original_binding(self):
        from apps.journals.models import GradeStudentMapping, Grade
        from apps.journals.services.roster_transfer import roster_request, send_journal_roster
        self.binding.roster_cache = {'schema_version': 2, 'source': 'schooltest',
            'instance_id': str(self.service.instance_id), 'capabilities': ['journal_roster_import'],
            'classes': [], 'subjects': [], 'students': []}
        self.binding.save(update_fields=['roster_cache'])
        for native_id, student in ((41, self.student), (42, self.empty_student)):
            GradeStudentMapping.objects.create(connection=self.binding, external_id=str(native_id), student=student)
        payload, refs = roster_request(self.binding)
        report = {'schema_version': 2, 'source': 'schooltest', 'instance_id': str(self.service.instance_id),
            'class_id': 7, 'subject_id': 2, 'class_name': self.group.name,
            'counts': {'created': 0, 'updated': 0, 'unchanged': 2, 'needs_review': 0, 'conflicts': 0},
            'needs_review': [], 'conflicts': [], 'mappings': [
                {'journal_id': row['id'], 'local_id': int(row['local_id']),
                    'name': refs[row['id']].get_full_name_ukr(), 'active': True}
                for row in payload['students']]}
        self.selection.selected_profile = self.school; self.selection.save()
        original_url = self.binding.url
        with patch.object(net, 'get_small_json', return_value=(200, self.ping())), \
                patch('apps.journals.services.roster_transfer.validate_api_url'), \
                patch('apps.journals.services.roster_transfer.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            response = session.post.return_value.__enter__.return_value
            response.status_code = 200; response.iter_content.return_value = [json.dumps(report).encode()]
            selected, result = exchange_with_fallback([self.binding], lambda binding: send_journal_roster(binding, self.teacher))
        self.assertEqual(session.post.call_args.args[0], 'http://10.44.0.13:8000/api/v2/journal/roster/')
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-source-key')
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.url, original_url)
        self.assertEqual(selected.pk, self.binding.pk)
        self.assertEqual(self.binding.student_mappings.count(), 2)
        self.assertFalse(Grade.objects.exists())

    def test_ping_is_anonymous_small_and_stable_without_roster_scan(self):
        self.client.logout()
        with patch('school_sync.django_backend.Store.all', side_effect=AssertionError('No roster scan')):
            first = self.client.get(reverse('portal:service_ping'))
            second = self.client.get(reverse('portal:service_ping'))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(set(first.json()), {'source', 'instance_id', 'version'})
        self.assertEqual(first.json(), second.json())
        self.assertIn('no-cache', first['Cache-Control'])


class ProbeBoundaryTests(TestCase):
    def test_deadline_includes_a_slow_worker(self):
        def slow(*args):
            time.sleep(.1)
            return 200, {}
        start = time.monotonic()
        with patch.object(net, '_http_json', side_effect=slow):
            with self.assertRaises(net.ConnectionFailure):
                net.get_small_json('http://synthetic.invalid', timeout=.01)
        self.assertLess(time.monotonic() - start, .09)

    def test_ssrf_is_rejected_before_socket_request(self):
        for url in ('http://127.0.0.1:8000/api/v2/ping', 'http://169.254.169.254/api/v2/ping',
                'http://user:synthetic-key@10.44.0.13/api/v2/ping', 'http://[::ffff:127.0.0.1]/api/v2/ping'):
            with patch.object(net.requests, 'Session') as session:
                with self.assertRaises(net.ConnectionFailure) as error: net._http_json(url, None, 1.5)
            session.assert_not_called()
            self.assertNotIn('synthetic-key', str(error.exception))

    def test_probe_does_not_send_token_follow_redirects_or_use_proxy_environment(self):
        payload = {'source': 'schooltest', 'instance_id': str(uuid.uuid4()), 'version': '1.7.0'}
        with patch.object(net, 'validate_api_url'), patch.object(net.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            response = session.get.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.return_value = [json.dumps(payload).encode()]
            self.assertEqual(net._http_json('http://schoolserver.local:8000/api/v2/ping', None, 1.5), (200, payload))
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('Authorization', session.get.call_args.kwargs['headers'])

    def test_probe_response_is_bounded(self):
        with patch.object(net, 'validate_api_url'), patch.object(net.requests, 'Session') as factory:
            response = factory.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.status_code = 200; response.iter_content.return_value = [b'x' * 8193]
            with self.assertRaises(net.ConnectionFailure): net._http_json('http://10.44.0.13:8000/api/v2/ping', None, 1.5)
