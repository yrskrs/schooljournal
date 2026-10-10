"""Copy teaching topics by sequence number, without moving pupils or dates."""
from hashlib import sha256
import json

from apps.journals.models import LessonPlanTopic, AuditLog
from .scheduling import SUMMARY_TYPES


def numbered_lessons(lessons):
    """Whole-journal teaching sequence; daily numbers and summaries are separate."""
    return dict(enumerate((l for l in lessons if l.lesson_type not in SUMMARY_TYPES), start=1))


def _numbered_records(journal, kind, lock):
    if kind == 'lessons':
        query = journal.lessons.order_by('date', 'lesson_number', 'pk')
        if lock:
            query = query.select_for_update()
        return numbered_lessons(query)
    query = journal.plan_topics.order_by('topic_number', 'pk')
    if lock:
        query = query.select_for_update()
    records = {}
    for topic in query:
        if topic.topic_number in records:
            raise ValueError(f'У календарному плані повторюється номер {topic.topic_number}. Спочатку виправте нумерацію.')
        records[topic.topic_number] = topic
    return records


def _title(record):
    return getattr(record, 'topic', getattr(record, 'title', ''))


def _snapshot(records):
    fields = ('pk', 'topic', 'title', 'date', 'lesson_number', 'topic_number', 'planned_date',
              'lesson_type', 'homework', 'is_conducted', 'updated_at', 'is_synced_to_nz')
    return [[number, *[str(getattr(record, field, '')) for field in fields]]
            for number, record in records.items()]


def preview_topic_copy(source, target, data, *, lock=False):
    if source.pk == target.pk or source.school_id != target.school_id or source.subject_id != target.subject_id:
        raise ValueError('Оберіть інший журнал того самого предмета й школи.')
    sources = _numbered_records(source, data['source_kind'], lock)
    targets = _numbered_records(target, data['target_kind'], lock)
    end = data['source_end'] or max(sources, default=0)
    selected = [(n, record) for n, record in sources.items()
                if data['source_start'] <= n <= end and record.lesson_type not in SUMMARY_TYPES]
    if not selected:
        raise ValueError('У вибраному діапазоні немає навчальних тем. Перевірте джерело та номери.')
    if len(selected) > 1000:
        raise ValueError('За одне копіювання можна перенести до 1000 тем. Зменште діапазон.')
    rows = []
    for number, record in selected:
        target_number = data['target_start'] + number - data['source_start']
        if target_number > 2147483647:
            raise ValueError('Цільовий номер теми виходить за допустимий діапазон.')
        destination = targets.get(target_number)
        if destination is None and data['target_kind'] == 'lessons':
            raise ValueError(f'У цільовому журналі немає уроку №{target_number}. Зменште діапазон або оберіть календарний план без дат.')
        old_title, new_title = _title(destination), _title(record)
        old_homework = destination.homework if destination else ''
        new_homework = record.homework if data['include_homework'] else old_homework
        if not new_title.strip():
            status, label = 'skip', 'Пропущено: порожня тема джерела'
        elif destination and data['target_kind'] == 'plan' and destination.is_conducted:
            status, label = 'skip', 'Пропущено: тему вже проведено'
        elif destination and destination.lesson_type in SUMMARY_TYPES:
            status, label = 'skip', 'Пропущено: підсумкова колонка'
        elif destination and old_title == new_title and old_homework == new_homework:
            status, label = 'skip', 'Без змін'
        elif destination and old_title.strip() and not data['replace_existing']:
            status, label = 'skip', 'Пропущено: тему вже заповнено'
        elif destination:
            status, label = 'update', 'Оновити тему'
        else:
            status, label = 'create', 'Додати тему без дати'
        rows.append({
            'source_number': number, 'target_number': target_number, 'target': destination,
            'source_type': record.lesson_type, 'source_id': record.pk,
            'old_title': old_title, 'new_title': new_title,
            'old_homework': old_homework, 'new_homework': new_homework,
            'status': status, 'status_label': label,
        })
    fingerprint = sha256(json.dumps({'source': _snapshot(sources), 'target': _snapshot(targets)},
                                    sort_keys=True).encode()).hexdigest()
    counts = {status: sum(row['status'] == status for row in rows) for status in ('create', 'update', 'skip')}
    return rows, fingerprint, counts


def apply_topic_copy(source, target, data, rows, user):
    """Caller locks both journals and verifies the current signed preview."""
    for row in rows:
        if row['status'] == 'skip':
            continue
        record = row['target']
        if row['status'] == 'create':
            record = LessonPlanTopic.objects.create(journal=target, topic_number=row['target_number'],
                title=row['new_title'], homework=row['new_homework'], lesson_type=row['source_type'])
        else:
            field = 'topic' if data['target_kind'] == 'lessons' else 'title'
            setattr(record, field, row['new_title'])
            fields = [field, 'updated_at']
            if data['include_homework']:
                record.homework = row['new_homework']
                fields.append('homework')
            if data['target_kind'] == 'lessons':
                record.is_synced_to_nz = False
                fields.append('is_synced_to_nz')
            record.save(update_fields=fields)
        AuditLog.objects.create(school=target.school, user=user,
            action=AuditLog.Action.CREATE if row['status'] == 'create' else AuditLog.Action.UPDATE,
            target_model=record.__class__.__name__, target_id=str(record.pk),
            description='Скопійовано тему з іншого журналу за номером уроку.',
            changes={'source_journal_id': source.pk, 'source_kind': data['source_kind'],
                     'source_record_id': row['source_id'], 'source_number': row['source_number'],
                     'target_number': row['target_number'], 'include_homework': data['include_homework']})
