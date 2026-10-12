"""The journal sends its class list to an optional source, using its one key."""
import json
from uuid import UUID, uuid5

import requests
from django.db import transaction
from django.utils import timezone

from apps.journals.models import GradeAPIConnection, GradeStudentMapping, Journal, AuditLog, GradePullLog
from apps.curriculum.models import ClassEnrollment, Student
from apps.journals.services.assessment import journal_students
from apps.portal.permissions import has_journal_access
from school_sync.django_backend import operate
from school_sync.journal_roster import signature
from .connection_setup import source_urls, ensure_same_source
from .external_grades import ExternalAPIError, validate_api_url


def roster_request(connection):
    origin = operate(lambda engine, store: engine.origin)
    namespace = UUID(origin)
    journal = connection.journal
    ignored_ids = set(journal.ignored_students.values_list('pk', flat=True))
    mappings = {m.student_id: m.external_id for m in connection.student_mappings.all()}
    # Omission keeps old copies in v2. Send inactive rows only for pupils that
    # were already bound; an ignored new pupil must never be created upstream.
    students = [s for s in journal_students(journal) if s.pk not in ignored_ids or s.pk in mappings]
    active = set(ClassEnrollment.objects.filter(student_class=journal.student_class, is_active=True).values_list('student_id', flat=True))
    # Mapping across services is explicit and stable; no nz.ua ID is required.
    refs = {str(uuid5(namespace, f'student:{s.pk}')): s for s in students}
    subject_name = next((r['name'] for r in connection.roster_cache.get('subjects', [])
        if r['id'] == connection.source_subject_id), journal.subject.name)
    payload = {'schema_version': 2, 'journal_instance_id': origin,
        'class': {'id': str(uuid5(namespace, f'class:{journal.student_class_id}')),
            'name': journal.student_class.name, 'grade_level': journal.student_class.grade_level,
            'letter': journal.student_class.letter},
        'subject': {'name': subject_name}, 'source_class_id': connection.source_class_id or None,
        'students': [{'id': ref, 'last_name': s.last_name, 'first_name': s.first_name,
            'middle_name': s.middle_name, 'active': s.pk not in ignored_ids and s.pk in active and s.status == Student.Status.ACTIVE,
            'local_id': mappings.get(s.pk)} for ref, s in refs.items()]}
    return payload, refs


def send_journal_roster(connection, user, resolutions=None):
    if not has_journal_access(user, connection.journal, write=True):
        raise ExternalAPIError('Немає права передавати список цього журналу.')
    if 'journal_roster_import' not in connection.roster_cache.get('capabilities', []):
        raise ExternalAPIError('Ця версія джерела ще не приймає списки через просте підключення. Оновіть її з GitHub. Наявне отримання оцінок і місцеві списки збережено.')
    payload, refs = roster_request(connection)
    before = signature(payload)
    payload['resolutions'] = resolutions or {}
    _, url = source_urls(connection.url)
    try:
        validate_api_url(url, resolve=True)
        with requests.Session() as session:
            session.trust_env = False
            with session.post(url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + connection.api_key},
                    json=payload, timeout=(5, 20), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    raise ExternalAPIError('Ключ не дозволяє передавати список. Створіть ключ для журналу з обома дозволами.')
                if response.status_code in (404, 405):
                    raise ExternalAPIError('Джерело ще не підтримує передачу списків. Оновіть його з GitHub.')
                if response.status_code != 200:
                    raise ExternalAPIError(f'Не вдалося передати список (HTTP {response.status_code}). Місцеві дані збережено.')
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise ExternalAPIError('Завеликий звіт передачі списків.')
                report = json.loads(content)
        if not isinstance(report, dict) or report.get('schema_version') != 2 or report.get('source') != connection.source:
            raise ExternalAPIError('Несумісна відповідь передачі списку. Прив’язки журналу збережено.')
        ensure_same_source(connection, report)
        class_id, subject_id = str(report.get('class_id')), str(report.get('subject_id'))
        if not class_id.isdigit() or int(class_id) < 1 or not subject_id.isdigit() or int(subject_id) < 1:
            raise ExternalAPIError('Джерело не підтвердило клас і предмет.')
        mappings = report.get('mappings')
        if not isinstance(mappings, list) or len(mappings) > len(refs):
            raise ExternalAPIError('Некоректний звіт відповідностей учнів.')
        seen_refs, seen_ids = set(), set()
        for row in mappings:
            if not isinstance(row, dict) or row.get('journal_id') not in refs or row['journal_id'] in seen_refs:
                raise ExternalAPIError('Некоректна відповідність учня.')
            local_id = str(row.get('local_id'))
            if not local_id.isdigit() or int(local_id) < 1 or local_id in seen_ids:
                raise ExternalAPIError('Джерело повернуло повторне або некоректне зіставлення.')
            seen_refs.add(row['journal_id']); seen_ids.add(local_id)
        with transaction.atomic():
            journal = Journal.objects.select_for_update().get(pk=connection.journal_id)
            fresh = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
            if not has_journal_access(user, journal, write=True) or fresh.url != connection.url:
                raise ExternalAPIError('Доступ або підключення змінилися. Оновіть сторінку.')
            current, _ = roster_request(fresh)
            if signature(current) != before:
                raise ExternalAPIError('Списки змінилися під час передачі. Повторіть її, щоб підтвердити актуальні відповідності.')
            if fresh.records.exists() and (fresh.source_class_id != class_id or fresh.source_subject_id != subject_id):
                raise ExternalAPIError('Джерело змінило контекст раніше імпортованих оцінок. Історію збережено.')
            for row in mappings:
                student = refs[row['journal_id']]
                local_id = str(row['local_id'])
                old = fresh.student_mappings.filter(student=student).first()
                if old and old.external_id != local_id:
                    raise ExternalAPIError('Джерело змінило підтверджений ID учня. Історію збережено.')
                occupied = fresh.student_mappings.filter(external_id=local_id).first()
                if occupied and occupied.student_id != student.pk:
                    raise ExternalAPIError('Джерело змінило належність підтвердженого учня. Прив’язки та історію збережено.')
                GradeStudentMapping.objects.update_or_create(connection=fresh, external_id=local_id, defaults={'student': student})
            fresh.source_class_id, fresh.source_subject_id = class_id, subject_id
            fresh.enabled = True
            fresh.roster_sent_at = timezone.now()
            fresh.roster_report = report
            cache = dict(fresh.roster_cache)
            for kind, identity, name in [('classes', class_id, report.get('class_name', journal.student_class.name)), ('subjects', subject_id, payload['subject']['name'])]:
                rows = list(cache.get(kind, []))
                existing = next((row for row in rows if row['id'] == identity), None)
                if existing is not None:
                    existing['name'] = name
                else:
                    rows.append({'id': identity, 'name': name})
                cache[kind] = rows
            students = {row['id']: row for row in cache.get('students', [])}
            inactive_refs = {row['id'] for row in payload['students'] if not row['active']}
            for row in mappings:
                local_id = str(row['local_id'])
                if row['journal_id'] in inactive_refs:
                    students.pop(local_id, None)
                else:
                    students[local_id] = {'id': local_id, 'class_id': class_id, 'name': row['name']}
            cache['students'] = list(students.values())
            fresh.roster_cache = cache
            fresh.save(update_fields=['source_class_id', 'source_subject_id', 'enabled', 'roster_sent_at', 'roster_report', 'roster_cache'])
            from .api_inbox import keep_roster_extras
            keep_roster_extras(fresh)
            GradePullLog.objects.create(connection=fresh, user=user,
                counts={**report['counts'], 'operation': 'roster', 'errors': 0}, errors=[])
            AuditLog.objects.create(school=journal.school, user=user, action=AuditLog.Action.EXPORT,
                target_model='GradeAPIConnection', target_id=str(fresh.pk),
                description='Передано копію класу й учнів у місцеву БД джерела. Оцінки не імпортувалися.')
        return report
    except ExternalAPIError as error:
        GradePullLog.objects.create(connection=connection, user=user,
            counts={'operation': 'roster', 'errors': 1}, errors=[{'message': str(error)}])
        raise
    except Exception as error:
        # Do not expose requests exception strings, URLs with keys or upstream HTML.
        message = 'Передачу списку не підтверджено. Перевірте доступність джерела та повторіть; місцеві списки й оцінки збережено.'
        GradePullLog.objects.create(connection=connection, user=user,
            counts={'operation': 'roster', 'errors': 1}, errors=[{'message': message}])
        raise ExternalAPIError(message) from error
