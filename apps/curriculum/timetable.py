import datetime
from django.urls import reverse
from apps.curriculum.models import LessonTime, WeeklyScheduleEntry
from apps.journals.models import Lesson
from apps.journals.services.scheduling import SUMMARY_TYPES


def build_week_schedule(school, journals, week_start):
    """Combine recurring timetable entries with dated journal lessons without writing records."""
    week_end = week_start + datetime.timedelta(days=6)
    days = [{'date': week_start + datetime.timedelta(days=offset), 'name': name}
            for offset, name in WeeklyScheduleEntry.WEEKDAYS]
    slots = list(LessonTime.objects.filter(school=school))
    by_start = {slot.start_time: slot for slot in slots}
    rows = {}

    def row_for(time):
        key = time.isoformat() if time else ''
        if key not in rows:
            slot = by_start.get(time)
            rows[key] = {'slot': slot, 'time': time,
                         'label': str(slot) if slot else time.strftime('%H:%M') if time else 'Без часу',
                         'cells': [{'day': day, 'items': []} for day in days]}
        return rows[key]

    for slot in slots:
        if slot.is_active:
            row_for(slot.start_time)
    records = list(Lesson.objects.filter(journal__in=journals, date__range=(week_start, week_end))
                   .exclude(lesson_type__in=SUMMARY_TYPES)
                   .select_related('journal__student_class', 'journal__subject', 'journal__teacher')
                   .order_by('date', 'lesson_number', 'pk'))
    recorded = {(lesson.journal_id, lesson.date, lesson.lesson_time) for lesson in records}
    matching_entries = {}
    entries = WeeklyScheduleEntry.objects.filter(journal__in=journals, start_date__lte=week_end,
              end_date__gte=week_start).select_related('lesson_time', 'journal__student_class', 'journal__subject')
    for entry in entries:
        day = days[entry.weekday]['date']
        key = (entry.journal_id, day, entry.lesson_time.start_time)
        if not entry.start_date <= day <= entry.end_date:
            continue
        matching_entries[key] = entry
        if key in recorded:
            continue
        row_for(entry.lesson_time.start_time)['cells'][entry.weekday]['items'].append({
            'journal': entry.journal, 'date': day, 'room': entry.room, 'kind': 'planned',
            'can_create': entry.lesson_time.is_active and not entry.journal.is_archived,
            'edit_url': reverse('portal:weekly_schedule') +
                        f'?school_id={school.pk}&teacher_id={entry.journal.teacher_id}&year_id={entry.journal.academic_year_id}&week={week_start.isoformat()}&edit={entry.pk}#scheduleEditor',
            'url': reverse('portal:journal_detail', kwargs={'journal_id': entry.journal_id}),
            'create_url': reverse('portal:journal_detail', kwargs={'journal_id': entry.journal_id}) +
                          f'?create_date={day.isoformat()}&create_time={entry.lesson_time.start_time:%H:%M}',
        })
    for lesson in records:
        scheduled = matching_entries.get((lesson.journal_id, lesson.date, lesson.lesson_time))
        row_for(lesson.lesson_time)['cells'][lesson.date.weekday()]['items'].append({
            'journal': lesson.journal, 'date': lesson.date, 'kind': 'recorded', 'topic': lesson.topic,
            'room': scheduled.room if scheduled else '',
            'url': reverse('portal:journal_detail', kwargs={'journal_id': lesson.journal_id}) +
                   f'?month={lesson.date:%Y-%m}#lesson-row-{lesson.pk}',
        })
    return days, [rows[key] for key in sorted(rows, key=lambda key: (not bool(key), key))]
