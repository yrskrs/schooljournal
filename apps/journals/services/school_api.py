"""Versioned server-to-server grade contract. No background grade imports."""
import datetime as dt
import json
import math

import requests
from django.core.exceptions import ValidationError

from .external_grades import ExternalAPIError, validate_api_url


def fetch_v2_grades(connection, start, end):
    if not connection.enabled:
        raise ExternalAPIError('Підключення вимкнене.')
    if not connection.source_class_id or not connection.source_subject_id:
        raise ExternalAPIError('Зіставте ID класу й предмета в налаштуваннях підключення.')
    try:
        from .connection_setup import verify_source_identity
        verify_source_identity(connection)
        validate_api_url(connection.url, resolve=True)
        records, cursor = [], 0
        with requests.Session() as session:
            session.trust_env = False
            for page in range(100):
                with session.get(connection.url, headers={'Accept': 'application/json',
                        'Authorization': 'Bearer ' + connection.api_key}, params={
                        'class_id': connection.source_class_id, 'subject_id': connection.source_subject_id,
                        'date_from': start.isoformat(), 'date_to': end.isoformat(), 'cursor': cursor,
                    }, timeout=(5, 20), allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise ExternalAPIError(f'API {connection.get_source_display()} повернув HTTP {response.status_code}. Перевірте адресу, ключ і версію API.')
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > 2 * 1024 * 1024:
                            raise ExternalAPIError('Сторінка API перевищує 2 МБ. Імпорт не виконано.')
                    payload = json.loads(data)
                if not isinstance(payload, dict) or payload.get('schema_version') != 2 or payload.get('source') != connection.source:
                    raise ExternalAPIError('Несумісний контракт API: потрібна версія 2 та правильне джерело. Дані журналу збережено.')
                rows = payload.get('grades')
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ExternalAPIError('Некоректна сторінка оцінок API v2.')
                records.extend(rows)
                if len(records) > 50000:
                    raise ExternalAPIError('Забагато оцінок. Зменште період; імпорт не виконано.')
                following = payload.get('next_cursor')
                if following is None:
                    return records
                if isinstance(following, bool) or not isinstance(following, int) or following <= cursor:
                    raise ExternalAPIError('Некоректний курсор API. Імпорт не виконано.')
                cursor = following
        raise ExternalAPIError('Перевищено ліміт сторінок API. Зменште період.')
    except ValidationError as error:
        raise ExternalAPIError(' '.join(error.messages)) from error
    except (requests.RequestException, ValueError, TypeError) as error:
        raise ExternalAPIError('Джерело недоступне або повернуло некоректну відповідь. Повторіть запит; збережені дані не змінено.') from error


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('API має передати час оновлення результату.')
    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Час оновлення має містити часовий пояс.')
    return stamp


def check_revision(previous, current):
    if previous.get('updated_at') and _timestamp(current['updated_at']) < _timestamp(previous['updated_at']):
        raise ValueError('API повернув старішу версію результату; чинну оцінку збережено.')


def normalize_v2_grade(connection, row):
    if row.get('source') != connection.source or str(row.get('class_id')) != connection.source_class_id or str(row.get('subject_id')) != connection.source_subject_id:
        raise ValueError('Результат іншого джерела, класу або предмета.')
    if row.get('status') != 'final' or row.get('approved_by_teacher') is not True:
        raise ValueError('Результат ще не остаточний.')
    if row.get('grading_scale') != 12:
        raise ValueError('Інша шкала оцінювання: потрібне ручне рішення вчителя.')
    _timestamp(row.get('updated_at'))
    for key in ('points', 'max_points'):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Некоректні бали результату.')
    if row['points'] > row['max_points']:
        raise ValueError('Бали перевищують максимум.')
    from .api_inbox import resolve_student
    local_id = resolve_student(connection, row)
    normalized = {'id': row.get('id'), 'student_id': local_id, 'date': row.get('date'),
        'value': row.get('value'), 'is_ai': row.get('is_ai'), 'approved_by_teacher': row.get('approved_by_teacher')}
    if row.get('lesson_number') is not None:
        normalized['lesson_number'] = row['lesson_number']
    metadata = {key: row.get(key) for key in ('source', 'id', 'student_id', 'class_id', 'subject_id',
        'work_id', 'title', 'points', 'max_points', 'grading_scale', 'date', 'status', 'updated_at', 'attempt_id')}
    return normalized, metadata
