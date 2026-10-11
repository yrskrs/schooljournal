"""Local journal cleanup; shared pupils, classes and online data are independent."""
from django.db import transaction

from apps.journals.models import Journal, Grade, Attendance, AuditLog, APIInboxResult, ExtraStudent, ExternalGrade


def journal_content_counts(journal):
    return {
        'lessons': journal.lessons.count(),
        'plan_topics': journal.plan_topics.count(),
        'grades': Grade.objects.filter(lesson__journal=journal).count(),
        'attendances': Attendance.objects.filter(lesson__journal=journal).count(),
        'summary_grades': journal.summary_grades.count(),
        'connections': journal.grade_apis.count(),
    }


def clear_journal_content(journal):
    counts = journal_content_counts(journal)
    journal.lessons.all().delete()
    journal.plan_topics.all().delete()
    journal.summary_grades.all().delete()
    journal.weekly_schedule.all().delete()
    journal.ignored_students.clear()
    return counts


@transaction.atomic
def delete_local_journal(journal, user):
    Journal.objects.select_for_update().get(pk=journal.pk)
    if ExtraStudent.objects.filter(separate_journal=journal).exists():
        raise ValueError('Цей журнал пов’язаний з позакласними учнями іншого журналу. Спочатку видаліть їхні позакласні записи на сторінці результатів.')
    if ExternalGrade.objects.filter(connection__journal=journal).exclude(lesson__journal=journal).exists():
        raise ValueError('Підключення цього журналу має імпортовані результати в іншому журналі. Спочатку перевірте їхні прив’язки.')
    counts = journal_content_counts(journal)
    journal_id = journal.pk
    label = str(journal)
    # Inbox results PROTECT extra identities, including during a cascade.
    APIInboxResult.objects.filter(connection__journal=journal).delete()
    journal.delete()
    AuditLog.objects.create(school_id=journal.school_id, user=user, action=AuditLog.Action.DELETE,
        target_model='Journal', target_id=str(journal_id),
        description=f'Видалено локальний журнал: {label}'[:255],
        changes={**counts, 'nz_journal_id': journal.nz_journal_id, 'online_unchanged': True})
    return counts
