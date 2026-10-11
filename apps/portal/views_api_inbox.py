from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from apps.journals.models import ExtraStudent, APIInboxResult, Journal, AuditLog
from apps.journals.services.assessment import journal_students
from apps.journals.services.external_grades import import_grade_records, ExternalAPIError
from apps.portal.permissions import has_journal_access
from apps.portal.views_journal import _get_journal_or_403
from school_sync.journal_roster import similar
from apps.journals.services.extra_students import match_student, keep_separate, delete_extra


def _same_revision(updated_at, submitted):
    """Templates localize date:c; compare instants rather than ISO strings."""
    try:
        value = parse_datetime(submitted or '')
    except (TypeError, ValueError):
        return False
    return value is not None and timezone.is_aware(value) and value == updated_at


@login_required
@require_http_methods(['GET', 'POST'])
def api_inbox(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=request.method == 'POST')
    if request.method == 'POST':
        try:
            with transaction.atomic():
                journal = Journal.objects.select_for_update().get(pk=journal.pk)
                if not has_journal_access(request.user, journal, write=True):
                    raise PermissionDenied('Доступ до журналу змінився.')
                action = request.POST.get('action')
                if action in ('match', 'keep_separate', 'delete_extra'):
                    extra = get_object_or_404(ExtraStudent.objects.select_for_update(),
                        pk=request.POST.get('extra_id') if str(request.POST.get('extra_id', '')).isdigit() else 0,
                        connection__journal=journal)
                    if not _same_revision(extra.updated_at, request.POST.get('version')):
                        raise ExternalAPIError('Запис оновився після перегляду. Оновіть сторінку й повторіть дію.')
                    choice = request.POST.get('student')
                    if action == 'delete_extra':
                        if request.POST.get('confirm_delete') != 'on':
                            raise ExternalAPIError('Підтвердьте видалення позакласного учня та його збережених результатів.')
                        name = extra.name
                        count, preserved = delete_extra(extra, request.user)
                        text = f'Позакласний запис «{name}» і результати ({count}) видалено. Дію записано в історії. Цей запис не завантажиться повторно.'
                        if preserved:
                            text += f' Оцінки підтвердженого учня класу збережено: {preserved}.'
                        messages.success(request, text)
                    elif action == 'keep_separate' or (action == 'match' and choice == ''):
                        separate, created = keep_separate(extra, request.user)
                        messages.success(request, f'Учня «{extra.name}» збережено в окремому журналі «{separate.student_class.name}». Оцінки додайте окремою кнопкою після створення уроку на дату роботи.')
                    else:
                        student = journal_students(journal).filter(pk=int(choice)).first() if str(choice or '').isdigit() else None
                        if not student:
                            raise ExternalAPIError('Оберіть учня цього журналу для підтвердження відповідності.')
                        match_student(extra, student, request.user)
                        # The identity is confirmed; grade writes remain an explicit separate action.
                        extra.results.filter(resolved=False, is_ignored=False).update(reason='Відповідність підтверджено. Додайте збережений результат або натисніть синхронізацію оцінок.')
                        messages.success(request, f'Відповідність підтверджено: «{extra.name}» → «{student.get_full_name_ukr()}». Оцінки додайте кнопкою синхронізації або «Додати збережений результат».')
                elif action == 'ignore_result':
                    result = get_object_or_404(APIInboxResult.objects.select_for_update(),
                        pk=request.POST.get('result_id') if str(request.POST.get('result_id', '')).isdigit() else 0,
                        connection__journal=journal)
                    if request.POST.get('version') and not _same_revision(result.updated_at, request.POST['version']):
                        raise ExternalAPIError('Результат змінився після перегляду. Оновіть сторінку.')
                    result.is_ignored = True
                    result.resolved = True
                    result.ignored_at = timezone.now()
                    result.save(update_fields=['is_ignored', 'resolved', 'ignored_at', 'updated_at'])
                    messages.success(request, f"Запис за {result.payload.get('date', 'цю дату')} переміщено в ігноровані.")
                elif action == 'unignore_result':
                    result = get_object_or_404(APIInboxResult.objects.select_for_update(),
                        pk=request.POST.get('result_id') if str(request.POST.get('result_id', '')).isdigit() else 0,
                        connection__journal=journal)
                    if request.POST.get('version') and not _same_revision(result.updated_at, request.POST['version']):
                        raise ExternalAPIError('Результат змінився після перегляду. Оновіть сторінку.')
                    result.is_ignored = False
                    result.resolved = False
                    result.ignored_at = None
                    result.save(update_fields=['is_ignored', 'resolved', 'ignored_at', 'updated_at'])
                    messages.success(request, f"Запис за {result.payload.get('date', 'цю дату')} повернуто з ігнорованих.")
                elif action == 'apply_saved':
                    result = get_object_or_404(APIInboxResult.objects.select_for_update(),
                        pk=request.POST.get('result_id') if str(request.POST.get('result_id', '')).isdigit() else 0,
                        connection__journal=journal)
                    if result.is_ignored:
                        raise ExternalAPIError('Цей результат ігноровано. Спочатку відновіть його зі списку ігнорованих.')
                    if not _same_revision(result.updated_at, request.POST.get('version')):
                        raise ExternalAPIError('Результат змінився після перегляду. Оновіть сторінку.')
                    payload = dict(result.payload)
                    lesson_id = request.POST.get('lesson')
                    if lesson_id:
                        target_journal = result.extra_student.separate_journal if result.extra_student and result.extra_student.separate_journal_id else journal
                        lesson = target_journal.lessons.filter(pk=int(lesson_id)).first() if str(lesson_id).isdigit() else None
                        if not lesson or lesson.date.isoformat() != payload.get('date'):
                            raise ExternalAPIError('Оберіть урок цього журналу на початкову дату результату.')
                        payload['lesson_number'] = lesson.lesson_number
                    log = import_grade_records(result.connection, [payload], request.user,
                        journal.academic_year.start_date, journal.academic_year.end_date)
                    if log.counts['errors']:
                        messages.warning(request, log.errors[0]['message'])
                    else:
                        messages.success(request, f"Додано {log.counts['added']}, оновлено {log.counts['updated']}, конфлікти {log.counts['conflicts']}.")
                else:
                    raise ExternalAPIError('Оберіть дію зі списку.')
        except ExternalAPIError as error:
            messages.error(request, str(error))
        url = request.path + ('?all=1' if request.GET.get('all') == '1' else '')
        return redirect(url)

    students = list(journal_students(journal))
    show_all = request.GET.get('all') == '1'
    visible_results = APIInboxResult.objects.filter(extra_student=OuterRef('pk'), is_ignored=False)
    if not show_all:
        visible_results = visible_results.filter(resolved=False)
    extras = ExtraStudent.objects.filter(connection__journal=journal).select_related('connection', 'matched_student', 'separate_student', 'separate_journal')
    extras = extras.annotate(
        has_results=Exists(APIInboxResult.objects.filter(extra_student=OuterRef('pk'))),
        has_visible_results=Exists(visible_results),
    ).filter(Q(has_results=False) | Q(has_visible_results=True))
    if not show_all:
        extras = extras.filter(matched_student__isnull=True, separate_student__isnull=True)
    page = Paginator(extras, 50).get_page(request.GET.get('page'))
    for extra in page:
        extra.suggestions = [s.get_full_name_ukr() for s in students if similar(extra.name, s.get_full_name_ukr())][:5]
        results = extra.results.filter(is_ignored=False)
        if not show_all:
            results = results.filter(resolved=False)
        extra.recent_results = list(results.order_by('-updated_at')[:5])

    inbox = APIInboxResult.objects.filter(
        connection__journal=journal, resolved=False, is_ignored=False
    ).select_related('connection', 'extra_student')
    result_page = Paginator(inbox, 50).get_page(request.GET.get('results_page'))
    for result in result_page:
        result.target_journal = result.extra_student.separate_journal if result.extra_student and result.extra_student.separate_journal_id else journal
        result.lessons = list(result.target_journal.lessons.filter(date=result.payload.get('date')))

    ignored_results = APIInboxResult.objects.filter(
        connection__journal=journal, is_ignored=True
    ).select_related('connection', 'extra_student').order_by('-ignored_at', '-updated_at')
    ignored_results = Paginator(ignored_results, 50).get_page(request.GET.get('ignored_page'))

    extra_journal = Journal.objects.filter(
        school=journal.school,
        academic_year=journal.academic_year,
        student_class__name=f"{journal.student_class.name} (Позакласні)",
        subject=journal.subject,
        teacher=journal.teacher,
    ).first()

    return render(request, 'portal/journal/api_inbox.html', {
        'journal': journal,
        'decision_logs': AuditLog.objects.filter(school=journal.school, target_model__in=['ExtraStudent', 'Student'], changes__journal_id=journal.pk).select_related('user')[:20],
        'extra_page': page,
        'result_page': result_page,
        'ignored_results': ignored_results,
        'extra_journal': extra_journal,
        'students': students,
        'show_all': request.GET.get('all') == '1',
        'can_edit': has_journal_access(request.user, journal, write=True)
    })
