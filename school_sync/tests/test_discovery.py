import unittest
import uuid
from school_sync.core import SyncError
from school_sync.discovery import ping_payload, validate_ping


class DiscoveryTests(unittest.TestCase):
    def test_ping_has_only_public_metadata_and_stable_identity(self):
        identity = str(uuid.uuid4())
        payload = ping_payload('schooltest', identity, '1.7.0')
        self.assertEqual(set(payload), {'source', 'instance_id', 'version'})
        self.assertEqual(validate_ping(payload, 'schooltest', identity), payload)

    def test_another_source_or_instance_is_rejected(self):
        identity = str(uuid.uuid4())
        data = ping_payload('schooltest', identity, '1.7.0')
        for source, instance in [('schoolwork', identity), ('schooltest', str(uuid.uuid4()))]:
            with self.assertRaises(SyncError): validate_ping(data, source, instance)

    def test_invalid_metadata_is_not_identity(self):
        for data in ({}, [], {'source': 'schooltest', 'instance_id': 'bad', 'version': '1'},
                {'source': 'schooltest', 'instance_id': str(uuid.uuid4()), 'version': {'bad': 'version'}}):
            with self.assertRaises(SyncError): validate_ping(data, 'schooltest')
