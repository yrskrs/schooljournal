import copy
import unittest
import uuid
from school_sync.core import Engine, SyncError
from school_sync.journal_roster import import_students, signature
from school_sync.tests.test_protocol import Store, Native


class JournalRosterTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(Store(), Native())
        self.ref = str(uuid.uuid4())
        self.payload = {'schema_version': 2, 'journal_instance_id': str(uuid.uuid4()),
            'class': {'id': str(uuid.uuid4()), 'name': '7-А', 'grade_level': 7, 'letter': 'А'},
            'students': [{'id': self.ref, 'last_name': 'Синтетичний', 'first_name': 'Тарас', 'middle_name': ''}]}

    def send(self, payload=None, allowed=None):
        allowed = allowed if allowed is not None else set(self.engine.native.rows['class'])
        return import_students(self.engine, self.engine.store, payload or self.payload, 'teacher', allowed)

    def test_copy_repeat_and_empty_keep_native_ids(self):
        first = self.send(); second = self.send()
        self.assertEqual(first['mappings'], second['mappings'])
        self.assertEqual(len(self.engine.native.rows['student']), 1)
        empty = {**self.payload, 'students': []}
        self.send(empty)
        self.assertEqual(len(self.engine.native.rows['student']), 1)

    def test_similar_and_identical_names_need_explicit_identity(self):
        group = self.engine.native.create('class', {'name': '7-А', 'grade_level': 7, 'letter': 'А', 'active': True})
        pupil = self.engine.native.create('student', {'name': 'Синтетичний Тарас', 'last_name': 'Синтетичний', 'first_name': 'Тарас', 'middle_name': '', 'native_class_id': group, 'active': True})
        self.payload['source_class_id'] = group
        report = self.send()
        self.assertEqual(report['counts']['needs_review'], 1)
        self.assertFalse(report['mappings'])
        self.assertEqual(len(self.engine.native.rows['student']), 1)
        self.payload['students'][0]['local_id'] = pupil
        self.assertEqual(self.send()['mappings'][0]['local_id'], pupil)

    def test_typos_are_suggestions_and_do_not_rename_pupil(self):
        self.test_similar_and_identical_names_need_explicit_identity()
        row = self.engine.native.rows['student'][1]
        row.update(name='Синтетичний Т.', first_name='Т.')
        report = self.send()
        self.assertEqual(report['counts']['conflicts'], 1)
        self.assertEqual(row['first_name'], 'Т.')

    def test_remote_correction_updates_without_changing_id(self):
        first = self.send()
        self.payload['students'][0]['first_name'] = 'Тарасик'
        report = self.send()
        self.assertEqual(report['counts']['updated'], 1)
        self.assertEqual(first['mappings'][0]['local_id'], report['mappings'][0]['local_id'])

    def test_local_edit_conflict_and_stale_decision(self):
        self.send()
        row = self.engine.native.rows['student'][1]
        row.update(name='Синтетичний Місцевий', first_name='Місцевий')
        conflict = self.send()['conflicts'][0]
        row.update(name='Синтетичний ЩеНовіший', first_name='ЩеНовіший')
        self.payload['resolutions'] = {self.ref: {'choice': 'journal', 'version': conflict['version']}}
        self.assertEqual(self.send()['counts']['conflicts'], 1)
        self.assertEqual(row['first_name'], 'ЩеНовіший')
        self.payload['resolutions'][self.ref]['version'] = signature(row)
        self.assertEqual(self.send()['counts']['updated'], 1)
        self.assertEqual(self.engine.native.read('student', 1)['first_name'], 'Тарас')

    def test_keep_local_decision_survives_repeat(self):
        self.send()
        row = self.engine.native.rows['student'][1]
        row.update(name='Синтетичний Місцевий', first_name='Місцевий')
        self.payload['resolutions'] = {self.ref: {'choice': 'local', 'version': signature(row)}}
        self.send(); self.payload.pop('resolutions')
        report = self.send()
        self.assertEqual(report['counts']['conflicts'], 0)
        self.assertEqual(self.engine.native.read('student', 1)['first_name'], 'Місцевий')

    def test_invalid_full_payload_rejected_before_creation(self):
        data = copy.deepcopy(self.payload)
        data['students'].append({'id': 'invalid-id', 'last_name': 'Другий', 'first_name': 'Учень'})
        with self.assertRaises(SyncError): self.send(data)
        self.assertFalse(self.engine.native.rows['class'])
        self.assertFalse(self.engine.native.rows['student'])

    def test_other_owner_class_is_rejected(self):
        self.send()
        with self.assertRaises(SyncError): self.send(allowed=set())
        self.assertEqual(len(self.engine.native.rows['student']), 1)

    def test_class_rename_keeps_native_id_and_old_label_for_history(self):
        first = self.send()
        self.payload['class'].update(name='8-Б', grade_level=8, letter='Б')
        second = self.send()
        self.assertEqual(first['class_id'], second['class_id'])
        self.assertEqual(self.engine.native.read('class', first['class_id'])['name'], '8-Б')
        from school_sync.journal_roster import historical_class_names
        self.assertIn('7-А', historical_class_names(self.engine.store, 'teacher', first['class_id'], '8-Б'))

    def test_conflicting_class_rename_needs_manual_choice(self):
        self.send()
        self.engine.native.rows['class'][1]['name'] = 'Місцева група'
        self.payload['class']['name'] = 'Група журналу'
        report = self.send()
        self.assertEqual(report['counts']['conflicts'], 1)
        self.assertEqual(self.engine.native.read('class', 1)['name'], 'Місцева група')


if __name__ == '__main__':
    unittest.main()
