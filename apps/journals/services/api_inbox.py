"""Keep unmapped identities and unusable grades without changing class lists."""
import datetime as dt
from apps.journals.models import APIInboxResult, ExtraStudent


def student_key(row):
    value = row.get('student_id')
    if value is not None:
        if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
            raise ValueError('Некоректна ідентичність учня джерела.')
        expected = str(value)
    else:
        value = row.get('attempt_id')
        if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
            raise ValueError('Немає стабільної ідентичності самостійно введеного учня.')
        expected = 'unmapped:' + str(value)
    if row.get('student_key', expected) != expected:
        raise ValueError('Суперечлива ідентичність учня джерела.')
    return expected


def keep_result(connection, row, start, end):
    if (not isinstance(row, dict) or row.get('source') != connection.source
            or str(row.get('class_id')) != connection.source_class_id
            or str(row.get('subject_id')) != connection.source_subject_id):
        raise ValueError('Результат іншого джерела, класу або предмета.')
    if row.get('status') != 'final' or row.get('approved_by_teacher') is not True:
        raise ValueError('Результат ще не остаточний.')
    raw_id = row.get('id')
    if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool) or not str(raw_id).strip() or len(str(raw_id)) > 200:
        raise ValueError('Потрібен стабільний ID результату.')
    date = dt.date.fromisoformat(row.get('date', ''))
    if not start <= date <= end or not connection.journal.academic_year.start_date <= date <= connection.journal.academic_year.end_date:
        raise ValueError('Дата результату поза періодом цього журналу.')
    identity = student_key(row)
    prior = APIInboxResult.objects.filter(connection=connection, external_id=str(raw_id)).first()
    from .school_api import _timestamp, check_revision
    _timestamp(row.get('updated_at'))
    if prior:
        check_revision(prior.payload, row)
        if student_key(prior.payload) != identity or prior.payload.get('date') != row.get('date'):
            raise ValueError('Джерело змінило учня або дату відомого результату. Історію збережено.')
    extra = None
    if not connection.student_mappings.filter(external_id=identity).exists():
        name = row.get('student_name')
        if not isinstance(name, str) or not name.strip():
            name = next((s['name'] for s in connection.roster_cache.get('students', []) if s['id'] == identity), 'Учень, що потребує перевірки')
        extra, created = ExtraStudent.objects.get_or_create(connection=connection, external_key=identity, defaults={'name': name.strip()[:200]})
        if not created and extra.name != name.strip()[:200]:
            extra.name = name.strip()[:200]
            extra.save(update_fields=['name', 'updated_at'])
    record, _ = APIInboxResult.objects.update_or_create(connection=connection, external_id=str(raw_id),
        defaults={'extra_student': extra, 'payload': row})
    return record


def resolve_student(connection, row):
    identity = student_key(row)
    mapping = connection.student_mappings.filter(external_id=identity).first()
    if mapping:
        return mapping.student_id
    extra = ExtraStudent.objects.filter(connection=connection, external_key=identity).first()
    if extra and extra.matched_student_id:
        return extra.matched_student_id
    name = (row.get('student_name') or (extra.name if extra else 'Учень'))[:200]
    raise ValueError(f'«{name}»: позакласний учень або помилка в імені. Відкрийте «Позакласні учні та результати» й підтвердьте відповідність, якщо це учень вашого класу.')


def keep_roster_extras(connection):
    for row in connection.roster_cache.get('students', []):
        if row['class_id'] != connection.source_class_id:
            continue
        mapping = connection.student_mappings.filter(external_id=row['id']).first()
        extra = ExtraStudent.objects.filter(connection=connection, external_key=row['id']).first()
        if mapping:
            if extra and not extra.matched_student_id:
                extra.matched_student = mapping.student
                extra.save(update_fields=['matched_student', 'updated_at'])
        elif extra is None:
            ExtraStudent.objects.create(connection=connection, external_key=row['id'], name=row['name'][:200])
