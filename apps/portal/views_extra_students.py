from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_GET
from django.core.paginator import Paginator
from apps.portal.permissions import get_user_journals, has_journal_access
from apps.curriculum.models import Student
from apps.journals.models import Journal, ExtraStudent
from apps.journals.services.assessment import journal_students
from apps.journals.services.extra_students import delete_separate_student, extra_parent
from apps.journals.services.external_grades import ExternalAPIError
from apps.portal.views_journal import _get_journal_or_403


@login_required
@require_GET
def outside_students(request):
    journals = [row for row in get_user_journals(request.user).select_related('nz_owner__user', 'local_profile')
                if has_journal_access(request.user, row)]
    extras = ExtraStudent.objects.filter(connection__journal_id__in=[row.pk for row in journals],
        alias_of__isnull=True, matched_student__isnull=True).select_related(
            'connection__journal__student_class', 'connection__journal__subject', 'separate_journal')
    search = request.GET.get('q', '').strip()
    source = request.GET.get('source', '')
    if search:
        extras = extras.filter(name__icontains=search)
    if source in ('schoolwork', 'schooltest'):
        extras = extras.filter(connection__source=source)
    groups = {}
    for extra in extras:
        results = list(extra.results.filter(is_ignored=False).order_by('-updated_at')[:5])
        if not results and extra.results.exists():
            continue
        connection = extra.connection
        identity = (connection.source, connection.roster_cache.get('instance_id') or connection.pk, extra.external_key)
        group = groups.setdefault(identity, {'name': extra.name, 'source': connection.get_source_display(), 'contexts': []})
        group['contexts'].append({'extra': extra, 'results': results})
    return render(request, 'portal/journal/outside_students.html', {
        'page_obj': Paginator(list(groups.values()), 25).get_page(request.GET.get('page')),
        'search': search, 'source': source,
    })


@login_required
@require_POST
def delete_extracurricular_student(request, journal_id, student_id):
    with transaction.atomic():
        _get_journal_or_403(request.user, journal_id, write=True)
        journal = Journal.objects.get(pk=journal_id)
        parent = extra_parent(journal)
        # Import locks the source journal before its separate table.
        if parent:
            Journal.objects.select_for_update().get(pk=parent.pk)
        Journal.objects.select_for_update().get(pk=journal.pk)
        _get_journal_or_403(request.user, journal_id, write=True)
        journal = Journal.objects.get(pk=journal_id)
        list(ExtraStudent.objects.select_for_update().filter(separate_journal=journal, separate_student_id=student_id))
        student = get_object_or_404(Student.objects.select_for_update(),
            pk=student_id, school=journal.school, pk__in=journal_students(journal).values('pk'))
        try:
            if request.POST.get('confirm_delete') != 'on':
                raise ExternalAPIError('Підтвердьте видалення позакласного учня та його оцінок.')
            from apps.portal.views_api_inbox import _same_revision
            if not _same_revision(student.updated_at, request.POST.get('version')):
                raise ExternalAPIError('Профіль змінився після перегляду. Оновіть сторінку.')
            name = student.get_full_name_ukr()
            delete_separate_student(journal, student, request.user)
            messages.success(request, f'Позакласного учня «{name}» та його локальні записи видалено. Дію записано в історії.')
        except ExternalAPIError as error:
            messages.error(request, str(error))
    return redirect('portal:journal_detail', journal_id=journal_id)
