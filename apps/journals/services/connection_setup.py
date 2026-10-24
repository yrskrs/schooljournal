"""Fetch source names; identities are committed only after teacher confirmation."""
import hashlib
import json
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

import requests
from django.core.exceptions import ValidationError

from .external_grades import ExternalAPIError, validate_api_url

SOURCES = {'schooltest': 'Тести', 'schoolwork': 'Завдання'}


def existing_source_connection(journal, source, url, payload):
    """A changed LAN address is still the same verified source instance."""
    candidates = journal.grade_apis.filter(source=source, contract_version=2)
    instance = payload.get('instance_id')
    if instance:
        same = list(candidates.filter(roster_cache__instance_id=instance))
        if same:
            return sorted(same, key=lambda c: (not c.enabled, c.pk))[0]
    return candidates.filter(url=url).order_by('pk').first()


def ensure_same_source(connection, payload):
    previous = connection.roster_cache.get('instance_id')
    if previous and payload.get('instance_id') != previous:
        raise ExternalAPIError('За цією адресою інший сервіс або його версія не підтверджує ідентичність. Перевірте адресу; попереднє підключення та оцінки збережено.')


def source_urls(value):
    validate_api_url(value)
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValidationError('Вставте адресу сайту без параметрів запиту.')
    path = parsed.path.rstrip('/')
    for suffix in ('/api/v2/journal/grades', '/api/v2/journal/roster'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    base = urlunsplit((parsed.scheme, parsed.netloc, path, '', '')).rstrip('/')
    return base + '/api/v2/journal/grades/', base + '/api/v2/journal/roster/'


def validate_roster(payload, source=None):
    if not isinstance(payload, dict) or payload.get('schema_version') != 2:
        raise ExternalAPIError('Сайт не підтримує це підключення. Оновіть його з GitHub; збережені дані журналу залишаються.')
    payload_source = payload.get('source')
    if source and payload_source != source:
        source_labels = {'schooltest': 'Сайт тестів (SchoolTest)', 'schoolwork': 'Сайт завдань (SchoolWork)'}
        remote_label = source_labels.get(payload_source, str(payload_source))
        curr_label = source_labels.get(source, str(source))
        raise ExternalAPIError(
            f'Невідповідність типу сайту: це підключення налаштовано як «{curr_label}», '
            f'але сервер за вказаною адресою повернув дані як «{remote_label}». '
            f'Будь ласка, оберіть тип джерела «{remote_label}» у налаштуваннях підключення.'
        )
    output = {'schema_version': 2, 'source': payload_source or source}
    if isinstance(payload.get('capabilities'), list):
        output['capabilities'] = [item for item in payload['capabilities'] if item in ('journal_roster_import', 'journal_roster_ignore')]
    if 'instance_id' in payload:
        try:
            output['instance_id'] = str(UUID(payload['instance_id']))
        except (ValueError, TypeError, AttributeError) as error:
            raise ExternalAPIError('Некоректна ідентичність сервісу. Попередні дані збережено.') from error
    for kind in ('classes', 'subjects', 'students'):
        rows = payload.get(kind)
        if not isinstance(rows, list) or len(rows) > 50000:
            raise ExternalAPIError('Сайт повернув некоректний список. Дані журналу збережено.')
        seen, output[kind] = set(), []
        for row in rows:
            if not isinstance(row, dict):
                raise ExternalAPIError('Некоректний запис списку.')
            identity = row.get('id')
            name = row.get('name')
            if isinstance(identity, bool) or len(str(identity)) > 100 or not str(identity).isdigit() or int(identity) < 1 or str(identity) in seen or not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ExternalAPIError('Некоректна назва або ідентичність запису списку.')
            seen.add(str(identity))
            item = {'id': str(identity), 'name': name.strip()}
            if kind == 'students':
                item['class_id'] = str(row.get('class_id'))
            output[kind].append(item)
    classes = {row['id'] for row in output['classes']}
    if any(row['class_id'] not in classes for row in output['students']):
        raise ExternalAPIError('Список містить учня невідомого класу.')
    return output


def fetch_source_roster(url, key, source=None):
    _, roster_url = source_urls(url)
    try:
        validate_api_url(roster_url, resolve=True)
        with requests.Session() as session:
            session.trust_env = False
            with session.get(roster_url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + key},
                    timeout=(5, 15), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    from .network_connections import api_failure
                    raise api_failure(response.status_code, source)
                if response.status_code == 404:
                    raise ExternalAPIError('API списків не знайдено. Перевірте адресу сайту та оновіть його з GitHub.')
                if response.status_code != 200:
                    from .network_connections import api_failure
                    raise api_failure(response.status_code, source)
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ExternalAPIError('Список завеликий для одного підключення.')
                return validate_roster(json.loads(content), source)
    except ValidationError as error:
        raise ExternalAPIError(' '.join(error.messages)) from error
    except requests.RequestException as error:
        from .network_connections import ConnectionFailure
        raise ConnectionFailure('Не вдалося отримати списки. Перевірте адресу та доступність сайту. Попередні списки й оцінки збережено.') from error
    except (ValueError, TypeError) as error:
        raise ExternalAPIError('Не вдалося отримати списки. Перевірте адресу та доступність сайту. Попередні списки й оцінки збережено.') from error


def fetch_connection_roster(connection):
    if connection.service_connection_id:
        from .network_connections import exchange_central, transport_credentials
        def receive(binding):
            url, token = transport_credentials(binding)
            payload = fetch_source_roster(url, token, binding.source)
            ensure_same_source(binding, payload)
            if payload.get('instance_id') != str(binding._central_transport['service'].instance_id):
                raise ExternalAPIError('Джерело змінило ідентичність під час отримання списку. Прив’язки збережено.')
            return payload
        return exchange_central(connection, receive)
    return fetch_source_roster(connection.url, connection.api_key, connection.source)


def verify_source_identity(connection):
    """Verify the live instance once per connection object, before any exchange."""
    if connection.service_connection_id:
        if not getattr(connection, '_central_in_exchange', False):
            from .network_connections import prepare_binding
            prepare_binding(connection)
        return
    instance = connection.roster_cache.get('instance_id')
    if not instance:
        return
    identity = (connection.url, connection.api_key, connection.source, instance)
    if getattr(connection, '_verified_source_identity', None) == identity:
        return
    payload = fetch_source_roster(connection.url, connection.api_key, connection.source)
    ensure_same_source(connection, payload)
    connection._verified_source_identity = identity


def roster_version(connection):
    from .assessment import journal_students
    state = [connection.roster_cache, connection.roster_report, connection.source_class_id, connection.source_subject_id,
        list(connection.student_mappings.order_by('external_id').values_list('external_id', 'student_id')),
        sorted((s.pk, s.get_full_name_ukr(), s.status) for s in journal_students(connection.journal)),
        sorted(connection.journal.ignored_students.values_list('pk', flat=True))]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def selected_students(connection):
    rows = [row for row in connection.roster_cache.get('students', []) if row['class_id'] == connection.source_class_id]
    report = connection.roster_report if isinstance(connection.roster_report, dict) else {}
    if (report.get('schema_version') != 2 or str(report.get('class_id')) != connection.source_class_id
            or report.get('instance_id') != connection.roster_cache.get('instance_id')):
        return rows
    known = {str(row['id']) for row in rows}
    # Unconfirmed candidates are absent from the confirmed mapping response.
    # Keep their explicit source IDs available in the teacher's review form.
    for item in report.get('needs_review', []):
        if not isinstance(item, dict):
            continue
        for candidate in item.get('candidates', []):
            if not isinstance(candidate, dict):
                continue
            identity, name = str(candidate.get('id', '')), candidate.get('name')
            if identity.isascii() and identity.isdecimal() and int(identity) > 0 and isinstance(name, str) and identity not in known:
                rows.append({'id': identity, 'name': name, 'class_id': connection.source_class_id})
                known.add(identity)
    return rows


def probe_connection_reachability(connection, timeout=1.5):
    """
    Швидка перевірка (до 1.5 с) фізичної доступності хоста й порту сервера.
    Запобігає зависанню на 10-20 секунд та хибним тривогам для неактивних адрес.
    """
    if connection.service_connection_id:
        from .network_connections import prepare_binding, ConnectionFailure
        try:
            prepare_binding(connection)
            return True, 'У мережі · сервіс підтверджено'
        except ConnectionFailure as error:
            return False, str(error)
    import socket
    try:
        validate_api_url(connection.url, resolve=True)
        parsed = urlsplit(connection.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if not host:
            return False, 'Не вказано хост'
        with socket.create_connection((host, port), timeout=timeout):
            return True, 'В мережі'
    except (socket.timeout, TimeoutError):
        return False, 'Тайм-аут (не відповідає в поточній мережі)'
    except ConnectionRefusedError:
        return False, 'Відхилено (порт закрито на сервері)'
    except OSError as err:
        return False, f'Недоступно ({err.strerror or str(err)})'
    except Exception as err:
        return False, f'Помилка ({str(err)})'


def group_connections_by_server(connections):
    """
    Групує адреси одного екземпляра в межах журналу, класу й предмета.
    Без підтвердженого instance_id використовує ручну групу або окрему адресу.
    """
    groups_dict = {}
    for conn in connections:
        cache = conn.roster_cache if isinstance(conn.roster_cache, dict) else {}
        try:
            instance = str(UUID(cache.get('instance_id', '')))
        except (ValueError, TypeError, AttributeError):
            instance = ''
        identity = ('instance:' + instance if instance and conn.contract_version == 2 else
                    'group:' + conn.server_group.strip() if conn.server_group.strip() else f'single:{conn.pk}')
        if conn.service_connection_id:
            identity = f'service:{conn.service_connection_id}'
        # Addresses may substitute for one another only within the same journal
        # and source context. A known instance takes precedence over a label.
        group_key = f'{conn.source}:{conn.journal_id}:{conn.source_class_id}:{conn.source_subject_id}:{identity}'
        group_name = conn.server_group.strip() or conn.name or conn.get_source_display()
        if group_key not in groups_dict:
            groups_dict[group_key] = {
                'key': group_key,
                'name': group_name,
                'source': conn.source,
                'source_display': conn.get_source_display(),
                'is_grouped': bool(conn.server_group.strip()),
                'connections': [],
            }
        groups_dict[group_key]['connections'].append(conn)
    return list(groups_dict.values())


def find_online_connection_in_group(group_connections, preferred_id=None, timeout=1.5):
    """
    Знаходить доступне в поточній мережі підключення з групи.
    Пріоритет:
    1. preferred_id (якщо явно вибрано користувачем)
    2. is_default_for_group (основна адреса)
    3. Перша доступна адреса після швидкої перевірки probe_connection_reachability.
    Повертає: (active_conn, probe_results_dict)
    """
    results = {}
    ordered = []

    # Спочатку шукаємо за preferred_id
    if preferred_id:
        pref = next((c for c in group_connections if str(c.pk) == str(preferred_id)), None)
        if pref:
            ordered.append(pref)

    # Потім пріоритетні за замовчуванням
    for c in group_connections:
        if c.is_default_for_group and c not in ordered:
            ordered.append(c)

    # Решта підключень
    for c in group_connections:
        if c not in ordered:
            ordered.append(c)

    # Перевіряємо по черзі
    for c in ordered:
        if not c.enabled:
            results[c.pk] = (False, 'Підключення вимкнено в налаштуваннях')
            continue
        is_up, reason = probe_connection_reachability(c, timeout=timeout)
        results[c.pk] = (is_up, reason)
        if is_up:
            try:
                verify_source_identity(c)
            except ExternalAPIError:
                results[c.pk] = (False, 'Адреса не підтвердила ідентичність сервісу або доступ ключа')
                continue
            return c, results

    return None, results


def exchange_with_fallback(connections, exchange, preferred_id=None, selector=None):
    """Try another verified address when the actual API request fails as well.

    The caller supplies one group from group_connections_by_server. Only the
    exchange is retried; importing grades happens once after it succeeds.
    """
    remaining = [c for c in connections if c.enabled]
    if not remaining:
        raise ExternalAPIError('Підключення вимкнене. Увімкніть його в розділі «API та обмін».')
    if all(c.service_connection_id for c in remaining):
        from .network_connections import exchange_central
        remaining.sort(key=lambda c: (str(c.pk) != str(preferred_id), not c.is_default_for_group, c.pk))
        last_error = None
        for candidate in remaining:
            try:
                return candidate, exchange_central(candidate, exchange)
            except ExternalAPIError as error:
                last_error = error
        raise last_error
    if len(remaining) == 1:
        return remaining[0], exchange(remaining[0])
    selector = selector or find_online_connection_in_group
    last_error = None
    while remaining:
        active, _ = selector(remaining, preferred_id=preferred_id, timeout=1.5)
        if active is None:
            break
        try:
            return active, exchange(active)
        except ExternalAPIError as error:
            last_error = error
            remaining = [c for c in remaining if c.pk != active.pk]
    if last_error:
        raise ExternalAPIError(f'Жодна адреса не завершила запит. {last_error}') from last_error
    raise ExternalAPIError('Жодна адреса цього сервісу не підтвердила доступність та ідентичність. Перевірте адресу, ключ і мережу в розділі «API та обмін».')


def safely_delete_grade_api_connection(connection):
    """
    Повне та безпечне видалення підключення GradeAPIConnection без конфліктів
    з обмеженнями захисту (on_delete=RESTRICT / PROTECT).
    Усі залежні технічні записи (журнал викликів, кеш імпортованих записів ExternalGrade,
    вхідні помилки, співставлення учнів) видаляються у правильному порядку.
    Реальні оцінки в клітинках журналу (Grade) гарантовано залишаються неушкодженими,
    оскільки їхній зв'язок import_record переходить у стан NULL (SET_NULL).
    """
    connection.inbox_results.all().delete()
    connection.records.all().delete()
    connection.extra_students.update(alias_of=None)
    connection.extra_students.all().delete()
    connection.student_mappings.all().delete()
    connection.deleted_identities.all().delete()
    connection.pull_logs.all().delete()
    connection.delete()

