"""Owner boundaries and credential-safe setup with synthetic service responses."""
import uuid
from unittest.mock import patch
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from apps.accounts.models import User
from apps.journals.models import ServiceConnection, ServiceEndpoint, NetworkProfile, GradeAPIConnection
from apps.journals.services.network_connections import ConnectionFailure
from tests import test_journal_workflows as fixtures
from apps.portal.forms_connections import ServiceSetupForm


@override_settings(LOCAL_SERVER_PROFILE_ENABLED=False)
class ConnectionsUITests(TestCase):
    setUp = fixtures.JournalWorkflowsTests.setUp

    def info(self, source='schooltest'):
        return {'source': source, 'instance_id': '11111111-1111-4111-8111-111111111111',
            'version': '1.7.0', 'permissions': {'export': True, 'roster_import': True}}

    def connect(self, **changes):
        return self.client.post(reverse('portal:connect_service'), {'source': 'schooltest',
            'address': '10.44.1.13:8080', 'api_key': 'synthetic-ui-source-key', **changes})

    def service(self):
        with patch('apps.portal.views_connections.inspect_address', return_value=self.info()):
            response = self.connect()
        self.assertEqual(response.status_code, 302)
        return ServiceConnection.objects.get()

    def test_wizard_keeps_single_identity_and_logical_endpoint_on_repeated_setup(self):
        service = self.service()
        with patch('apps.portal.views_connections.inspect_address', return_value=self.info()):
            self.connect()
        self.assertEqual(ServiceConnection.objects.count(), 1)
        self.assertEqual(service.endpoints.filter(profile__host='10.44.1.13').count(), 1)
        self.assertEqual(service.token, 'synthetic-ui-source-key')
        self.assertFalse(GradeAPIConnection.objects.exists())

    def test_second_source_reuses_one_host_profile_and_preserves_separate_ports(self):
        self.service()
        with patch('apps.portal.views_connections.inspect_address', return_value=self.info('schoolwork')):
            response = self.connect(source='schoolwork', address='10.44.1.13:8000')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NetworkProfile.objects.filter(host='10.44.1.13').count(), 1)
        profile = NetworkProfile.objects.get(host='10.44.1.13')
        self.assertEqual(set(profile.endpoints.values_list('connection__source', 'port_override')),
            {('schooltest', 8080), ('schoolwork', 8000)})

    def test_failed_probe_does_not_store_key_or_echo_it_in_html(self):
        with patch('apps.portal.views_connections.inspect_address', side_effect=ConnectionFailure('Сайт недоступний')):
            response = self.connect()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'synthetic-ui-source-key')
        self.assertFalse(ServiceConnection.objects.exists())

    def test_cards_and_check_json_never_include_raw_token_or_fingerprint(self):
        service = self.service()
        page = self.client.get(reverse('portal:connections'))
        self.assertContains(page, 'Ключ прийнято')
        self.assertIn('no-store', page['Cache-Control'])
        with patch('apps.portal.views_connections.check_service', return_value=self.info()):
            response = self.client.post(reverse('portal:connection_check', args=[service.pk]))
        for response in (page, response):
            self.assertNotContains(response, service.token)
            self.assertNotContains(response, service.token_fingerprint)

    def test_foreign_service_profile_endpoint_and_binding_are_blocked(self):
        service = self.service()
        profile = service.endpoints.first().profile
        other = User.objects.create_user(username='foreign-central-ui', school=self.school, role=User.Role.TEACHER)
        self.client.force_login(other)
        with patch('apps.portal.views_connections.resolve_endpoint') as probe:
            for name, args in [('connection_edit', [service.pk]), ('connection_profile_edit', [profile.pk]),
                    ('connection_endpoint_edit', [service.pk, service.endpoints.first().pk])]:
                self.assertEqual(self.client.get(reverse('portal:'+name, args=args)).status_code, 404)
            self.assertEqual(self.client.post(reverse('portal:connection_check', args=[service.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse('portal:connection_bind', args=[self.journal.pk]),
                {'service': service.pk}).status_code, 403)
        probe.assert_not_called()

    def test_check_requires_post_and_csrf(self):
        service = self.service()
        url = reverse('portal:connection_check', args=[service.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True); client.force_login(self.teacher)
        self.assertEqual(client.post(url).status_code, 403)

    def test_new_key_rejection_keeps_previous_key_and_bindings(self):
        service = self.service()
        binding = GradeAPIConnection.objects.create(journal=self.journal, service_connection=service,
            source=service.source, url='http://10.44.1.13:8080/api/v2/journal/grades/', api_key=service.token)
        with patch('apps.portal.views_connections.resolve_endpoint', return_value=(service.endpoints.first(), {})), \
                patch('apps.portal.views_connections.inspect_address', side_effect=ConnectionFailure('Ключ не прийнято', 'unauthorized', 401)):
            response = self.client.post(reverse('portal:connection_edit', args=[service.pk]),
                {'name': service.name, 'api_key': 'synthetic-new-rejected', 'enabled': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'synthetic-new-rejected')
        service.refresh_from_db(); binding.refresh_from_db()
        self.assertEqual(service.token, binding.api_key)
        self.assertEqual(service.token, 'synthetic-ui-source-key')

    def test_network_switch_invalidates_green_state_and_preserves_key(self):
        service = self.service()
        response = self.client.post(reverse('portal:connections_network'), {'profile': ''})
        self.assertEqual(response.status_code, 302)
        service.refresh_from_db()
        self.assertIsNone(service.last_checked_at)
        self.assertEqual(service.token, 'synthetic-ui-source-key')

    def test_old_key_menu_redirects_to_central_page(self):
        self.assertRedirects(self.client.get(reverse('portal:teacher_api_keys')), reverse('portal:connections'))

    def test_host_and_full_url_ports_are_distinct_and_legacy_code_supported(self):
        for address, expected in [('schoolserver.local', ':8080'), ('https://schoolserver.local', ''),
                ('https://schoolserver.local:9443/tests', ':9443/tests')]:
            form = ServiceSetupForm({'source': 'schooltest', 'api_key': 'synthetic-form-key', 'address': address},
                owner=self.teacher)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['site_url'], 'https://schoolserver.local'+expected if address.startswith('https')
                else 'http://schoolserver.local'+expected)

