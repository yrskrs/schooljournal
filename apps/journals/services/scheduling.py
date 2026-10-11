"""Local scheduling operations preserve lesson IDs, marks and NZ bindings."""
from datetime import date, timedelta
from django.db import transaction
from django.db.models import Max

from apps.journals.models import Journal, Lesson, LessonPlanTopic, LessonType


SUMMARY_TYPES = {
    LessonType.THEMATIC, LessonType.MODULAR, LessonType.THEMATIC_NUSH,
    LessonType.SEMESTER_1, LessonType.SEMESTER_2,
    LessonType.SEMESTER_1_NUSH, LessonType.SEMESTER_2_NUSH, LessonType.ANNUAL,
}
MAX_REPEATED_LESSONS = 100


def lesson_occurrence_dates(start, frequency='none', *, interval_weeks=None, count=None, until=None, academic_year=None):
    """Finite, inclusive local recurrence; no background jobs or remote writes."""
    if not frequency or frequency == 'none':
        return [start]
    interval = {'weekly': 1, 'biweekly': 2}.get(frequency)
    if frequency == 'custom':
        interval = interval_weeks
    if not interval or not 1 <= interval <= 52 or frequency not in ('weekly', 'biweekly', 'custom'):
        raise ValueError('Оберіть повторення кожні 1–52 тижні.')
    if count is not None:
        if not 2 <= count <= MAX_REPEATED_LESSONS:
            raise ValueError('Кількість уроків у серії має бути від 2 до 100, включно з першим.')
    elif until is None or until < start:
        raise ValueError('Дата завершення повторень має бути не раніше першого уроку.')
    if academic_year and (start < academic_year.start_date or start > academic_year.end_date or
                          (until is not None and until > academic_year.end_date)):
        raise ValueError('Повторення можна створити лише в межах навчального року.')
    if count is None:
        count = (until - start).days // (7 * interval) + 1
        if count > MAX_REPEATED_LESSONS:
            raise ValueError('За один раз можна створити не більше 100 уроків.')
    try:
        dates = [start + timedelta(weeks=interval * i) for i in range(count)]
    except OverflowError:
        raise ValueError('Дати повторення виходять за допустимий діапазон.') from None
    if academic_year and dates[-1] > academic_year.end_date:
        raise ValueError('Останній урок виходить за межі навчального року. Зменште кількість або оберіть кінцеву дату.')
    return dates


def copy_repeated_homework(first, lesson):
    """Copy the assignment with the same day offset; preserve automatic next."""
    if first.homework_for_next_lesson:
        lesson.homework_for_next_lesson = True
        return
    due = first.get_homework_due_lesson()
    due_date = due.date if due else first.homework_due_date
    if not due_date and first.homework_due_plan_topic_id:
        due_date = first.homework_due_plan_topic.planned_date
    if due_date:
        try:
            lesson.homework_due_date = due_date + (lesson.date - first.date)
        except OverflowError:
            raise ValueError('Дата виконання повтореного ДЗ виходить за допустимий діапазон.') from None
    elif first.homework_due_plan_topic_id:
        lesson.homework_due_plan_topic_id = first.homework_due_plan_topic_id


def next_lesson_number(journal, lesson_date):
    number = (journal.lessons.filter(date=lesson_date).aggregate(last=Max('lesson_number'))['last'] or 0) + 1
    if number > 32767:
        raise ValueError('На цю дату перевищено кількість уроків.')
    return number


def set_homework_target(lesson, target):
    previous_date = lesson.homework_due_date
    lesson.homework_due_lesson = None
    lesson.homework_due_plan_topic = None
    lesson.homework_due_date = None
    if target == 'none':
        lesson.homework_for_next_lesson = False
        return
    if not target or target == 'next':
        lesson.homework_for_next_lesson = True
        return
    lesson.homework_for_next_lesson = False
    kind, _, raw_id = target.partition(':')
    if kind == 'date':
        try:
            due_date = date.fromisoformat(raw_id)
        except ValueError:
            raise ValueError('Некоректна дата виконання домашнього завдання.') from None
        # Retain an imported assignment; new choices come from this journal's
        # lesson days (including the selected day) and dated calendar plan.
        if due_date != previous_date and (
            due_date < lesson.date or
            (due_date != lesson.date and
             not lesson.journal.lessons.filter(date=due_date).exclude(lesson_type__in=SUMMARY_TYPES).exists() and
             not lesson.journal.plan_topics.filter(planned_date=due_date, is_conducted=False).exclude(lesson_type__in=SUMMARY_TYPES).exists())
        ):
            raise ValueError('Оберіть день уроку цього журналу, починаючи з вибраної дати.')
        lesson.homework_due_date = due_date
        return
    if not raw_id.isdigit():
        raise ValueError('Некоректний урок для домашнього завдання.')
    if kind == 'lesson':
        due = Lesson.objects.filter(journal=lesson.journal, pk=raw_id).first()
        if not due or due.pk == lesson.pk or (due.date, due.lesson_number) <= (lesson.date, lesson.lesson_number):
            raise ValueError('Домашнє завдання можна задати лише на наступний урок цього журналу.')
        lesson.homework_due_lesson = due
    elif kind == 'plan':
        due = LessonPlanTopic.objects.filter(journal=lesson.journal, pk=raw_id, is_conducted=False).first()
        if not due or (due.planned_date and due.planned_date < lesson.date):
            raise ValueError('Оберіть майбутню тему цього календарного плану.')
        lesson.homework_due_plan_topic = due
    else:
        raise ValueError('Некоректний вибір домашнього завдання.')


def insert_day_slot(journal, lesson_date, number):
    """Caller holds the journal lock. Descending updates respect unique slots."""
    lessons = list(journal.lessons.filter(date=lesson_date, lesson_number__gte=number).order_by('-lesson_number'))
    if lessons and lessons[0].lesson_number >= 32767:
        raise ValueError('На цю дату перевищено кількість уроків.')
    for lesson in lessons:
        lesson.lesson_number += 1
        lesson.save(update_fields=['lesson_number', 'updated_at'])


@transaction.atomic
def swap_topics(journal, first_id, second_id):
    Journal.objects.select_for_update().get(pk=journal.pk)
    lessons = {l.pk: l for l in Lesson.objects.select_for_update().filter(journal=journal, pk__in=[first_id, second_id])}
    if first_id == second_id or len(lessons) != 2:
        raise ValueError('Оберіть два різні уроки цього журналу.')
    first, second = lessons[first_id], lessons[second_id]
    if first.lesson_type in SUMMARY_TYPES or second.lesson_type in SUMMARY_TYPES:
        raise ValueError('Підсумкові колонки не можна обмінювати зі звичайними темами.')
    fields = ['topic', 'homework', 'notes', 'linked_plan_topic_id', 'homework_due_lesson_id',
              'homework_due_plan_topic_id', 'homework_for_next_lesson', 'homework_due_date']
    for field in fields:
        old = getattr(first, field)
        setattr(first, field, getattr(second, field))
        setattr(second, field, old)
    for lesson in (first, second):
        if lesson.homework_due_date and lesson.homework_due_date < lesson.date:
            lesson.homework_due_date = None
            lesson.homework_for_next_lesson = True
        if lesson.homework_due_lesson_id:
            due = lesson.homework_due_lesson
            if (due.date, due.lesson_number) <= (lesson.date, lesson.lesson_number):
                lesson.homework_due_lesson = None
                lesson.homework_for_next_lesson = True
        lesson.is_synced_to_nz = False
        lesson.save()
        if lesson.linked_plan_topic_id:
            lesson.linked_plan_topic.planned_date = lesson.date
            lesson.linked_plan_topic.save(update_fields=['planned_date', 'updated_at'])
    return first, second


@transaction.atomic
def reorder_plan_topic(journal, topic_id, direction, planned_date):
    Journal.objects.select_for_update().get(pk=journal.pk)
    topics = list(journal.plan_topics.select_for_update().order_by('topic_number', 'pk'))
    index = next((i for i, t in enumerate(topics) if t.pk == topic_id and not t.is_conducted), None)
    if index is None:
        raise ValueError('Тему вже проведено або її немає в цьому журналі.')
    topic = topics[index]
    if direction:
        if direction not in ('up', 'down'):
            raise ValueError('Некоректний напрям переміщення.')
        other = index + (-1 if direction == 'up' else 1)
        if 0 <= other < len(topics) and not topics[other].is_conducted:
            topic.topic_number, topics[other].topic_number = topics[other].topic_number, topic.topic_number
            topics[other].save(update_fields=['topic_number', 'updated_at'])
    else:
        topic.planned_date = planned_date
    topic.save()
    return topic
