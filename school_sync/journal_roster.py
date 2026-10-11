"""Optional, journal-initiated roster copies, under the existing roster lock.

Native IDs and work history never change. Names only suggest a match; a caller
must explicitly supply local_id to bind an existing pupil. Missing rows do not
mean deletion. The same module is vendored in all three independent projects.
"""
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import uuid

from .core import SyncError


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def similar(left, right):
    left, right = left.casefold().split(), right.casefold().split()
    if not left or not right:
        return False
    return (SequenceMatcher(None, ' '.join(left), ' '.join(right)).ratio() >= .68
        or (SequenceMatcher(None, left[0], right[0]).ratio() >= .8
            and len(left) > 1 and len(right) > 1 and left[1][0] == right[1][0]))


def validate(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != 2:
        raise SyncError('Потрібен контракт списків v2. Місцеві дані збережено.')
    try:
        uuid.UUID(payload['journal_instance_id'])
        uuid.UUID(payload['class']['id'])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SyncError('Некоректна ідентичність журналу або класу.') from None
    group = payload['class']
    if (not isinstance(group.get('name'), str) or not group['name'].strip() or len(group['name']) > 50
            or isinstance(group.get('grade_level'), bool) or not isinstance(group.get('grade_level'), int)
            or not 1 <= group['grade_level'] <= 12 or not isinstance(group.get('letter', ''), str)
            or len(group.get('letter', '')) > 10):
        raise SyncError('Некоректна назва класу.')
    rows = payload.get('students')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise SyncError('Очікується список до 5000 учнів.')
    seen = set()
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError
            uuid.UUID(row['id'])
            if row['id'] in seen:
                raise ValueError
            seen.add(row['id'])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SyncError('Некоректний або повторний стабільний ID учня.') from None
        if any(not isinstance(row.get(k, ''), str) or len(row.get(k, '')) > 100
               for k in ('first_name', 'last_name', 'middle_name')) or not row.get('first_name', '').strip() or not row.get('last_name', '').strip():
            raise SyncError('Потрібні коректні прізвище та ім’я учня.')
        value = row.get('local_id')
        if not isinstance(row.get('active', True), bool):
            raise SyncError('Статус учня має бути true або false.')
        if value is not None and (isinstance(value, bool) or not str(value).isdigit() or int(value) < 1):
            raise SyncError('Некоректне явне зіставлення учня.')
    value = payload.get('source_class_id')
    if value is not None and (isinstance(value, bool) or not str(value).isdigit() or int(value) < 1):
        raise SyncError('Некоректний ID явно вибраного класу.')
    if not isinstance(payload.get('resolutions', {}), dict):
        raise SyncError('Некоректне рішення конфлікту.')
    return payload


def binding_key(payload, owner):
    return f"{owner}:{payload['journal_instance_id']}:{payload['class']['id']}"


def imported_classes(store, owner):
    return {item['class_id'] for key, item in store.meta.get('journal_imports', {}).items()
            if key.startswith(str(owner) + ':')}


def historical_class_names(store, owner, class_id, name):
    names = {name}
    for key, item in store.meta.get('journal_imports', {}).items():
        if key.startswith(str(owner) + ':') and item['class_id'] == class_id:
            names.update(item.get('class_names', []))
    return names


def import_students(engine, store, payload, owner, allowed_classes):
    """Apply a requested local copy, returning explicit review/conflict details."""
    validate(payload)
    imports = store.meta.setdefault('journal_imports', {})
    key = binding_key(payload, owner)
    binding = imports.get(key)
    requested = payload.get('source_class_id')
    requested = int(requested) if requested else None
    if binding and requested and binding['class_id'] != requested:
        raise SyncError('Цей клас журналу вже пов’язано з іншим місцевим класом. Історію збережено.')
    class_id = binding['class_id'] if binding else requested
    if class_id and class_id not in allowed_classes:
        raise SyncError('Клас недоступний власнику ключа.')
    if not class_id:
        group = payload['class']
        class_id = engine.native.create('class', {'name': group['name'].strip(),
            'grade_level': group['grade_level'], 'letter': group.get('letter', ''), 'active': True})
    if engine.native.read('class', class_id) is None:
        raise SyncError('Місцевий клас більше не існує. Виберіть клас заново.')
    current_class = engine.native.read('class', class_id)
    desired_class = {'name': payload['class']['name'].strip(), 'grade_level': payload['class']['grade_level'],
        'letter': payload['class'].get('letter', ''), 'active': True}
    if binding is None:
        binding = imports[key] = {'class_id': class_id, 'students': {}, 'class_baseline': deepcopy(current_class),
            'remote_class': deepcopy(desired_class), 'class_names': [current_class['name']]}
    counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'needs_review': 0, 'conflicts': 0}
    mappings, review, conflicts = [], [], []
    # An explicitly selected class may use a different local label initially.
    # Later journal renames are copied only if the local label was not edited.
    if desired_class != binding.get('remote_class', desired_class) and current_class != desired_class:
        decision = payload.get('resolutions', {}).get(payload['class']['id'], {})
        version = signature(current_class)
        choice = decision.get('choice') if decision.get('version') == version else None
        if current_class == binding.get('class_baseline') or choice == 'journal':
            engine.native.write('class', class_id, desired_class)
            binding.setdefault('class_names', []).append(current_class['name'])
            current_class = deepcopy(desired_class)
        elif choice != 'local':
            counts['conflicts'] += 1
            conflicts.append({'journal_id': payload['class']['id'], 'name': desired_class['name'],
                'local_name': current_class['name'], 'version': version})
        if current_class == desired_class or choice == 'local':
            binding.update(class_baseline=deepcopy(current_class), remote_class=deepcopy(desired_class))
    claimed = {entry['native_id']: ref for ref, entry in binding['students'].items()}
    for row in payload['students']:
        ref = row['id']
        desired = {k: row.get(k, '').strip() for k in ('first_name', 'last_name', 'middle_name')}
        desired.update(name=' '.join(desired[k] for k in ('last_name', 'first_name', 'middle_name') if desired[k]),
            native_class_id=class_id, active=row.get('active', True))
        entry = binding['students'].get(ref)
        explicit = int(row['local_id']) if row.get('local_id') else None
        if entry and explicit and entry['native_id'] != explicit:
            raise SyncError('Підтверджений учень уже має іншу прив’язку. Історію збережено.')
        native_id = entry['native_id'] if entry else explicit
        if native_id:
            current = engine.native.read('student', native_id)
            if not current or current['native_class_id'] != class_id:
                raise SyncError('Явно вибраний учень не належить цьому класу.')
            if native_id in claimed and claimed[native_id] != ref:
                raise SyncError('Двох учнів журналу не можна пов’язати з одним місцевим учнем.')
        else:
            candidates = []
            for local_id in engine.native.students(class_id):
                current = engine.native.read('student', local_id)
                if current and similar(current['name'], desired['name']):
                    candidates.append({'id': local_id, 'name': current['name']})
            if candidates:
                counts['needs_review'] += 1
                review.append({'journal_id': ref, 'name': desired['name'], 'candidates': candidates})
                continue
            native_id = engine.native.create('student', desired)
            current = deepcopy(desired)
            counts['created'] += 1
        baseline = entry.get('baseline') if entry else None
        if current != desired:
            # A first explicit match also needs an explicit choice of spelling.
            if entry and entry.get('keep_local') and entry.get('remote') == desired and current == baseline:
                counts['unchanged'] += 1
            elif baseline is None or current != baseline:
                resolution = payload.get('resolutions', {}).get(ref, {})
                version = signature(current)
                if resolution.get('version') != version or resolution.get('choice') not in ('local', 'journal'):
                    counts['conflicts'] += 1
                    conflicts.append({'journal_id': ref, 'local_id': native_id, 'name': desired['name'],
                        'local_name': current['name'], 'version': version})
                elif resolution['choice'] == 'journal':
                    engine.native.write('student', native_id, desired)
                    current = deepcopy(desired)
                    counts['updated'] += 1
            else:
                engine.native.write('student', native_id, desired)
                current = deepcopy(desired)
                counts['updated'] += 1
        else:
            counts['unchanged'] += 1
        # The baseline is the last agreed spelling, not a skipped remote edit.
        agreed = current == desired or (payload.get('resolutions', {}).get(ref, {}).get('version') == signature(current)
            and payload.get('resolutions', {}).get(ref, {}).get('choice') == 'local')
        keep_local = (payload.get('resolutions', {}).get(ref, {}).get('choice') == 'local'
            and payload.get('resolutions', {}).get(ref, {}).get('version') == signature(current))
        if entry and entry.get('keep_local') and entry.get('remote') == desired and current == baseline:
            keep_local = True
        binding['students'][ref] = {'native_id': native_id, 'baseline': deepcopy(current) if agreed else baseline,
            'remote': deepcopy(desired), 'keep_local': keep_local}
        claimed[native_id] = ref
        mappings.append({'journal_id': ref, 'local_id': native_id, 'name': current['name']})
    engine.capture()
    return {'schema_version': 2, 'class_id': class_id, 'class_name': current_class['name'], 'counts': counts, 'mappings': mappings,
        'needs_review': review, 'conflicts': conflicts}
