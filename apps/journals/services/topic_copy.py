"""Copy teaching topics by sequence number, without moving pupils or dates."""
from hashlib import sha256
import json

from apps.journals.models import Lesson, LessonPlanTopic, AuditLog
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


def get_journal_calendar_records(journal, kind='auto', lock=False):
    """
    Збирає записи календарного плану класу за номерами уроків.
    Враховує фактичні непроміжні уроки (numbered_lessons) та планові теми.
    """
    lessons_query = journal.lessons.exclude(lesson_type__in=SUMMARY_TYPES).order_by('date', 'lesson_number', 'pk')
    plan_query = journal.plan_topics.exclude(lesson_type__in=SUMMARY_TYPES).order_by('topic_number', 'pk')
    if lock:
        lessons_query = lessons_query.select_for_update()
        plan_query = plan_query.select_for_update()

    lessons_dict = numbered_lessons(lessons_query)
    plan_dict = {}
    for topic in plan_query:
        if topic.topic_number in plan_dict:
            raise ValueError(f'У календарному плані {journal.student_class.name} повторюється номер {topic.topic_number}. Спочатку виправте нумерацію.')
        plan_dict[topic.topic_number] = topic

    if kind == 'lessons':
        return lessons_dict, lessons_dict, plan_dict
    if kind == 'plan':
        return plan_dict, lessons_dict, plan_dict

    # auto mode: об'єднуємо уроки та теми плану.
    # Фактичні уроки мають пріоритет за своїм номером уроку (1..N).
    # Якщо є планові теми для наступних номерів, додаємо їх.
    combined = dict(lessons_dict)
    for num, pt in plan_dict.items():
        if num not in combined and not pt.is_conducted:
            combined[num] = pt
    if not combined:
        combined = dict(plan_dict)
    return combined, lessons_dict, plan_dict


def preview_calendar_sync(source, target, options, *, lock=False):
    """
    Автоматичне порівняння календарних планів двох класів за номерами уроків (№1, 2, 3...).
    Знаходить спільні уроки, відмінності в темах та домашніх завданнях,
    а також відсутні уроки для автоматичного додавання у календарний план цілі.
    """
    if source.pk == target.pk or source.school_id != target.school_id or source.subject_id != target.subject_id:
        raise ValueError('Оберіть інший журнал того самого предмета й школи.')

    source_kind = options.get('sync_mode') or options.get('source_kind') or 'auto'
    target_kind = options.get('sync_mode') or options.get('target_kind') or 'auto'
    include_homework = bool(options.get('include_homework', True))
    replace_existing = bool(options.get('replace_existing', True))

    sources, src_lessons, src_plan = get_journal_calendar_records(source, source_kind, lock)
    targets, tgt_lessons, tgt_plan = get_journal_calendar_records(target, target_kind, lock)

    if not sources:
        raise ValueError(f'У класі «{source.student_class.name}» немає навчальних тем для синхронізації.')

    all_numbers = sorted(set(sources.keys()).union(targets.keys()))
    rows = []

    for number in all_numbers:
        src_rec = sources.get(number)
        tgt_rec = targets.get(number)

        if src_rec is None:
            # У джерелі такого уроку немає, але він є в цільовому класі
            old_title = _title(tgt_rec)
            old_homework = tgt_rec.homework if tgt_rec else ''
            rows.append({
                'number': number,
                'source_number': '—',
                'target_number': number,
                'source_title': '',
                'source_homework': '',
                'source_type': getattr(tgt_rec, 'lesson_type', 'ORDINARY'),
                'source_id': None,
                'old_title': old_title,
                'new_title': old_title,
                'old_homework': old_homework,
                'new_homework': old_homework,
                'target': tgt_rec,
                'status': 'skip',
                'status_label': 'Лише в цьому класі (без змін)',
                'dest_kind': 'lesson' if isinstance(tgt_rec, Lesson) else ('plan' if tgt_rec else 'none')
            })
            continue

        new_title = _title(src_rec)
        src_homework = src_rec.homework if src_rec else ''
        src_type = src_rec.lesson_type

        old_title = _title(tgt_rec) if tgt_rec else ''
        old_homework = tgt_rec.homework if tgt_rec else ''
        new_homework = src_homework if include_homework else old_homework

        if not new_title.strip():
            status, label = 'skip', 'Пропущено: порожня тема в джерелі'
        elif tgt_rec is None:
            status, label = 'create', 'Додати відсутню тему (у календарний план)'
        elif not isinstance(tgt_rec, Lesson) and getattr(tgt_rec, 'is_conducted', False):
            status, label = 'skip', 'Пропущено: тему плану вже проведено'
        elif getattr(tgt_rec, 'lesson_type', '') in SUMMARY_TYPES:
            status, label = 'skip', 'Пропущено: підсумкова колонка'
        elif old_title.strip() == new_title.strip() and (not include_homework or (old_homework or '').strip() == (new_homework or '').strip()):
            status, label = 'skip', 'Без змін (теми збігаються)'
        elif not old_title.strip():
            status, label = 'update', 'Заповнити порожню тему'
        elif not replace_existing:
            status, label = 'skip', 'Пропущено: тему вже заповнено'
        else:
            status = 'update'
            if include_homework and (old_homework or '').strip() != (new_homework or '').strip():
                label = 'Оновити тему та ДЗ'
            else:
                label = 'Оновити тему'

        rows.append({
            'number': number,
            'source_number': number,
            'target_number': number,
            'source_title': new_title,
            'source_homework': src_homework,
            'source_type': src_type,
            'source_id': src_rec.pk,
            'old_title': old_title,
            'new_title': new_title,
            'old_homework': old_homework,
            'new_homework': new_homework,
            'target': tgt_rec,
            'status': status,
            'status_label': label,
            'dest_kind': 'lesson' if isinstance(tgt_rec, Lesson) else ('plan' if tgt_rec else 'missing')
        })

    fingerprint = sha256(json.dumps({
        'source': _snapshot(sources),
        'target': _snapshot(targets),
        'options': {k: str(v) for k, v in options.items()},
    }, sort_keys=True).encode()).hexdigest()

    counts = {s: sum(r['status'] == s for r in rows) for s in ('create', 'update', 'skip')}
    stats = {
        'source_total': len(sources),
        'target_total': len(targets),
        'differences': counts['create'] + counts['update'],
        'source_class': source.student_class.name,
        'target_class': target.student_class.name,
    }
    return rows, fingerprint, counts, stats


def apply_calendar_sync(source, target, options, rows, user):
    """Caller locks both journals and verifies the current signed preview."""
    include_homework = bool(options.get('include_homework', True))
    for row in rows:
        if row['status'] == 'skip':
            continue
        record = row['target']
        if row['status'] == 'create':
            record = LessonPlanTopic.objects.create(
                journal=target,
                topic_number=row['target_number'],
                title=row['new_title'],
                homework=row['new_homework'],
                lesson_type=row['source_type'] if row['source_type'] not in SUMMARY_TYPES else 'ORDINARY'
            )
            AuditLog.objects.create(
                school=target.school,
                user=user,
                action=AuditLog.Action.CREATE,
                target_model='LessonPlanTopic',
                target_id=str(record.pk),
                description=f'Синхронізовано відсутню тему №{row["target_number"]} з класу {source.student_class.name} (додано в план).',
                changes={
                    'source_journal_id': source.pk,
                    'source_class': source.student_class.name,
                    'target_class': target.student_class.name,
                    'topic_number': row['target_number'],
                    'new_title': row['new_title'],
                    'include_homework': include_homework,
                }
            )
        else:
            is_lesson = isinstance(record, Lesson)
            field = 'topic' if is_lesson else 'title'
            setattr(record, field, row['new_title'])
            fields = [field, 'updated_at']
            if include_homework:
                record.homework = row['new_homework']
                fields.append('homework')
                if is_lesson:
                    record.homework_for_next_lesson = True
                    fields.append('homework_for_next_lesson')
            if is_lesson:
                record.is_synced_to_nz = False
                fields.append('is_synced_to_nz')
            record.save(update_fields=fields)
            AuditLog.objects.create(
                school=target.school,
                user=user,
                action=AuditLog.Action.UPDATE,
                target_model=record.__class__.__name__,
                target_id=str(record.pk),
                description=f'Синхронізовано тему уроку №{row["target_number"]} з класу {source.student_class.name}.',
                changes={
                    'source_journal_id': source.pk,
                    'source_class': source.student_class.name,
                    'target_class': target.student_class.name,
                    'number': row['target_number'],
                    'old_title': row['old_title'],
                    'new_title': row['new_title'],
                    'include_homework': include_homework,
                }
            )
