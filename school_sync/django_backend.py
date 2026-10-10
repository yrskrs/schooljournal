import copy
import importlib
import json
import threading
import time

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError, close_old_connections
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET, require_http_methods

from .core import Engine, SyncError
from .transport import fetch


class Store:
    def __init__(self, replica, state):
        self.model, self.state = replica, state
        self.meta = copy.deepcopy(state.data)
        self.rows = {row.ref: copy.deepcopy(row.data) for row in replica.objects.all()}

    def all(self):
        return copy.deepcopy(list(self.rows.values()))

    def get(self, ref):
        return copy.deepcopy(self.rows.get(ref))

    def put(self, row):
        self.model.objects.update_or_create(ref=row['ref'], defaults={'data': row})
        self.rows[row['ref']] = copy.deepcopy(row)


class SafeNative:
    def __init__(self, native):
        self.native = native

    def __setattr__(self, key, value):
        if key == 'engine':
            self.native.engine = value
        else:
            object.__setattr__(self, key, value)

    def __getattr__(self, key):
        method = getattr(self.native, key)
        if key not in ('create', 'write'):
            return method
        def call(*args, **kwargs):
            try:
                with transaction.atomic():
                    return method(*args, **kwargs)
            except (IntegrityError, ValidationError) as error:
                raise SyncError('Локальна модель не приймає цю зміну. Вирішіть конфлікт вручну; дані збережено.') from error
        return call


def backend():
    return importlib.import_module(settings.ROSTER_BACKEND)


@transaction.atomic
def operate(action):
    module = backend()
    state, _ = module.RosterState.objects.get_or_create(pk=1)
    state = module.RosterState.objects.select_for_update().get(pk=1)
    store = Store(module.RosterReplica, state)
    engine = Engine(store, SafeNative(module.Native()))
    previous_depth = getattr(_local, 'sync_depth', 0)
    _local.sync_depth = previous_depth + 1
    try:
        result = action(engine, store)
    finally:
        _local.sync_depth = previous_depth
    state.data = store.meta
    state.save(update_fields=['data'])
    return result


def peers():
    data = getattr(settings, 'ROSTER_PEERS', [])
    if not isinstance(data, list) or any(not isinstance(peer, dict) for peer in data):
        raise SyncError('ROSTER_PEERS має бути JSON-масивом налаштованих API.')
    return data


def cycle():
    interval = max(2, getattr(settings, 'ROSTER_SYNC_INTERVAL', 30))
    operate(lambda engine, store: engine.capture())
    for index, peer in enumerate(peers()):
        peer_id = str(index)
        state = operate(lambda engine, store: store.meta.get('peers', {}).get(peer_id, {}))
        if state.get('next_retry', 0) > time.time():
            continue
        try:
            payload = fetch(peer)
            def receive(engine, store):
                known = store.meta.setdefault('peers', {}).setdefault(peer_id, {})
                duplicate = any(key != peer_id and item.get('origin') == payload.get('origin') for key, item in store.meta['peers'].items())
                if duplicate or payload.get('origin') == engine.origin or (known.get('origin') and known['origin'] != payload.get('origin')):
                    raise SyncError('Ідентичність сервісу змінилася або дублюється. Перевірте конфігурацію; дані збережено.')
                engine.receive(payload)
                known.update(origin=payload['origin'], failures=0, next_retry=time.time() + interval,
                    last_success=time.time(), last_error='', empty=not payload['records'])
            operate(receive)
        except Exception as error:
            # No exception strings from HTTP libraries/DBs reach the interface.
            message = str(error) if isinstance(error, SyncError) else 'Помилка синхронізації. Локальні дані збережено; повторну спробу буде виконано.'
            def failed(engine, store):
                known = store.meta.setdefault('peers', {}).setdefault(peer_id, {})
                failures = min(known.get('failures', 0) + 1, 10)
                known.update(failures=failures, next_retry=time.time() + min(interval * 2 ** (failures - 1), 300), last_error=message)
            operate(failed)


def start_worker():
    if not getattr(settings, 'ROSTER_PEERS', []):
        return
    def worker():
        while True:
            close_old_connections()
            try:
                cycle()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Roster retry failed; local data retained.')
            finally:
                close_old_connections()
            time.sleep(max(2, getattr(settings, 'ROSTER_SYNC_INTERVAL', 30)))
    threading.Thread(target=worker, name='roster-sync', daemon=True).start()


@require_GET
def roster_api(request):
    configured = getattr(settings, 'ROSTER_SYNC_TOKEN', '')
    header = request.headers.get('Authorization', '')
    token = header[7:] if header.startswith('Bearer ') else ''
    if not configured or not token or not constant_time_compare(configured, token):
        response = JsonResponse({'error': 'Потрібен чинний Bearer-ключ API списків.'}, status=401)
    else:
        response = JsonResponse(operate(lambda engine, store: engine.snapshot()))
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def roster_ui(request):
    module = backend()
    if not module.can_access(request.user):
        raise PermissionDenied('Потрібні права вчителя або адміністратора.')
    allowed = module.allowed_classes(request.user)
    allowed_ids = {int(row['id']) for row in allowed}
    if request.method == 'POST':
        try:
            def mutate(engine, store):
                action = request.POST.get('action')
                if action == 'share':
                    native_id = int(request.POST['native_class_id'])
                    if native_id not in allowed_ids:
                        raise PermissionDenied('Немає права змінювати цей клас.')
                    engine.share('class', native_id)
                elif action == 'edit':
                    engine.capture()
                    expected = signing.loads(request.POST['version'], salt='roster-review', max_age=86400)
                    row = store.get(request.POST['ref'])
                    if not row or row != expected or row.get('native_id') is None:
                        raise SyncError('Запис змінився або ще не зіставлений. Оновіть сторінку.')
                    payload = module.Native().read(row['kind'], row['native_id'])
                    group_id = row['native_id'] if row['kind'] == 'class' else payload['native_class_id']
                    if group_id not in allowed_ids:
                        raise PermissionDenied('Немає права редагувати цей клас.')
                    payload['active'] = request.POST.get('active') == '1'
                    if row['kind'] == 'class':
                        payload.update(name=request.POST.get('name', '').strip(), grade_level=int(request.POST.get('grade_level', '0')), letter=request.POST.get('letter', '').strip())
                        if not payload['name'] or len(payload['name']) > 50 or not 1 <= payload['grade_level'] <= 12 or len(payload['letter']) > 10:
                            raise SyncError('Потрібні коректна назва, номер 1–12 та літера класу.')
                    else:
                        for field in ('first_name', 'last_name', 'middle_name'):
                            payload[field] = request.POST.get(field, '').strip()
                        if not payload['first_name'] or not payload['last_name'] or any(len(payload[field]) > 100 for field in ('first_name', 'last_name', 'middle_name')):
                            raise SyncError('Потрібні прізвище та ім’я до 100 символів.')
                        payload['name'] = ' '.join(payload[field] for field in ('last_name', 'first_name', 'middle_name') if payload[field])
                    engine.native.write(row['kind'], row['native_id'], payload)
                    engine.capture()
                elif action == 'resolve':
                    expected = signing.loads(request.POST['version'], salt='roster-review', max_age=86400)
                    row = store.get(request.POST['ref'])
                    if row is None:
                        raise SyncError('Запис відсутній.')
                    native_id = int(request.POST['native_id']) if request.POST.get('native_id') else None
                    if row['kind'] == 'class':
                        selected = native_id if native_id is not None else row.get('native_id')
                        if selected not in allowed_ids:
                            raise PermissionDenied('Оберіть доступний клас. Нові класи створюйте засобами сайту.')
                    elif row['kind'] == 'student':
                        parent = store.get(engine.canonical(row['payload']['class_ref']))
                        if not parent or parent.get('native_id') not in allowed_ids:
                            raise PermissionDenied('Немає доступу до класу цього учня.')
                        if native_id is not None and native_id not in module.Native().students(parent['native_id']):
                            raise PermissionDenied('Оберіть учня цього класу.')
                    else:
                        raise SyncError('Службове зіставлення потрібно перевірити через його цільовий запис.')
                    choice = request.POST.get('choice', '')
                    choice = int(choice) if choice.isdigit() else choice
                    if row['kind'] == 'student' and isinstance(choice, int) and 0 <= choice < len(row.get('pending', [])):
                        proposed = row['pending'][choice]
                        target = store.get(engine.canonical(proposed['payload'].get('class_ref')))
                        if not target or target.get('native_id') not in allowed_ids:
                            raise PermissionDenied('Немає права переносити учня до запропонованого класу.')
                    engine.resolve(row['ref'], choice, native_id=native_id,
                        create=request.POST.get('create') == '1' and row['kind'] == 'student', expected=expected)
                else:
                    raise SyncError('Невідома дія.')
                decisions = store.meta.setdefault('decisions', [])
                decisions.append({'user_id': request.user.pk, 'time': time.time(), 'action': action, 'ref': request.POST.get('ref')})
                store.meta['decisions'] = decisions[-100:]
            operate(mutate)
            messages.success(request, 'Рішення збережено. Фоновий обмін передасть його іншим сайтам. Оцінки не імпортовано.')
        except (SyncError, ValueError, KeyError, signing.BadSignature) as error:
            messages.error(request, str(error) if isinstance(error, SyncError) else 'Некоректне або застаріле рішення. Оновіть сторінку.')
        return redirect(request.path)
    def overview(engine, store):
        engine.capture()
        visible = []
        for row in store.all():
            if row['kind'] == 'alias':
                continue
            if row['kind'] == 'class':
                if row.get('native_id') is not None and row['native_id'] not in allowed_ids:
                    continue
                choices = allowed
            else:
                parent = store.get(engine.canonical(row['payload']['class_ref']))
                if not parent or parent.get('native_id') not in allowed_ids:
                    continue
                choices = [dict(id=pk, name=module.Native().read('student', pk)['name']) for pk in module.Native().students(parent['native_id'])]
            row['version'] = signing.dumps(row, salt='roster-review')
            row['choices'] = choices
            visible.append(row)
        return visible, store.meta
    rows, meta = operate(overview)
    return render(request, 'roster_sync.html', {'roster_rows': rows, 'roster_meta': meta, 'roster_classes': allowed})

# Serialize native teacher edits with incoming sync before any row is written.
# This avoids a race in which a remote update could erase an uncaptured edit.
_local = threading.local()

class LocalRosterMixin:
    def save(self, *args, **kwargs):
        if getattr(_local, 'sync_depth', 0):
            return super().save(*args, **kwargs)
        with transaction.atomic():
            module = backend()
            state, _ = module.RosterState.objects.get_or_create(pk=1)
            module.RosterState.objects.select_for_update().get(pk=state.pk)
            result = super().save(*args, **kwargs)
            operate(lambda engine, store: engine.capture())
            return result
