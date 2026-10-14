"""Read saved outside results as a journal grid without creating school records."""
import datetime as dt
from collections import defaultdict

from apps.journals.models import Grade
from apps.journals.services.assessment import journal_students


def result_date(payload):
    try:
        return dt.date.fromisoformat(payload.get('date', ''))
    except (ValueError, TypeError, AttributeError):
        return None


def build_outside_tables(groups, journals, legacy_by_parent, *, search='', source='', month='', semester=None):
    tables = {}

    def in_period(date):
        if month and (date is None or date.strftime('%Y-%m') != month):
            return False
        return not semester or (date is not None and semester.start_date <= date <= semester.end_date)

    def table_for(journal):
        if journal.pk not in tables:
            lessons = list(journal.lessons.order_by('date', 'lesson_number'))
            columns = {(lesson.date, lesson.lesson_number): {
                'date': lesson.date, 'number': lesson.lesson_number, 'topic': lesson.topic,
                'is_lesson': True} for lesson in lessons if in_period(lesson.date)}
            tables[journal.pk] = {'journal': journal, 'columns_by_key': columns,
                'lessons': lessons, 'rows_by_key': {}, 'legacy_journals': legacy_by_parent.get(journal.pk, [])}
        return tables[journal.pk]

    for group in groups:
        for context in group['contexts']:
            extra = context['extra']
            journal = extra.connection.journal
            table = table_for(journal)
            # A confirmed separate profile keeps its native ID, including old rows.
            identity = ('student', extra.separate_student_id) if extra.separate_student_id else group['identity']
            row = table['rows_by_key'].setdefault(identity, {'name': group['name'], 'extra': extra,
                'is_separate': extra.is_separate, 'cells_by_key': defaultdict(list), 'work_keys': set()})
            for result in context['all_results']:
                payload = result.payload
                date = result_date(payload)
                if not in_period(date):
                    continue
                connection = result.connection
                work_key = (connection.source, connection.roster_cache.get('instance_id') or connection.pk, result.external_id)
                if work_key in row['work_keys']:
                    continue
                row['work_keys'].add(work_key)
                number = payload.get('lesson_number')
                if isinstance(number, bool) or not str(number or '').isdigit():
                    same_day = [lesson for lesson in table['lessons'] if lesson.date == date]
                    number = same_day[0].lesson_number if len(same_day) == 1 else 0
                else:
                    number = int(number)
                key = (date, number)
                table['columns_by_key'].setdefault(key, {'date': date, 'number': number,
                    'topic': 'Роботи без прив’язки до уроку', 'is_lesson': False})
                value = payload.get('value')
                row['cells_by_key'][key].append({'value': value if value is not None else '—',
                    'title': payload.get('title', '') or 'Результат роботи',
                    'source': connection.get_source_display(), 'extra_id': extra.pk,
                    'is_saved_grade': False})

    # Older dedicated journals may also contain manual grades or pupils without
    # an API identity. Their persisted history remains visible in the directory.
    for journal in journals:
        for legacy in legacy_by_parent.get(journal.pk, []):
            table = table_for(journal)
            pupils = list(journal_students(legacy))
            for pupil in pupils:
                if search and search.casefold() not in pupil.get_full_name_ukr().casefold():
                    continue
                identity = ('student', pupil.pk)
                if source and identity not in table['rows_by_key']:
                    continue
                row = table['rows_by_key'].setdefault(identity, {'name': pupil.get_full_name_ukr(),
                    'extra': None, 'is_separate': True, 'cells_by_key': defaultdict(list), 'work_keys': set()})
                row['legacy_journal'] = legacy
            for grade in Grade.objects.filter(lesson__journal=legacy).select_related('lesson', 'import_record__connection'):
                row = table['rows_by_key'].get(('student', grade.student_id))
                if row is None or not in_period(grade.lesson.date):
                    continue
                if source and (not grade.import_record_id or grade.import_record.connection.source != source):
                    continue
                key = (grade.lesson.date, grade.lesson.lesson_number)
                table['columns_by_key'].setdefault(key, {'date': grade.lesson.date,
                    'number': grade.lesson.lesson_number, 'topic': grade.lesson.topic, 'is_lesson': True})
                # The persisted grade is authoritative after an explicit manual edit.
                row['cells_by_key'][key] = [{'value': grade.value or '—', 'title': grade.lesson.topic,
                    'source': 'Збережена оцінка окремого журналу', 'is_saved_grade': True}]

    result = []
    for table in tables.values():
        keys = sorted(table['columns_by_key'], key=lambda key: (key[0] or dt.date.max, key[1]))
        table['columns'] = [table['columns_by_key'][key] for key in keys]
        table['rows'] = sorted(table['rows_by_key'].values(), key=lambda row: row['name'])
        for row in table['rows']:
            row['cells'] = [row['cells_by_key'].get(key, []) for key in keys]
        table['column_count'] = max(len(keys), 1) + 3
        result.append(table)
    return result
