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

from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import ExtraStudent, APIInboxResult, Journal, AuditLog, Lesson, LessonType
from apps.journals.services.assessment import journal_students
from apps.journals.services.external_grades import import_grade_records, ExternalAPIError
from apps.portal.permissions import has_journal_access
from apps.portal.views_journal import _get_journal_or_403
from school_sync.journal_roster import similar
from apps.journals.services.extra_students import match_student, keep_separate, delete_extra, merge_extra


def _same_revision(updated_at, submitted):
    """Templates localize date:c; compare instants rather than ISO strings."""
    try:
        value = parse_datetime(submitted or '')
    except (TypeError, ValueError):
        return False
    return value is not None and timezone.is_aware(value) and value == updated_at


def find_suggested_student(extra_name, students):
    if not extra_name or not students:
        return None
    name_clean = ' '.join(extra_name.strip().casefold().split())
    parts = name_clean.split()
    if not parts:
        return None

    # 1. Exact full name match
    for s in students:
        s_full = ' '.join(s.get_full_name_ukr().strip().casefold().split())
        if s_full == name_clean:
            return s

    # 2. Exact (last_name + first_name)
    if len(parts) >= 2:
        for s in students:
            s_last = s.last_name.strip().casefold()
            s_first = s.first_name.strip().casefold()
            if (parts[0] == s_last and parts[1] == s_first) or (parts[0] == s_first and parts[1] == s_last):
                return s
            if parts[0] == s_last and s_first and parts[1][0] == s_first[0]:
                return s

    # 3. Surname match only (if unique in this class!)
    matching_surname = [s for s in students if s.last_name.strip().casefold() == parts[0] or (len(parts) > 1 and s.last_name.strip().casefold() == parts[-1])]
    if len(matching_surname) == 1:
        return matching_surname[0]

    # 4. Fuzzy similarity
    candidates = [s for s in students if similar(extra_name, s.get_full_name_ukr())]
    if len(candidates) == 1:
        return candidates[0]

    return None


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
                if action == 'batch_match':
                    count = 0
                    class_students = list(journal_students(journal))
                    unmatched_extras = ExtraStudent.objects.select_for_update().filter(
                        connection__journal=journal, alias_of__isnull=True, matched_student__isnull=True, separate_student__isnull=True
                    )
                    for ext in unmatched_extras:
                        sug = find_suggested_student(ext.name, class_students)
                        if sug:
                            match_student(ext, sug, request.user)
                            ext.results.filter(resolved=False, is_ignored=False).update(
                                reason='Відповідність підтверджено автоматично. Оцінки синхронізуються.'
                            )
                            count += 1
                    messages.success(request, f'Успішно підтверджено {count} учнів за прізвищами! Надалі вони синхронізуватимуться автоматично без запиту.')
                elif action == 'unmatch':
                    extra = get_object_or_404(ExtraStudent.objects.select_for_update(),
                        pk=request.POST.get('extra_id') if str(request.POST.get('extra_id', '')).isdigit() else 0,
                        connection__journal=journal, alias_of__isnull=True)
                    if not _same_revision(extra.updated_at, request.POST.get('version')):
                        raise ExternalAPIError('Запис оновився після перегляду. Оновіть сторінку.')
                    name = extra.name
                    if extra.matched_student_id:
                        from apps.journals.models import GradeStudentMapping
                        GradeStudentMapping.objects.filter(connection=extra.connection, external_id=extra.external_key).delete()
                        for alias in extra.aliases.all():
                            GradeStudentMapping.objects.filter(connection=alias.connection, external_id=alias.external_key).delete()
                        extra.matched_student = None
                        extra.is_separate = True
                        extra.save(update_fields=['matched_student', 'is_separate', 'updated_at'])
                        extra.results.filter(is_ignored=False).update(resolved=False, reason='Відповідність скасовано. Оберіть учня знову.')
                        messages.success(request, f'Відповідність для «{name}» скасовано. Запис повернуто до черги.')
                    elif extra.separate_student_id:
                        extra.separate_student = None
                        extra.separate_journal = None
                        extra.is_separate = True
                        extra.save(update_fields=['separate_student', 'separate_journal', 'is_separate', 'updated_at'])
                        extra.results.filter(is_ignored=False).update(resolved=False, reason='Позакласний статус скасовано. Запис повернуто до черги.')
                        messages.success(request, f'Позакласний статус для «{name}» скасовано. Запис повернуто до черги.')
                elif action in ('match', 'keep_separate', 'delete_extra', 'merge_extra'):
                    extra = get_object_or_404(ExtraStudent.objects.select_for_update(),
                        pk=request.POST.get('extra_id') if str(request.POST.get('extra_id', '')).isdigit() else 0,
                        connection__journal=journal, alias_of__isnull=True)
                    if not _same_revision(extra.updated_at, request.POST.get('version')):
                        raise ExternalAPIError('Запис оновився після перегляду. Оновіть сторінку й повторіть дію.')
                    choice = request.POST.get('student')
                    if action == 'merge_extra':
                        if request.POST.get('confirm_same_student') != 'on':
                            raise ExternalAPIError('Підтвердьте, що це одна й та сама дитина. Однакового ПІБ недостатньо.')
                        target_choice, _, selected_version = request.POST.get('target_extra', '').partition('|')
                        target = get_object_or_404(ExtraStudent.objects.select_for_update(),
                            pk=target_choice if target_choice.isdigit() else 0,
                            connection=extra.connection, alias_of__isnull=True)
                        if not _same_revision(target.updated_at, selected_version or request.POST.get('target_version')):
                            raise ExternalAPIError('Цільовий запис оновився. Оновіть сторінку.')
                        merge_extra(extra, target, request.user)
                        messages.success(request, 'Записи об’єднано за вашим підтвердженням. Роботи та оцінки збережено.')
                    elif action == 'delete_extra':
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
                        messages.success(request, f'Учня «{extra.name}» додано до журналу позакласних учнів «{separate.student_class.name}». Теми уроків та оцінки перенесено.')
                    else:
                        student = Student.objects.filter(school=journal.school, pk=int(choice)).first() if str(choice or '').isdigit() else None
                        if not student:
                            raise ExternalAPIError('Оберіть учня цього журналу або школи для підтвердження відповідності.')
                        match_student(extra, student, request.user)
                        is_current = journal_students(journal).filter(pk=student.pk).exists()
                        if is_current:
                            extra.results.filter(resolved=False, is_ignored=False).update(
                                reason='Відповідність підтверджено. Додайте збережений результат або натисніть синхронізацію оцінок.'
                            )
                            messages.success(request, f'Відповідність підтверджено: «{extra.name}» → «{student.get_full_name_ukr()}». Оцінки додайте кнопкою синхронізації або «Додати збережений результат».')
                        else:
                            enr = ClassEnrollment.objects.filter(student=student, is_active=True).select_related('student_class').first()
                            cls_name = enr.student_class.name if enr else 'іншого класу'
                            other_j = Journal.objects.filter(
                                school=journal.school,
                                academic_year=journal.academic_year,
                                student_class=enr.student_class if enr else None,
                                subject=journal.subject,
                                teacher=journal.teacher,
                            ).first() if enr else None
                            if other_j:
                                extra.results.filter(resolved=False, is_ignored=False).update(
                                    resolved=True,
                                    reason=f'Учень належить до класу {cls_name}. Журнал: {other_j.student_class.name}.'
                                )
                                messages.success(request, f'Відповідність підтверджено: «{extra.name}» → «{student.get_full_name_ukr()} ({cls_name})». Учень закріплений за класом {cls_name}.')
                            else:
                                extra.results.filter(resolved=False, is_ignored=False).update(
                                    resolved=True,
                                    reason=f'Учень належить до класу {cls_name}.'
                                )
                                messages.success(request, f'Відповідність підтверджено: «{extra.name}» → «{student.get_full_name_ukr()} ({cls_name})».')
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
                    target_journal = journal
                    if result.extra_student and result.extra_student.separate_journal_id:
                        target_journal = result.extra_student.separate_journal
                    elif result.extra_student and result.extra_student.matched_student_id:
                        if not journal_students(journal).filter(pk=result.extra_student.matched_student_id).exists():
                            other_enr = ClassEnrollment.objects.filter(student_id=result.extra_student.matched_student_id, is_active=True).first()
                            if other_enr:
                                other_j = Journal.objects.filter(
                                    school=journal.school,
                                    academic_year=journal.academic_year,
                                    student_class=other_enr.student_class,
                                    subject=journal.subject,
                                    teacher=journal.teacher,
                                ).first()
                                if other_j and has_journal_access(request.user, other_j, write=True):
                                    target_journal = other_j
                    if lesson_id:
                        lesson = target_journal.lessons.filter(pk=int(lesson_id)).first() if str(lesson_id).isdigit() else None
                        if not lesson or lesson.date.isoformat() != payload.get('date'):
                            raise ExternalAPIError('Оберіть урок цього журналу на початкову дату результату.')
                        payload['lesson_number'] = lesson.lesson_number
                    else:
                        d_str = payload.get('date')
                        if d_str and target_journal != journal:
                            l_date = dt.date.fromisoformat(d_str)
                            lesson = target_journal.lessons.filter(date=l_date).first()
                            if not lesson:
                                main_lesson = journal.lessons.filter(date=l_date).first()
                                topic = main_lesson.topic if main_lesson else (payload.get('title') or 'Позакласна робота')
                                lesson = Lesson.objects.create(
                                    journal=target_journal,
                                    date=l_date,
                                    lesson_number=main_lesson.lesson_number if main_lesson else 1,
                                    topic=topic,
                                    homework=main_lesson.homework if main_lesson else '',
                                    lesson_type=main_lesson.lesson_type if main_lesson else LessonType.ORDINARY,
                                    semester=main_lesson.semester if main_lesson else None,
                                    created_by=request.user
                                )
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
        return redirect(request.get_full_path())

    students = list(journal_students(journal))
    journal_student_ids = set(s.pk for s in students)

    other_enrollments = ClassEnrollment.objects.filter(
        student_class__school=journal.school,
        student_class__academic_year=journal.academic_year,
        is_active=True
    ).exclude(
        student_id__in=journal_student_ids
    ).exclude(
        student_class__letter='ПК'
    ).select_related('student', 'student_class').order_by(
        'student_class__grade_level', 'student_class__name', 'student__last_name', 'student__first_name'
    )

    other_school_students = []
    seen_student_ids = set()
    for enr in other_enrollments:
        if enr.student_id not in seen_student_ids:
            st = enr.student
            st.current_class_name = enr.student_class.name
            st.current_class_id = enr.student_class_id
            other_school_students.append(st)
            seen_student_ids.add(enr.student_id)

    remaining_students = Student.objects.filter(
        school=journal.school,
        status=Student.Status.ACTIVE
    ).exclude(
        pk__in=journal_student_ids
    ).exclude(
        pk__in=seen_student_ids
    ).order_by('last_name', 'first_name')

    for st in remaining_students:
        st.current_class_name = 'Без класу'
        st.current_class_id = None
        other_school_students.append(st)

    mismatched_connections = []
    for conn in journal.grade_apis.filter(enabled=True, contract_version=2):
        if conn.source_class_id and conn.roster_cache:
            classes = conn.roster_cache.get('classes', [])
            c_name = next((c['name'] for c in classes if str(c['id']) == str(conn.source_class_id)), None)
            if c_name:
                def _cl(s): return ''.join(ch for ch in s.casefold() if ch.isalnum())
                if _cl(c_name) != _cl(journal.student_class.name):
                    mismatched_connections.append({
                        'connection': conn,
                        'source_class_name': c_name,
                        'journal_class_name': journal.student_class.name,
                    })

    show_all = request.GET.get('all') == '1'
    visible_results = APIInboxResult.objects.filter(extra_student=OuterRef('pk'), is_ignored=False)
    extras = ExtraStudent.objects.filter(connection__journal=journal, connection__enabled=True, alias_of__isnull=True).select_related('connection', 'matched_student', 'separate_student', 'separate_journal')
    extras = extras.annotate(
        has_results=Exists(APIInboxResult.objects.filter(extra_student=OuterRef('pk'))),
        has_visible_results=Exists(visible_results),
    ).filter(Q(has_results=False) | Q(has_visible_results=True))
    if not show_all:
        extras = extras.filter(matched_student__isnull=True)
    search = request.GET.get('q', '').strip()
    if search:
        extras = extras.filter(name__icontains=search)
    student_filter = request.GET.get('student', '')
    if student_filter.isdigit():
        extras = extras.filter(pk=int(student_filter))
    source_filter = request.GET.get('source', '')
    if source_filter in ('schoolwork', 'schooltest'):
        extras = extras.filter(connection__source=source_filter)
    page = Paginator(extras, 50).get_page(request.GET.get('page'))
    for extra in page:
        extra.suggestions = [s.get_full_name_ukr() for s in students if similar(extra.name, s.get_full_name_ukr())][:5]
        sug = find_suggested_student(extra.name, students)
        extra.suggested_student = sug
        extra.suggested_student_id = sug.pk if sug else None
        results = extra.results.filter(is_ignored=False)
        if not show_all and not extra.is_separate:
            results = results.filter(resolved=False)
        extra.recent_results = list(results.order_by('-updated_at')[:5])
        extra.merge_candidates = list(ExtraStudent.objects.filter(connection__journal=journal,
            alias_of__isnull=True).exclude(pk=extra.pk)[:100])

    inbox = APIInboxResult.objects.filter(
        connection__journal=journal, connection__enabled=True, resolved=False, is_ignored=False
    ).select_related('connection', 'extra_student')
    if student_filter.isdigit():
        inbox = APIInboxResult.objects.filter(connection__journal=journal, extra_student_id=int(student_filter),
            is_ignored=False).select_related('connection', 'extra_student')
    if search:
        inbox = inbox.filter(Q(extra_student__name__icontains=search) | Q(payload__student_name__icontains=search))
    if source_filter in ('schoolwork', 'schooltest'):
        inbox = inbox.filter(connection__source=source_filter)
    result_page = Paginator(inbox, 50).get_page(request.GET.get('results_page'))
    for result in result_page:
        target_journal = journal
        if result.extra_student and result.extra_student.separate_journal_id:
            target_journal = result.extra_student.separate_journal
        elif result.extra_student and result.extra_student.matched_student_id:
            if not journal_students(journal).filter(pk=result.extra_student.matched_student_id).exists():
                enr = ClassEnrollment.objects.filter(student_id=result.extra_student.matched_student_id, is_active=True).first()
                if enr:
                    oj = Journal.objects.filter(
                        school=journal.school,
                        academic_year=journal.academic_year,
                        student_class=enr.student_class,
                        subject=journal.subject,
                        teacher=journal.teacher,
                    ).first()
                    if oj:
                        target_journal = oj
        result.target_journal = target_journal
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

    filters = request.GET.copy()
    for key in ('page', 'results_page', 'ignored_page'):
        filters.pop(key, None)

    unconfirmed_all = ExtraStudent.objects.filter(
        connection__journal=journal, connection__enabled=True,
        alias_of__isnull=True, matched_student__isnull=True, separate_student__isnull=True
    )
    auto_matched_count = sum(1 for e in unconfirmed_all if find_suggested_student(e.name, students))

    return render(request, 'portal/journal/api_inbox.html', {
        'journal': journal,
        'decision_logs': AuditLog.objects.filter(school=journal.school, target_model__in=['ExtraStudent', 'Student'], changes__journal_id=journal.pk).select_related('user')[:20],
        'extra_page': page,
        'result_page': result_page,
        'ignored_results': ignored_results,
        'extra_journal': extra_journal,
        'students': students,
        'auto_matched_count': auto_matched_count,
        'other_school_students': other_school_students,
        'mismatched_connections': mismatched_connections,
        'show_all': request.GET.get('all') == '1',
        'can_edit': has_journal_access(request.user, journal, write=True),
        'search': search, 'source_filter': source_filter, 'student_filter': student_filter,
        'filter_query': filters.urlencode(),
    })
