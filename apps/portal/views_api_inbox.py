import datetime as dt
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

from apps.curriculum.models import StudentClass, Student, ClassEnrollment
from apps.journals.models import ExtraStudent, APIInboxResult, Journal, Lesson, Grade
from apps.journals.services.assessment import journal_students
from apps.journals.services.external_grades import import_grade_records, ExternalAPIError
from apps.portal.permissions import has_journal_access
from apps.portal.views_journal import _get_journal_or_403
from school_sync.journal_roster import similar


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
                if action == 'match':
                    extra = get_object_or_404(ExtraStudent.objects.select_for_update(),
                        pk=request.POST.get('extra_id') if str(request.POST.get('extra_id', '')).isdigit() else 0,
                        connection__journal=journal)
                    if not _same_revision(extra.updated_at, request.POST.get('version')):
                        raise ExternalAPIError('Ім’я оновилося після перегляду. Перевірте його ще раз.')
                    choice = request.POST.get('student')
                    student = journal_students(journal).filter(pk=int(choice)).first() if str(choice or '').isdigit() else None
                    if choice and not student:
                        raise ExternalAPIError('Оберіть учня саме цього журналу.')

                    if choice:
                        if extra.matched_student_id and extra.matched_student_id != student.pk:
                            ids = extra.results.values_list('external_id', flat=True)
                            if extra.connection.records.filter(external_id__in=ids).exists():
                                raise ExternalAPIError('Відповідність уже має імпортовану історію. Переприв’язування заборонено.')
                        extra.matched_student = student
                        extra.save(update_fields=['matched_student', 'updated_at'])
                        messages.success(request, 'Відповідність збережено. Для оцінок натисніть синхронізацію або «Додати збережений результат».')
                    else:
                        # Вчитель обрав пункт «Залишити позакласним»: створюємо окремий журнал (таблицю) для цих учнів
                        extra_class_name = f"{journal.student_class.name} (Позакласні)"
                        extra_class, _ = StudentClass.objects.get_or_create(
                            school=journal.school,
                            academic_year=journal.academic_year,
                            name=extra_class_name,
                            defaults={
                                'grade_level': journal.student_class.grade_level,
                                'letter': 'ПК',
                            }
                        )
                        extra_journal, _ = Journal.objects.get_or_create(
                            school=journal.school,
                            academic_year=journal.academic_year,
                            student_class=extra_class,
                            subject=journal.subject,
                            defaults={
                                'teacher': journal.teacher,
                            }
                        )
                        names = extra.name.strip().split(None, 1)
                        last_name = names[0]
                        first_name = names[1] if len(names) > 1 else ''
                        ext_student, _ = Student.objects.get_or_create(
                            school=journal.school,
                            last_name=last_name,
                            first_name=first_name or last_name,
                            defaults={'notes': f'Позакласний учень ({journal.student_class.name})'}
                        )
                        ClassEnrollment.objects.get_or_create(
                            student=ext_student,
                            student_class=extra_class,
                            defaults={'is_active': True}
                        )
                        # Переносимо результати учня в окремий журнал позакласних
                        for res in extra.results.filter(resolved=False, is_ignored=False):
                            payload = res.payload
                            d_str = payload.get('date')
                            val = payload.get('value')
                            if d_str and val is not None:
                                try:
                                    lesson_date = dt.date.fromisoformat(d_str)
                                    lesson, _ = Lesson.objects.get_or_create(
                                        journal=extra_journal,
                                        date=lesson_date,
                                        defaults={
                                            'lesson_number': 1,
                                            'topic': payload.get('title') or 'Позакласна робота',
                                        }
                                    )
                                    Grade.objects.get_or_create(
                                        lesson=lesson,
                                        student=ext_student,
                                        defaults={'value': str(val), 'is_approved': True}
                                    )
                                    res.resolved = True
                                    res.save(update_fields=['resolved', 'updated_at'])
                                except Exception:
                                    pass

                        extra.matched_student = None
                        extra.save(update_fields=['matched_student', 'updated_at'])
                        messages.success(
                            request,
                            f'Учня «{extra.name}» залишено позакласним та створено окремий журнал «{extra_journal.student_class.name}».'
                        )
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
                        lesson = journal.lessons.filter(pk=int(lesson_id)).first() if str(lesson_id).isdigit() else None
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
        return redirect('portal:api_inbox', journal_id=journal.pk)

    students = list(journal_students(journal))
    show_all = request.GET.get('all') == '1'
    visible_results = APIInboxResult.objects.filter(extra_student=OuterRef('pk'), is_ignored=False)
    if not show_all:
        visible_results = visible_results.filter(resolved=False)
    extras = ExtraStudent.objects.filter(connection__journal=journal).select_related('connection', 'matched_student')
    extras = extras.annotate(
        has_results=Exists(APIInboxResult.objects.filter(extra_student=OuterRef('pk'))),
        has_visible_results=Exists(visible_results),
    ).filter(Q(has_results=False) | Q(has_visible_results=True))
    if not show_all:
        extras = extras.filter(matched_student__isnull=True)
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
        result.lessons = list(journal.lessons.filter(date=result.payload.get('date')))

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
        'extra_page': page,
        'result_page': result_page,
        'ignored_results': ignored_results,
        'extra_journal': extra_journal,
        'students': students,
        'show_all': request.GET.get('all') == '1',
        'can_edit': has_journal_access(request.user, journal, write=True)
    })
