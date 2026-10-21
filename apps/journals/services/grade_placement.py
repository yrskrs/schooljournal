"""Explicit teacher choices for a work/session; upstream completion dates stay intact."""
import hashlib
import json

from apps.journals.models import GradeLessonAssignment
from .scheduling import SUMMARY_TYPES


def work_key(connection, row):
    from .external_grades import json_field, _extract_session_id
    if not isinstance(row, dict):
        return None
    session = _extract_session_id(row) if connection.contract_version == 2 else None
    if session:
        identity = ['session', session]
    elif row.get('work_id') is not None:
        identity = ['work', str(row['work_id'])]
    else:
        raw_id = row.get('id') if connection.contract_version == 2 else json_field(row, connection.id_key)
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not str(raw_id).strip():
            return None
        identity = ['result', str(raw_id)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def connection_version(connection):
    state = [connection.url, connection.api_key, connection.source, connection.contract_version,
             connection.source_class_id, connection.source_subject_id,
             (connection.roster_cache or {}).get('instance_id')]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def validate_lesson(journal, lesson):
    from .external_grades import ExternalAPIError
    if (lesson.journal_id != journal.pk or lesson.lesson_type in SUMMARY_TYPES
            or not journal.academic_year.start_date <= lesson.date <= journal.academic_year.end_date):
        raise ExternalAPIError('Оберіть звичайний урок цього журналу в межах навчального року.')


def peer_connections(connection):
    from .connection_setup import group_connections_by_server
    groups = group_connections_by_server(connection.journal.grade_apis.filter(source=connection.source))
    return next((g['connections'] for g in groups if any(c.pk == connection.pk for c in g['connections'])), [connection])


def assignment_map(connection, peers=None):
    peers = peer_connections(connection) if peers is None else peers
    result = {}
    for assignment in GradeLessonAssignment.objects.filter(connection__in=peers).select_related('lesson'):
        key = assignment.work_key
        if key in result and (result[key] is None or result[key].pk != assignment.lesson_id):
            result[key] = None  # Conflicting saved choices require a new teacher decision.
        else:
            result[key] = assignment.lesson
    return result
