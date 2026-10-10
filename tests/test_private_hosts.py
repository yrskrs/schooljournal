from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from config.middleware import PrivateNetworkHostMiddleware


@override_settings(ALLOWED_HOSTS=['journal.example'], ALLOW_PRIVATE_NETWORK_HOSTS=True)
class PrivateHostTests(SimpleTestCase):
    def check(self, host):
        request = RequestFactory().get('/', HTTP_HOST=host)
        return PrivateNetworkHostMiddleware(lambda request: HttpResponse(request.get_host()))(request)

    def test_changing_private_ip_and_port_work_without_global_host_changes(self):
        for host in ('192.168.8.25:8085', '10.35.2.7:8085', '172.16.4.11:8185', '[fd00::21]:8085'):
            self.assertEqual(self.check(host).content.decode(), host)
        self.assertEqual(settings.ALLOWED_HOSTS, ['journal.example'])

    def test_public_addresses_domains_and_malformed_hosts_are_rejected(self):
        for host in ('evil.example:8085', '8.8.8.8:8085', '192.168.8.25.evil.example',
                     '192.168.8.25:65536', 'user@192.168.8.25', '169.254.169.254:8085'):
            with self.assertRaises(DisallowedHost): self.check(host)
        self.assertEqual(self.check('journal.example:8085').status_code, 200)

    @override_settings(ALLOW_PRIVATE_NETWORK_HOSTS=False)
    def test_lan_support_can_be_disabled_for_explicit_hosts(self):
        with self.assertRaises(DisallowedHost): self.check('192.168.8.25:8085')
