import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from school_sync import network_inventory as inventory


class NetworkInventoryTests(unittest.TestCase):
    def test_restricted_interface_inspection_does_not_break_settings_page(self):
        import socket
        fake = SimpleNamespace(net_if_stats=lambda: (_ for _ in ()).throw(PermissionError()),
            net_if_addrs=lambda: {'eth0': [SimpleNamespace(family=socket.AF_INET, address='10.44.1.13')]})
        with patch.dict('sys.modules', psutil=fake):
            self.assertEqual(inventory.native_addresses(), ['10.44.1.13'])

    def test_only_unicast_nonlocal_addresses_are_advertised(self):
        for value in ('127.0.0.1', '::1', '169.254.1.1', '0.0.0.0', '224.0.0.1', 'not-an-ip'):
            self.assertIsNone(inventory.valid_address(value))
        self.assertEqual(inventory.valid_address('10.44.1.13'), '10.44.1.13')

    def test_ready_urls_preserve_current_address_and_lan_published_port(self):
        with patch.object(inventory, 'advertised_addresses', return_value=['10.44.1.13', '10.44.0.13']):
            rows = inventory.connection_addresses('http://10.44.0.13:9080', 8080)
        self.assertEqual(rows[0]['url'], 'http://10.44.0.13:9080')
        self.assertEqual([r['url'] for r in rows], ['http://10.44.0.13:9080', 'http://10.44.1.13:9080'])

    def test_atomic_inventory_contains_only_addresses_and_time(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'addresses.json'
            with patch.object(inventory, 'native_addresses', return_value=['10.44.1.13']):
                inventory.publish(path)
            data = json.loads(path.read_text())
            self.assertEqual(set(data), {'addresses', 'updated_at'})
            self.assertEqual(data['addresses'], ['10.44.1.13'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
