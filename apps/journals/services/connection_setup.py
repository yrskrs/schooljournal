"""Fetch source names; identities are committed only after teacher confirmation."""
import hashlib
import json
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

import requests
from django.core.exceptions import ValidationError

from .external_grades import ExternalAPIError, validate_api_url

SOURCES = {'schooltest': 'Тести', 'schoolwork': 'Завдання'}


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
