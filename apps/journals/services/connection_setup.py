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


def validate_roster(payload, source):
    if not isinstance(payload, dict) or payload.get('schema_version') != 2 or payload.get('source') != source:
        raise ExternalAPIError('Сайт не підтримує це підключення. Оновіть його з GitHub; збережені дані журналу залишаються.')
    output = {'schema_version': 2, 'source': source}
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


def fetch_source_roster(url, key, source):
    _, roster_url = source_urls(url)
    try:
        validate_api_url(roster_url, resolve=True)
        with requests.Session() as session:
            session.trust_env = False
            with session.get(roster_url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + key},
                    timeout=(5, 15), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    raise ExternalAPIError('Ключ не прийнято. Створіть на сайті джерела ключ для журналу з доступом до оцінок.')
                if response.status_code == 404:
                    raise ExternalAPIError('API списків не знайдено. Перевірте адресу сайту та оновіть його з GitHub.')
                if response.status_code != 200:
                    raise ExternalAPIError(f'Сайт тимчасово не відповідає правильно (HTTP {response.status_code}). Повторіть пізніше; місцеві дані збережено.')
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ExternalAPIError('Список завеликий для одного підключення.')
                return validate_roster(json.loads(content), source)
    except ValidationError as error:
        raise ExternalAPIError(' '.join(error.messages)) from error
    except (requests.RequestException, ValueError, TypeError) as error:
        raise ExternalAPIError('Не вдалося отримати списки. Перевірте адресу та доступність сайту. Попередні списки й оцінки збережено.') from error


def roster_version(connection):
    state = [connection.roster_cache, connection.source_class_id, connection.source_subject_id,
        list(connection.student_mappings.order_by('external_id').values_list('external_id', 'student_id'))]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def selected_students(connection):
    return [row for row in connection.roster_cache.get('students', []) if row['class_id'] == connection.source_class_id]


def probe_connection_reachability(connection, timeout=1.5):
    """
    Швидка перевірка (до 1.5 с) фізичної доступності хоста й порту сервера.
    Запобігає зависанню на 10-20 секунд та хибним тривогам для неактивних адрес.
    """
    import socket
    try:
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
    Групує підключення за джерелом і групою сервера.
    Повертає список груп: [{'group_key', 'group_name', 'source', 'connections': [...]}]
    """
    groups_dict = {}
    for conn in connections:
        # Ключ групування: якщо вказано server_group, то (source, server_group), інакше окремо для кожного conn
        group_key = f"{conn.source}:{conn.server_group.strip()}" if conn.server_group.strip() else f"{conn.source}:single_{conn.pk}"
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
    online_conn = None
    for c in ordered:
        if not c.enabled:
            results[c.pk] = (False, 'Підключення вимкнено в налаштуваннях')
            continue
        is_up, reason = probe_connection_reachability(c, timeout=timeout)
        results[c.pk] = (is_up, reason)
        if is_up and online_conn is None:
            online_conn = c

    # Якщо ніхто не відповів через probe, повертаємо перше увімкнене для спроби або None
    return online_conn, results

