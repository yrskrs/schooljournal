"""Teacher-facing class lists, independent of optional server peer settings."""
from django.db.models import Count

from apps.curriculum.models import ClassEnrollment, Student
from apps.journals.models import GradeAPIConnection, Journal
from apps.portal.permissions import get_user_journals, has_journal_access


def context(user, selected_class_id=None):
    journals = [journal for journal in get_user_journals(user).filter(is_archived=False)
                .select_related('nz_owner__user', 'local_profile')
                .order_by('-academic_year__start_date', 'student_class__grade_level', 'student_class__name', 'subject__name')
                if has_journal_access(user, journal)]
    classes = {}
    for journal in journals:
        classes.setdefault(journal.student_class_id, {
            'id': journal.student_class_id, 'name': journal.student_class.name,
            'year': journal.academic_year.name, 'school': journal.school.short_name or journal.school.name,
        })
    selected = next((row for row in classes.values() if str(row['id']) == str(selected_class_id)), None)
    if selected is None:
        selected = next(iter(classes.values()), None)
    selected_journals = [journal for journal in journals if selected and journal.student_class_id == selected['id']]
    ids = [journal.pk for journal in selected_journals]
    connections = {}
    for item in GradeAPIConnection.objects.filter(journal_id__in=ids, source__in=['schooltest', 'schoolwork']).order_by('pk'):
        item.roster_can_send = item.contract_version == 2 and 'journal_roster_import' in item.roster_cache.get('capabilities', [])
        connections.setdefault(item.journal_id, []).append(item)
    ignored = dict(Journal.objects.filter(pk__in=ids).annotate(total=Count('ignored_students')).values_list('pk', 'total'))
    for journal in selected_journals:
        journal.roster_connections = connections.get(journal.pk, [])
        journal.roster_ignored_count = ignored.get(journal.pk, 0)
        journal.roster_can_edit = has_journal_access(user, journal, write=True)
    return {
        'teacher_roster_classes': list(classes.values()), 'teacher_roster_selected_class': selected,
        'teacher_roster_journals': selected_journals,
        'teacher_roster_student_count': ClassEnrollment.objects.filter(student_class_id=selected['id'], is_active=True,
                                                                     student__status=Student.Status.ACTIVE).count() if selected else 0,
    }
