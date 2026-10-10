"""Three equal replicas: offline edits, explicit IDs, conflict decisions."""
import copy
import unittest
import uuid
from school_sync.core import Engine, SyncError

class Store:
    def __init__(self): self.rows, self.meta = {}, {}
    def all(self): return copy.deepcopy(list(self.rows.values()))
    def get(self, ref): return copy.deepcopy(self.rows.get(ref))
    def put(self, row): self.rows[row['ref']] = copy.deepcopy(row)

class Native:
    def __init__(self): self.rows = {'class': {}, 'student': {}}
    def create(self, kind, payload):
        pk = max(self.rows[kind], default=0) + 1
        self.rows[kind][pk] = copy.deepcopy(payload)
        return pk
    def write(self, kind, pk, payload):
        payload=copy.deepcopy(payload); payload.pop('class_ref', None)
        self.rows[kind][pk] = payload
    def read(self, kind, pk): return copy.deepcopy(self.rows[kind].get(pk))
    def students(self, pk): return [sid for sid, s in self.rows['student'].items() if s['native_class_id']==pk]
    def collision(self, kind, payload): return any(r['name']==payload['name'] for r in self.rows[kind].values())

class ProtocolTests(unittest.TestCase):
    def site(self):
        store, native=Store(),Native()
        return Engine(store,native)
    def seed(self, engine, pupils=True):
        group=engine.native.create('class',dict(name='7-А',grade_level=7,letter='А',active=True))
        if pupils: engine.native.create('student',dict(name='Вигаданий Учень',last_name='Вигаданий',first_name='Учень',middle_name='',native_class_id=group,active=True))
        return group
    def connect(self, source, target):
        target.receive(source.snapshot())
        ref=source.native_record('class',1)['ref']
        target.resolve(ref,0,native_id=1)
        target.receive(source.snapshot())
        return ref
    def test_three_replicas_names_are_not_identity(self):
        a,b,c=[self.site() for i in range(3)]
        for engine in (a,b,c): self.seed(engine)
        a.share('class',1)
        for engine in (b,c):
            self.connect(a,engine)
            row=next(row for row in engine.store.all() if row['kind']=='student')
            self.assertIsNone(row['native_id'])
            self.assertEqual(len(engine.native.rows['student']),1)
            engine.resolve(row['ref'],0,native_id=1)
        for _ in range(2):
            a.receive(b.snapshot());b.receive(c.snapshot());c.receive(a.snapshot())
        self.assertEqual(a.snapshot()['records'],b.snapshot()['records'])
        self.assertEqual(b.snapshot()['records'],c.snapshot()['records'])
    def test_offline_conflict_manual_and_propagated(self):
        a,b=self.site(),self.site()
        self.seed(a);self.seed(b,False);a.share('class',1);self.connect(a,b)
        ref=a.native_record('student',1)['ref']
        for engine, first in ((a,'Перше'),(b,'Друге')):
            engine.native.rows['student'][1].update(name='Вигаданий '+first,first_name=first)
        a.receive(b.snapshot());b.receive(a.snapshot())
        self.assertEqual(a.store.get(ref)['status'],'conflict')
        a.resolve(ref,0)
        b.receive(a.snapshot());a.receive(b.snapshot())
        self.assertEqual(a.native.read('student',1),b.native.read('student',1))
        self.assertEqual(b.store.get(ref)['status'],'ready')
    def test_empty_and_bad_version_do_not_remove_records(self):
        a=self.site();self.seed(a);a.share('class',1);before=a.snapshot()
        a.receive(dict(schema_version=2,origin=str(uuid.uuid4()),records=[]))
        self.assertEqual(before,a.snapshot())
        with self.assertRaises(SyncError):a.receive(dict(schema_version=99,records=[]))
        self.assertEqual(before,a.snapshot())
    def test_withdrawal_keeps_native_id(self):
        a,b=self.site(),self.site();self.seed(a);self.seed(b,False);a.share('class',1);self.connect(a,b)
        a.native.rows['student'][1]['active']=False
        b.receive(a.snapshot())
        self.assertIn(1,b.native.rows['student']);self.assertFalse(b.native.read('student',1)['active'])
    def test_stale_review_cannot_overwrite_new_local_edit(self):
        a=self.site();self.seed(a);a.share('class',1)
        row=a.native_record('student',1)
        a.native.rows['student'][1].update(name='Вигаданий Нове',first_name='Нове')
        with self.assertRaises(SyncError):a.resolve(row['ref'],'local',expected=row)
        self.assertEqual(a.native.read('student',1)['first_name'],'Нове')
    def test_explicit_alias_preserves_two_native_histories(self):
        a,b=self.site(),self.site();self.seed(a);self.seed(b);a.share('class',1);b.share('class',1)
        old=b.native_record('class',1)['ref'];b.receive(a.snapshot())
        ref=a.native_record('class',1)['ref'];b.resolve(ref,0,native_id=1)
        a.receive(b.snapshot())
        self.assertEqual(a.canonical(old),ref)
        # Both pupil records exist until an explicit decision identifies them.
        pupil=a.native_record('student',1)['ref']
        b.resolve(pupil,0,native_id=1)
        a.receive(b.snapshot());b.receive(a.snapshot())
        self.assertEqual(len(a.native.rows['student']),1);self.assertEqual(len(b.native.rows['student']),1)
        self.assertEqual(a.native_record('student',1)['ref'],b.native_record('student',1)['ref'])
    def test_new_pupil_after_initial_mapping_is_copied(self):
        a,b=self.site(),self.site();self.seed(a,False);self.seed(b,False);a.share('class',1);self.connect(a,b)
        a.native.create('student',dict(name='Новий Учень',first_name='Учень',last_name='Новий',middle_name='',native_class_id=1,active=True))
        b.receive(a.snapshot())
        self.assertEqual(b.native.read('student',1)['name'],'Новий Учень')
    def test_newer_version_cannot_drop_another_unresolved_branch(self):
        a,b,c=[self.site() for i in range(3)];self.seed(a);a.share('class',1)
        for engine in (b,c):self.seed(engine,False);self.connect(a,engine)
        ref=a.native_record('student',1)['ref']
        for engine, first in ((a,'A'),(b,'B'),(c,'C')):engine.native.rows['student'][1].update(name='Вигаданий '+first,first_name=first)
        a.receive(b.snapshot());a.receive(c.snapshot())
        b.native.rows['student'][1].update(name='Вигаданий BB',first_name='BB');a.receive(b.snapshot())
        self.assertEqual(a.store.get(ref)['status'],'conflict');self.assertEqual(a.native.read('student',1)['first_name'],'A')
    def test_invalid_name_fields_and_duplicate_refs_rejected(self):
        a=self.site();self.seed(a);a.share('class',1);payload=a.snapshot()
        payload['records'] += [payload['records'][0]]
        with self.assertRaises(SyncError):self.site().receive(payload)
        payload=a.snapshot();payload['records'][1]['payload']['first_name']='Інше'
        with self.assertRaises(SyncError):self.site().receive(payload)
    def test_mapping_and_creation_are_mutually_exclusive(self):
        a=self.site();self.seed(a);a.share('class',1)
        with self.assertRaises(SyncError):a.resolve(a.native_record('class',1)['ref'],'local',native_id=1,create=True)

    def test_alias_cycle_and_cross_kind_mapping_are_rejected(self):
        a=self.site();self.seed(a);a.share('class',1)
        base=a.snapshot();student=next(r for r in base['records'] if r['kind']=='student');group=next(r for r in base['records'] if r['kind']=='class')
        clock=a.bump(group['clock']);alias=dict(ref=group['ref'],kind='alias',payload={'target':student['ref']},clock=clock)
        with self.assertRaises(SyncError):a.receive(dict(schema_version=2,origin=a.origin,records=[alias]))
        self.assertEqual(a.native_record('class',1)['ref'],group['ref'])
        one,two=str(uuid.uuid4()),str(uuid.uuid4())
        records=[dict(ref=one,kind='alias',payload={'target':two},clock={a.origin:1}),dict(ref=two,kind='alias',payload={'target':one},clock={a.origin:1})]
        with self.assertRaises(SyncError):a.receive(dict(schema_version=2,origin=a.origin,records=records))

if __name__=='__main__':unittest.main()
