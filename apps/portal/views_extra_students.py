import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.curriculum.models import Student
from apps.journals.models import APIInboxResult, ExtraStudent, Journal
from apps.journals.services.assessment import journal_students
from apps.journals.services.external_grades import ExternalAPIError
from apps.journals.services.extra_students import delete_separate_student, extra_parent
from apps.portal.permissions import get_user_journals, has_journal_access
from apps.portal.views_journal import _get_journal_or_403


@login_required
@require_GET
def outside_students(request):
    journals = [row for row in get_user_journals(request.user).select_related('nz_owner__user', 'local_profile')
                if has_journal_access(request.user, row)]
    journal_id = request.GET.get('journal', '')
    selected_journal = None
    legacy_by_parent = {}
    main_journals = []
    accessible = {journal.pk for journal in journals}
    for journal in journals:
        parent = extra_parent(journal)
        if parent:
            if parent.pk in accessible:
                legacy_by_parent.setdefault(parent.pk, []).append(journal)
        else:
            main_journals.append(journal)
    if journal_id:
        selected_journal = next((row for row in main_journals if str(row.pk) == journal_id), None)
        if selected_journal is None:
            raise PermissionDenied('Вибраний журнал недоступний. Оберіть свій основний журнал.')
    visible_journals = [selected_journal] if selected_journal else main_journals
    # Disabled connections stop fetching new data; they do not erase saved
    # identities, including canonical targets of aliases from newer connections.
    extras = ExtraStudent.objects.filter(connection__journal_id__in=[row.pk for row in visible_journals],
        alias_of__isnull=True, matched_student__isnull=True).select_related(
            'connection__journal__student_class', 'connection__journal__subject', 'connection__journal__academic_year', 'separate_journal', 'separate_student')
    search = request.GET.get('q', '').strip()
    source = request.GET.get('source', '')
    if search:
        extras = extras.filter(name__icontains=search)
    if source in ('schoolwork', 'schooltest'):
        extras = extras.filter(Q(connection__source=source) | Q(results__connection__source=source)).distinct()
    else:
        source = ''
    visible_results = APIInboxResult.objects.filter(is_ignored=False).select_related('connection').order_by('-updated_at', '-pk')
    if source:
        visible_results = visible_results.filter(connection__source=source)
    extras = extras.annotate(has_results=Exists(APIInboxResult.objects.filter(extra_student=OuterRef('pk'))),
        has_visible_results=Exists(visible_results.filter(extra_student=OuterRef('pk')))).filter(Q(has_results=False) | Q(has_visible_results=True))
    extras = extras.prefetch_related(Prefetch('results', queryset=visible_results, to_attr='visible_results'))
    month = request.GET.get('month', '').strip()
    try:
        if month:
            dt.date.fromisoformat(month + '-01')
    except ValueError:
        month = ''
    semesters = list(selected_journal.academic_year.semesters.all().order_by('number')) if selected_journal else []
    semester_id = request.GET.get('semester_id', 'all')
    semester = next((row for row in semesters if str(row.pk) == semester_id), None)
    groups = {}
    for extra in extras:
        results = extra.visible_results
        connection = extra.connection
        identity = (connection.source, connection.roster_cache.get('instance_id') or connection.pk, extra.external_key)
        group = groups.setdefault(identity, {'name': extra.name, 'identity': identity,
            'source': connection.get_source_display(), 'source_names': set(), 'contexts': []})
        group['source_names'].update(result.connection.get_source_display() for result in results)
        if not results:
            group['source_names'].add(connection.get_source_display())
        group['source'] = ', '.join(sorted(group['source_names']))
        group['contexts'].append({'extra': extra, 'results': results[:5], 'all_results': results})
    page = Paginator(list(groups.values()), 25).get_page(request.GET.get('page'))
    from apps.journals.services.outside_gradebook import build_outside_tables
    tables = build_outside_tables(page.object_list, visible_journals, legacy_by_parent,
        search=search, source=source, month=month, semester=semester)
    if selected_journal and not tables:
        # Render the real calendar even when no outside pupils have results yet.
        lessons = list(selected_journal.lessons.order_by('date', 'lesson_number'))
        lessons = [lesson for lesson in lessons if (not month or lesson.date.strftime('%Y-%m') == month)
            and (not semester or semester.start_date <= lesson.date <= semester.end_date)]
        tables = [{'journal': selected_journal, 'columns': [{'date': lesson.date, 'number': lesson.lesson_number,
            'topic': lesson.topic, 'is_lesson': True} for lesson in lessons], 'rows': [],
            'column_count': max(len(lessons), 1) + 3, 'legacy_journals': legacy_by_parent.get(selected_journal.pk, [])}]
    return render(request, 'portal/journal/outside_students.html', {
        'page_obj': page, 'tables': tables, 'journals': main_journals,
        'search': search, 'source': source,
        'selected_journal': journal_id, 'journal': selected_journal,
        'semesters': semesters, 'selected_semester': semester,
        'selected_semester_param': semester.pk if semester else 'all', 'selected_month': month,
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
