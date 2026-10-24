"""Owner-scoped endpoint selection; probe identity before sending credentials."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import ContextVar
from urllib.parse import urlsplit
import json

import requests
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.journals.models import NetworkSelection, ServiceConnection, ServiceEndpoint
from school_sync.core import SyncError
from school_sync.discovery import validate_ping
from .external_grades import ExternalAPIError, validate_api_url

current_journal_host = ContextVar('current_journal_host', default=None)
_probes = ThreadPoolExecutor(max_workers=4, thread_name_prefix='service-probe')


class ConnectionFailure(ExternalAPIError):
    def __init__(self, message, kind='offline', status_code=None):
        super().__init__(message)
        self.kind, self.status_code = kind, status_code

    @property
    def retryable(self):
        return self.kind == 'offline' or bool(self.status_code and self.status_code >= 500)


def api_failure(status, source, operation='export'):
    label = 'SchoolTest' if source == 'schooltest' else 'SchoolNet'
    if status == 401:
        return ConnectionFailure(f'Ключ не прийнято. Створіть новий ключ для журналу в {label} та замініть його в «Підключеннях».', 'unauthorized', status)
    if status == 403:
        right = 'передачу списків' if operation == 'roster_import' else 'експорт оцінок'
        return ConnectionFailure(f'У ключа немає права на {right}. Увімкніть цей дозвіл або створіть ключ з обома дозволами в {label}.', 'forbidden', status)
    return ConnectionFailure(f'Сервіс тимчасово не завершив запит (HTTP {status}). Перевірте «Підключення» та повторіть дію.', 'http', status)


def _http_json(url, token, timeout):
    """Network-only worker; no database, session or pupil access."""
    try:
        validate_api_url(url, resolve=True)
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, headers=headers, timeout=(min(timeout, .6), timeout),
                    allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    return response.status_code, None
                raw = bytearray()
                for chunk in response.iter_content(1024):
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        raise ConnectionFailure('Відповідь перевірки завелика. Перевірте адресу сервісу.', 'invalid_response')
                return 200, json.loads(raw)
    except ValidationError as error:
        raise ConnectionFailure('Адреса не дозволена правилами безпеки. Вкажіть мережеву адресу без ключа в URL.', 'invalid_address') from error
    except requests.RequestException as error:
        raise ConnectionFailure('Сервер не відповідає. Перевірте адресу, порт і профіль мережі.') from error
    except (ValueError, TypeError) as error:
        raise ConnectionFailure('Сервіс повернув незрозумілу відповідь. Перевірте порт і оновлення сайту.', 'invalid_response') from error


def get_small_json(url, token=None, timeout=1.5):
    # The deadline includes DNS and response bytes, unlike socket timeout alone.
    future = _probes.submit(_http_json, url, token, timeout)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout as error:
        future.cancel()
        raise ConnectionFailure('Сервер не відповів за 1,5 секунди. Перевірте профіль мережі.') from error


def ordered_endpoints(service, exclude=()):
    selection = NetworkSelection.objects.filter(owner_id=service.owner_id).first()
    rows = service.endpoints.filter(enabled=True, profile__enabled=True,
        profile__owner_id=service.owner_id).exclude(pk__in=exclude).select_related('profile', 'connection')
    if selection and selection.selected_profile_id:
        rows = rows.filter(profile_id=selection.selected_profile_id)
    last_profile = selection.last_profile_id if selection else None
    host = current_journal_host.get()
    return sorted(rows, key=lambda row: (row.pk != service.last_endpoint_id,
        row.profile_id != last_profile, not (row.profile.use_journal_host and host), row.pk))


def resolve_endpoint(service, exclude=()):
    if not service.enabled:
        raise ConnectionFailure('Підключення вимкнено. Увімкніть його на сторінці «Підключення».', 'disabled')
    last_error = None
    for endpoint in ordered_endpoints(service, exclude):
        try:
            status, data = get_small_json(endpoint.api_url('ping', current_journal_host.get()))
            if status == 404:
                raise ConnectionFailure('Оновіть сайт джерела: він ще не підтримує швидку перевірку підключення.', 'unsupported')
            if status != 200:
                raise api_failure(status, service.source)
            try:
                ping = validate_ping(data, service.source, service.instance_id)
            except SyncError as error:
                raise ConnectionFailure(str(error), 'identity_mismatch') from error
            ServiceEndpoint.objects.filter(pk=endpoint.pk).update(last_checked_at=timezone.now(), last_status='online', last_error='')
            return endpoint, ping
        except ValidationError:
            last_error = ConnectionFailure('Адреса профілю ще не вказана. Відкрийте «Підключення» та вкажіть хост сервера.', 'invalid_address')
        except ConnectionFailure as error:
            last_error = error
        ServiceEndpoint.objects.filter(pk=endpoint.pk).update(last_checked_at=timezone.now(),
            last_status=last_error.kind, last_error=str(last_error)[:500])
    message = str(last_error) if last_error else 'У вибраному профілі немає доступної адреси. Відкрийте «Підключення» та перевірте мережу.'
    ServiceConnection.objects.filter(pk=service.pk).update(last_checked_at=timezone.now(), last_error=message[:500])
    raise last_error or ConnectionFailure(message)


def remember_endpoint(service, endpoint, ping):
    now = timezone.now()
    ServiceConnection.objects.filter(pk=service.pk).update(last_endpoint_id=endpoint.pk,
        last_checked_at=now, last_success_at=now, version=ping['version'], last_error='')
    service.last_endpoint_id, service.last_checked_at = endpoint.pk, now
    service.last_success_at, service.version, service.last_error = now, ping['version'], ''
    selection, _ = NetworkSelection.objects.get_or_create(owner_id=service.owner_id)
    selection.last_profile_id = endpoint.profile_id
    selection.save(update_fields=['last_profile'])


def _check_service(service):
    endpoint, ping = resolve_endpoint(service)
    status, data = get_small_json(endpoint.api_url('connection', current_journal_host.get()), service.token)
    if status != 200:
        error = api_failure(status, service.source)
        ServiceConnection.objects.filter(pk=service.pk).update(token_status=error.kind,
            last_error=str(error)[:500], last_checked_at=timezone.now())
        raise error
    try:
        info = validate_ping(data, service.source, ping['instance_id'])
    except SyncError as error:
        raise ConnectionFailure('Сайт змінив ідентичність під час перевірки. Ключ і дані журналу не змінено.', 'identity_mismatch') from error
    rights = data.get('permissions')
    if (data.get('schema_version') != 2 or not isinstance(rights, dict)
            or any(not isinstance(rights.get(k), bool) for k in ('export', 'roster_import'))):
        raise ConnectionFailure('Оновіть сайт джерела: він не підтвердив права ключа.', 'invalid_response')
    service.instance_id = info['instance_id']
    service.permissions = {k: rights[k] for k in ('export', 'roster_import')}
    service.token_status = 'accepted'
    service.save(update_fields=['instance_id', 'permissions', 'token_status'])
    remember_endpoint(service, endpoint, ping)
    return {'endpoint': endpoint, 'ping': ping, 'permissions': service.permissions}


def check_service(service):
    try:
        return _check_service(service)
    except ConnectionFailure as error:
        updates = {'last_error': str(error)[:500], 'last_checked_at': timezone.now()}
        if error.status_code in (401, 403):
            updates['token_status'] = error.kind
        ServiceConnection.objects.filter(pk=service.pk).update(**updates)
        raise


def prepare_binding(binding, exclude=()):
    service = ServiceConnection.objects.get(pk=binding.service_connection_id)
    journal = binding.journal
    owner_id = (journal.nz_owner.user_id if journal.nz_owner_id else None) or journal.local_profile_id or journal.teacher_id
    if service.owner_id != owner_id or service.source != binding.source:
        raise ConnectionFailure('Підключення належить іншому власнику або джерелу. Перевірте зіставлення журналу.', 'invalid_owner')
    if not binding.enabled:
        raise ConnectionFailure('Джерело вимкнено в налаштуваннях цього журналу.', 'disabled')
    if not service.instance_id:
        raise ConnectionFailure('Спочатку підтвердьте сервіс кнопкою «Перевірити» на сторінці «Підключення».', 'unverified')
    endpoint, ping = resolve_endpoint(service, exclude)
    binding._central_transport = {'endpoint': endpoint, 'service': service,
        'grades': endpoint.api_url('grades', current_journal_host.get()),
        'roster': endpoint.api_url('roster', current_journal_host.get()), 'token': service.token}
    remember_endpoint(service, endpoint, ping)
    return endpoint


def transport_credentials(binding, kind='grades'):
    if binding.service_connection_id:
        if getattr(binding, '_central_transport', None) is None:
            prepare_binding(binding)
        return binding._central_transport[kind], binding._central_transport['token']
    if kind == 'roster':
        from .connection_setup import source_urls
        return source_urls(binding.url)[1], binding.api_key
    return binding.url, binding.api_key


def verify_prepared_transport(binding):
    if binding.service_connection_id:
        transport = binding._central_transport
        service, endpoint = transport['service'], transport['endpoint']
        status, payload = get_small_json(endpoint.api_url('ping', current_journal_host.get()))
        if status != 200:
            raise api_failure(status, service.source)
        try:
            validate_ping(payload, service.source, service.instance_id)
        except SyncError as error:
            raise ConnectionFailure(str(error), 'identity_mismatch') from error


def exchange_central(binding, exchange):
    excluded, last_error = set(), None
    while True:
        try:
            endpoint = prepare_binding(binding, excluded)
        except ConnectionFailure:
            if last_error:
                raise last_error
            raise
        binding._central_in_exchange = True
        try:
            return exchange(binding)
        except ConnectionFailure as error:
            if not error.retryable:
                raise
            last_error = error
            excluded.add(endpoint.pk)
            ServiceEndpoint.objects.filter(pk=endpoint.pk).update(last_status=error.kind, last_error=str(error)[:500])
        finally:
            binding._central_in_exchange = False


class NetworkContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = urlsplit('http://' + request.get_host()).hostname
        token = current_journal_host.set(host)
        try:
            return self.get_response(request)
        finally:
            current_journal_host.reset(token)
