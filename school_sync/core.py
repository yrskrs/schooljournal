"""Native records remain authoritative locally; concurrent changes need a person.

Store methods: all(), get(ref), put(record), meta (mutable dictionary).
Native methods: read(kind, id), students(class_id), create(kind, payload),
write(kind, id, payload), collision(kind, payload), and class_ref lookup via engine.
All engine operations run inside the caller's database transaction and lock.
"""
from copy import deepcopy
import uuid


class SyncError(ValueError):
    pass


def compare(left, right):
    keys = set(left) | set(right)
    less = any(left.get(key, 0) < right.get(key, 0) for key in keys)
    more = any(left.get(key, 0) > right.get(key, 0) for key in keys)
    return 'concurrent' if less and more else 'older' if less else 'newer' if more else 'equal'


def validate_record(record):
    if not isinstance(record, dict) or not isinstance(record.get('ref'), str) or len(record['ref']) > 100:
        raise SyncError('Некоректний стабільний ID списку.')
    uuid.UUID(record['ref'])
    if record.get('kind') not in ('class', 'student', 'alias') or not isinstance(record.get('payload'), dict):
        raise SyncError('Некоректний тип запису списку.')
    clock = record.get('clock')
    if not isinstance(clock, dict) or not clock or len(clock) > 16:
        raise SyncError('Некоректна версія запису.')
    for origin, counter in clock.items():
        uuid.UUID(origin)
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 1:
            raise SyncError('Некоректний лічильник версії.')
    payload = record['payload']
    if record['kind'] == 'alias':
        uuid.UUID(payload.get('target', ''))
        if payload['target'] == record['ref']:
            raise SyncError('Зіставлення ID не може вказувати на себе.')
    else:
        if not isinstance(payload.get('name'), str) or not payload['name'].strip() or len(payload['name']) > 300:
            raise SyncError('Потрібне коректне ім’я / назва.')
        if not isinstance(payload.get('active'), bool):
            raise SyncError('Статус списку має бути boolean.')
        if record['kind'] == 'student':
            uuid.UUID(payload.get('class_ref', ''))
            for key in ('first_name', 'last_name', 'middle_name'):
                if not isinstance(payload.get(key, ''), str) or len(payload.get(key, '')) > 100:
                    raise SyncError('Некоректне поле імені учня.')
            if payload['name'] != ' '.join(payload.get(key, '') for key in ('last_name', 'first_name', 'middle_name') if payload.get(key, '')):
                raise SyncError('ПІБ має відповідати окремим полям імені.')
        elif isinstance(payload.get('grade_level'), bool) or not isinstance(payload.get('grade_level'), int) or not 1 <= payload['grade_level'] <= 12 or not isinstance(payload.get('letter'), str) or len(payload['letter']) > 10:
            raise SyncError('Некоректний номер або літера класу.')


class Engine:
    def __init__(self, store, native):
        self.store, self.native = store, native
        self.origin = store.meta.setdefault('origin', str(uuid.uuid4()))
        store.meta.setdefault('waiting', {})
        native.engine = self

    def canonical(self, ref):
        seen = set()
        while ref:
            if ref in seen:
                raise SyncError('Цикл зіставлення ID.')
            seen.add(ref)
            record = self.store.get(ref)
            if not record or record['kind'] != 'alias':
                return ref
            ref = record['payload']['target']
        return ref

    def native_record(self, kind, native_id):
        return next((row for row in self.store.all() if row['kind'] == kind and row.get('native_id') == native_id), None)

    def bump(self, clock, other=None):
        result = {key: max(clock.get(key, 0), (other or {}).get(key, 0)) for key in set(clock) | set(other or {})}
        result[self.origin] = result.get(self.origin, 0) + 1
        return result

    def payload(self, kind, native_id):
        payload = self.native.read(kind, native_id)
        if payload and kind == 'student':
            class_record = self.native_record('class', payload.pop('native_class_id'))
            if not class_record:
                return None
            payload['class_ref'] = class_record['ref']
        return payload

    def share(self, kind, native_id):
        if self.native_record(kind, native_id):
            return self.native_record(kind, native_id)
        payload = self.payload(kind, native_id)
        if payload is None:
            raise SyncError('Запис відсутній або клас ще не приєднано до обміну.')
        record = {'ref': str(uuid.uuid4()), 'kind': kind, 'native_id': native_id,
            'payload': payload, 'clock': {self.origin: 1}, 'status': 'ready', 'pending': []}
        self.store.put(record)
        if kind == 'class':
            for student_id in self.native.students(native_id):
                self.share('student', student_id)
        else:
            for waiting in self.store.meta['waiting'].values():
                if native_id in waiting:
                    waiting.remove(native_id)
        return record

    def capture(self):
        for record in self.store.all():
            if record['kind'] == 'alias' or record.get('native_id') is None:
                continue
            current = self.payload(record['kind'], record['native_id'])
            if current is None:
                current = {**record['payload'], 'active': False}
            if current != record['payload']:
                record['clock'] = self.bump(record['clock'])
                record['payload'] = current
                self.store.put(record)
        # Pupils created later in an already shared class get fresh stable IDs.
        # Existing pupils of a joined class wait for explicit initial mapping.
        for record in self.store.all():
            if record['kind'] == 'class' and record.get('native_id') is not None:
                waiting = self.store.meta['waiting'].get(record['ref'], [])
                for pupil_id in self.native.students(record['native_id']):
                    if pupil_id not in waiting and not self.native_record('student', pupil_id):
                        self.share('student', pupil_id)

    def snapshot(self):
        self.capture()
        return {'schema_version': 2, 'origin': self.origin, 'records': [
            {key: deepcopy(row[key]) for key in ('ref', 'kind', 'payload', 'clock')}
            for row in self.store.all() if row.get('native_id') is not None or row['kind'] == 'alias']}

    def conflict(self, local, incoming, reason):
        local['status'] = 'conflict' if local.get('native_id') is not None else 'unmapped'
        pending = local.setdefault('pending', [])
        if incoming not in pending:
            pending.append(deepcopy(incoming))
        if len(pending) > 16:
            raise SyncError('Забагато невирішених версій. Потрібне рішення вчителя.')
        local['message'] = reason
        self.store.put(local)

    def apply(self, record):
        payload = deepcopy(record['payload'])
        if record['kind'] == 'student':
            class_record = self.store.get(self.canonical(payload['class_ref']))
            if not class_record or class_record.get('native_id') is None:
                raise SyncError('Спочатку явно зіставте клас.')
            payload['native_class_id'] = class_record['native_id']
        if record.get('native_id') is None:
            if record['kind'] == 'class':
                raise SyncError('Оберіть наявний клас або явно підтвердьте створення нового.')
            waiting = self.store.meta['waiting'].get(self.canonical(payload['class_ref']), [])
            if waiting or self.native.collision(record['kind'], payload):
                raise SyncError('Потрібне явне зіставлення ID учня або підтвердження нового запису. ПІБ не використовується для об’єднання.')
            record['native_id'] = self.native.create(record['kind'], payload)
        else:
            self.native.write(record['kind'], record['native_id'], payload)
        record['status'], record['pending'] = 'ready', []
        record.pop('message', None)
        self.store.put(record)

    def receive(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 2:
            raise SyncError('Несумісна версія API списків: потрібна v2. Локальні дані збережено.')
        uuid.UUID(snapshot.get('origin', ''))
        records = snapshot.get('records')
        if not isinstance(records, list) or len(records) > 50000:
            raise SyncError('Некоректний або завеликий список.')
        seen = set()
        for record in records:
            validate_record(record)
            if record['ref'] in seen:
                raise SyncError('Повторний стабільний ID у відповіді.')
            seen.add(record['ref'])
        aliases = {row['ref']: row['payload']['target'] for row in self.store.all() if row['kind'] == 'alias'}
        aliases.update({row['ref']: row['payload']['target'] for row in records if row['kind'] == 'alias'})
        for ref in aliases:
            visited = set()
            while ref in aliases:
                if ref in visited:
                    raise SyncError('Цикл зіставлення ID. Локальні прив’язки збережено.')
                visited.add(ref)
                ref = aliases[ref]
        self.capture()
        # Alias targets may arrive in the same response. Stage them without
        # creating native pupils before processing the explicit ID mapping.
        targets = {row['payload']['target'] for row in records if row['kind'] == 'alias'}
        for row in records:
            if row['ref'] in targets and not self.store.get(row['ref']):
                self.store.put({**deepcopy(row), 'native_id': None, 'status': 'unmapped', 'pending': []})
        for incoming in sorted(records, key=lambda row: {'alias': 0, 'class': 1, 'student': 2}[row['kind']]):
            incoming = deepcopy(incoming)
            original = self.store.get(incoming['ref'])
            if incoming['kind'] == 'alias':
                if original:
                    relation = compare(incoming['clock'], original['clock'])
                    if relation == 'older':
                        continue
                    if relation == 'concurrent' or (relation == 'equal' and original['payload'] != incoming['payload']):
                        self.conflict(original, incoming, 'Зіставлення ID суперечить локальним змінам. Потрібне рішення вчителя.')
                        continue
                target = self.store.get(self.canonical(incoming['payload']['target']))
                if original and original['kind'] != 'alias' and target and original['kind'] != target['kind']:
                    raise SyncError('Зіставлення ID не може змінити тип класу / учня.')
                if original and original.get('native_id') is not None:
                    if target and target.get('native_id') not in (None, original['native_id']):
                        self.conflict(original, incoming, 'Два локальні записи мають різні прив’язки. Потрібне явне рішення.')
                        continue
                    if not target:
                        # Defer until the target arrives; never discard a native binding.
                        self.conflict(original, incoming, 'Очікується ціль явного зіставлення.')
                        continue
                    target['native_id'] = original['native_id']
                    self.store.put(target)
                    self.apply(target)
                incoming.update(native_id=None, status='ready', pending=[])
                if not original or compare(incoming['clock'], original['clock']) in ('newer', 'equal'):
                    self.store.put(incoming)
                continue
            alias = self.store.get(incoming['ref'])
            if alias and alias['kind'] == 'alias' and compare(incoming['clock'], alias['clock']) in ('older', 'equal'):
                continue
            incoming['ref'] = self.canonical(incoming['ref'])
            if incoming['kind'] == 'student':
                incoming['payload']['class_ref'] = self.canonical(incoming['payload']['class_ref'])
            local = self.store.get(incoming['ref'])
            if not local:
                local = {**incoming, 'native_id': None, 'status': 'unmapped', 'pending': []}
                self.store.put(local)
                try:
                    self.apply(local)
                except (SyncError, ValueError) as error:
                    self.conflict(local, incoming, str(error))
                continue
            relation = compare(incoming['clock'], local['clock'])
            if incoming['kind'] != local['kind']:
                raise SyncError('Стабільний ID змінив тип запису.')
            if relation == 'older':
                continue
            if relation == 'concurrent' and local.get('native_id') is not None and incoming['payload'] == local['payload'] and all(proposal['kind'] == local['kind'] and proposal['payload'] == local['payload'] for proposal in local.get('pending', [])):
                local['clock'] = {key: max(local['clock'].get(key, 0), incoming['clock'].get(key, 0)) for key in set(local['clock']) | set(incoming['clock'])}
                local.update(status='ready', pending=[])
                local.pop('message', None)
                self.store.put(local)
                continue
            if relation == 'concurrent' or (relation == 'equal' and incoming['payload'] != local['payload']):
                self.conflict(local, incoming, 'Одночасні зміни списку. Виберіть версію вручну.')
            elif relation == 'newer' or (local.get('native_id') is None and relation == 'equal'):
                if any(compare(incoming['clock'], proposal['clock']) not in ('newer', 'equal') for proposal in local.get('pending', [])):
                    self.conflict(local, incoming, 'Залишилися одночасні зміни. Потрібне рішення вручну.')
                    continue
                candidate = {**local, 'payload': incoming['payload'], 'clock': incoming['clock']}
                try:
                    self.apply(candidate)
                except (SyncError, ValueError) as error:
                    self.conflict(local, incoming, str(error))

    def resolve(self, ref, choice, native_id=None, create=False, expected=None):
        if create and native_id is not None:
            raise SyncError('Оберіть зіставлення або створення нового запису.')
        self.capture()
        record = self.store.get(ref)
        if not record:
            raise SyncError('Запис відсутній.')
        if expected is not None and expected != record:
            raise SyncError('Запис змінився після перегляду. Оновіть сторінку.')
        pending = record.get('pending', [])
        if choice == 'local' and (record.get('native_id') is not None or native_id is not None):
            payload = self.payload(record['kind'], native_id) if native_id is not None else record['payload']
        elif isinstance(choice, int) and 0 <= choice < len(pending):
            payload = pending[choice]['payload']
        else:
            raise SyncError('Оберіть доступну версію.')
        if native_id is not None:
            current = self.payload(record['kind'], native_id)
            if current is None:
                raise SyncError('Оберіть коректний локальний запис із приєднаного класу.')
            old = self.native_record(record['kind'], native_id)
            if old and old['ref'] != ref:
                old_clock = old['clock']
                record['clock'] = {key: max(record['clock'].get(key, 0), old_clock.get(key, 0)) for key in set(record['clock']) | set(old_clock)}
                old.update(kind='alias', payload={'target': ref}, native_id=None,
                    clock=self.bump(old_clock), pending=[], status='ready')
                self.store.put(old)
            record['native_id'] = native_id
            if record['kind'] == 'class':
                already_mapped = {row['native_id'] for row in self.store.all() if row['kind'] == 'student' and row.get('native_id') is not None}
                self.store.meta['waiting'][ref] = [student for student in self.native.students(native_id) if student not in already_mapped]
            for waiting in self.store.meta['waiting'].values():
                if record['kind'] == 'student' and native_id in waiting:
                    waiting.remove(native_id)
        if create:
            adapted = deepcopy(payload)
            if record['kind'] == 'student':
                class_row = self.store.get(self.canonical(payload['class_ref']))
                if not class_row or class_row.get('native_id') is None:
                    raise SyncError('Спочатку зіставте клас.')
                adapted['native_class_id'] = class_row['native_id']
            record['native_id'] = self.native.create(record['kind'], adapted)
        if record.get('native_id') is None:
            raise SyncError('Потрібно явно зіставити ID або створити новий запис.')
        clock = record['clock']
        for proposal in pending:
            clock = {key: max(clock.get(key, 0), proposal['clock'].get(key, 0)) for key in set(clock) | set(proposal['clock'])}
        record.update(payload=deepcopy(payload), clock=self.bump(clock))
        self.apply(record)
        return record
