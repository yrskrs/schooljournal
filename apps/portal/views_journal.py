import datetime
import calendar
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core import signing
from django.db import transaction, IntegrityError
from django.db.models.deletion import ProtectedError, RestrictedError
from django.db.models import Q
from django.views.decorators.http import require_POST, require_http_methods
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from apps.portal.permissions import has_journal_access, can_manage_journal
from apps.journals.services.journal_lifecycle import delete_local_journal, journal_content_counts
from apps.accounts.personal import journal_nz_account
from apps.curriculum.models import Student, StudentClass, ClassEnrollment, Semester, LessonTime
from apps.journals.models import (
    Journal, Lesson, Grade, SummaryGrade, Attendance, LessonPlanTopic,
    LessonType, SummaryGradeType, AuditLog, SPECIAL_GRADE_VALUES, ExternalGrade
)
from apps.portal.forms_journal import LessonForm, LessonCreateForm, SummaryColumnForm, AssessmentForm, TopicCopyForm, CalendarSyncForm
from apps.journals.services.topic_copy import (
    numbered_lessons, preview_topic_copy, apply_topic_copy,
    preview_calendar_sync, apply_calendar_sync
)
from apps.journals.services.extra_students import extra_parent
from apps.journals.services.scheduling import (
    next_lesson_number, insert_day_slot, set_homework_target, swap_topics, reorder_plan_topic,
    lesson_occurrence_dates, copy_repeated_homework, SUMMARY_TYPES,
)
from apps.journals.services.assessment import (
    calculate, journal_students, legacy_summary_groups, filter_summary_sources,
    assessment_type_choices, assessment_period, period_lessons,
)


def _get_journal_or_403(user, journal_id, write=False):
    journal = get_object_or_404(
        Journal.objects.select_related('school', 'academic_year', 'student_class', 'subject', 'teacher'),
        id=journal_id
    )
    if not has_journal_access(user, journal, write=write):
        if write and journal.is_archived:
            raise PermissionDenied("Журнал заархівовано. Редагування можливе лише з дозволу суперадміністратора.")
        raise PermissionDenied("У вас немає прав для виконання цієї дії над журналом.")
    return journal


def _student_for_journal(journal, student_id):
    return get_object_or_404(journal_students(journal), pk=student_id)


@login_required
@require_http_methods(['GET', 'POST'])
def journal_delete(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    if not can_manage_journal(request.user, journal):
        raise PermissionDenied('Локальний журнал може видалити лише власник або адміністратор.')
    if request.method == 'POST':
        if request.POST.get('confirm_delete') != 'on':
            messages.error(request, 'Підтвердіть видалення локального журналу.')
        else:
            try:
                delete_local_journal(journal, request.user)
            except ValueError as error:
                messages.error(request, str(error))
            except (ProtectedError, RestrictedError):
                messages.error(request, 'Журнал має залежні записи в іншому журналі. Видалення скасовано; перевірте позакласні записи та підключення.')
            else:
                messages.success(request, 'Локальний журнал видалено. Журнал у nz.ua збережено; його можна завантажити знову.')
                return redirect('portal:nz_journals_list' if journal.nz_journal_id else 'portal:teacher_dashboard')
    return render(request, 'portal/journal/confirm_cleanup.html', {
        'journal': journal, 'counts': journal_content_counts(journal), 'cleanup_kind': 'delete',
    })


@login_required
def journal_detail(request, journal_id):
    """
    Основний екран електронного журналу:
    - Сітка: учні у рядках, уроки у стовпцях
    - Закріплені заголовки та колонка ПІБ
    - Фільтрація за семестрами та місяцями
    - Пошук учня
    - Швидке внесення оцінок та відвідування
    """
    journal = _get_journal_or_403(request.user, journal_id, write=False)
    parent = extra_parent(journal)
    if parent and not journal.lessons.exists() and not journal.summary_grades.exists():
        return redirect(reverse('portal:outside_students') + '?journal=' + str(parent.pk))
    can_edit = has_journal_access(request.user, journal, write=True)
    can_manage = can_manage_journal(request.user, journal)
    show_withdrawn = request.GET.get('withdrawn') == '1'

    # 1. Семестри навчального року
    semesters = list(journal.academic_year.semesters.all().order_by('number'))
    selected_semester_id = request.GET.get('semester_id')
    selected_semester = None
    if selected_semester_id and selected_semester_id != 'all':
        selected_semester = next((s for s in semesters if str(s.id) == str(selected_semester_id)), None)
    if not selected_semester and semesters and selected_semester_id not in (None, 'all'):
        # Default to current by date, or semester that contains lessons, or first semester
        today = timezone.now().date()
        selected_semester = next((s for s in semesters if s.start_date <= today <= s.end_date), None)
        if not selected_semester:
            journal_lesson_dates = list(Lesson.objects.filter(journal=journal).values_list('date', flat=True))
            for s in semesters:
                if any(s.start_date <= d <= s.end_date for d in journal_lesson_dates):
                    selected_semester = s
                    break
        if not selected_semester:
            selected_semester = semesters[0]

    # 2. Уроки
    lessons_qs = Lesson.objects.filter(journal=journal).select_related(
        'linked_plan_topic', 'homework_due_lesson', 'homework_due_plan_topic'
    ).order_by('date', 'lesson_number')
    if selected_semester:
        lessons_qs = lessons_qs.filter(
            date__gte=selected_semester.start_date,
            date__lte=selected_semester.end_date
        )

    # Фільтрація за місяцем
    selected_month = request.GET.get('month', 'all')
    if selected_month and selected_month != 'all':
        try:
            if '-' in selected_month:
                year, month = map(int, selected_month.split('-'))
                datetime.date(year, month, 1)
                lessons_qs = lessons_qs.filter(date__year=year, date__month=month)
            else:
                m_int = int(selected_month)
                if not 1 <= m_int <= 12:
                    raise ValueError
                lessons_qs = lessons_qs.filter(date__month=m_int)
        except ValueError:
            selected_month = 'all'

    lessons = list(lessons_qs)

    # Місяці для вкладок
    all_journal_dates = Lesson.objects.filter(journal=journal)
    if selected_semester:
        all_journal_dates = all_journal_dates.filter(date__range=(selected_semester.start_date, selected_semester.end_date))
    all_journal_dates = all_journal_dates.values_list('date', flat=True)
    months_available = sorted(list(set((d.year, d.month) for d in all_journal_dates)))
    month_names_ukr = {
        1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень", 6: "Червень",
        7: "Липень", 8: "Серпень", 9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
    }
    month_tabs = [{'number': f'{y}-{m:02d}', 'name': f"{month_names_ukr[m]} {y}"} for y, m in months_available]
    month_groups = []
    previous_month = None
    for lesson in lessons:
        month = (lesson.date.year, lesson.date.month)
        lesson.starts_month = month != previous_month
        lesson.month_label = f'{month_names_ukr[month[1]]} {month[0]}'
        if lesson.starts_month:
            month_groups.append({'name': lesson.month_label, 'count': 0})
        month_groups[-1]['count'] += 1
        previous_month = month

    # 3. Список учнів (активні зарахування + учні, що мають оцінки в цьому журналі)
    active_enrollments = ClassEnrollment.objects.filter(
        student_class=journal.student_class,
        is_active=True
    ).select_related('student')

    historical_enrollments = ClassEnrollment.objects.filter(
        student_class=journal.student_class,
        is_active=False
    ).select_related('student')

    active_student_ids = set(e.student_id for e in active_enrollments)
    historical_student_ids = set(e.student_id for e in historical_enrollments)
    graded_student_ids = set(Grade.objects.filter(lesson__journal=journal).values_list('student_id', flat=True))

    all_relevant_student_ids = active_student_ids.union(historical_student_ids).union(graded_student_ids)
    students = list(journal_students(journal))

    # Формуємо інформацію про статус учня у класі
    ignored_students_set = set(journal.ignored_students.values_list('pk', flat=True))
    student_rows = []
    displayed_student_number = 0
    for s in students:
        is_active_in_class = s.id in active_student_ids
        if is_active_in_class or show_withdrawn:
            displayed_student_number += 1
        status_note = "" if is_active_in_class else " (вибув / переведено)"
        student_rows.append({
            'student': s,
            'is_active': is_active_in_class,
            'status_note': status_note,
            'display_number': displayed_student_number,
            'is_ignored_for_sync': s.pk in ignored_students_set,
        })

    # 4. Матриці оцінок, відвідування та підсумків
    grades = {} # (lesson_id, student_id) -> Grade
    for g in Grade.objects.filter(lesson__in=lessons).select_related('import_record__connection'):
        grades[(g.lesson_id, g.student_id)] = g

    attendances = {} # (lesson_id, student_id) -> Attendance
    for a in Attendance.objects.filter(lesson__in=lessons):
        attendances[(a.lesson_id, a.student_id)] = a

    summary_grades = {}
    legacy_summary_columns = []
    for key, group in legacy_summary_groups(journal).items():
        if any(sg.value for sg in group['grades'].values()):
            legacy_summary_columns.append((key, group['label']))
            for student_id, sg in group['grades'].items():
                summary_grades[(student_id, key)] = sg

    # Розрахунок довідкового середнього балу для кожного учня
    student_averages = {}
    for s in students:
        s_grades = [
            grades[(l.id, s.id)].numeric_value
            for l in lessons
            if l.lesson_type not in SUMMARY_TYPES and (l.id, s.id) in grades and grades[(l.id, s.id)].is_approved and grades[(l.id, s.id)].numeric_value is not None
        ]
        if s_grades:
            student_averages[s.id] = round(sum(s_grades) / len(s_grades), 1)
        else:
            student_averages[s.id] = "-"

    # Планові теми для модального вікна створення уроку
    plan_topics = list(LessonPlanTopic.objects.filter(journal=journal, is_conducted=False).order_by('topic_number', 'pk'))
    all_lessons = list(journal.lessons.order_by('date', 'lesson_number'))
    sequence_numbers = {l.pk: number for number, l in numbered_lessons(all_lessons).items()}
    for lesson in lessons:
        lesson.sequence_number = sequence_numbers.get(lesson.pk)
    lesson_choices = [{
        'id': l.pk, 'date': l.date.isoformat(), 'number': l.lesson_number,
        'topic': l.topic, 'homework': l.homework, 'type': l.lesson_type, 'notes': l.notes,
        'time': l.lesson_time.strftime('%H:%M') if l.lesson_time else '',
        'homework_target': (f'lesson:{l.homework_due_lesson_id}' if l.homework_due_lesson_id else
                            f'plan:{l.homework_due_plan_topic_id}' if l.homework_due_plan_topic_id else
                            'next' if l.homework_for_next_lesson else
                            f'date:{l.homework_due_date.isoformat()}' if l.homework_due_date else
                            'next'),
        'nz_bound': bool(l.nz_schedule_id),
    } for l in all_lessons]

    # Рахуємо несинхронізовані зміни з nz.ua для цього журналу (без ігнорованих учнів та ігнорованих записів)
    unsynced_grades_count = Grade.objects.filter(
        lesson__journal=journal, is_synced_to_nz=False, is_approved=True, is_ignored_for_nz=False
    ).exclude(student_id__in=ignored_students_set).count()
    unsynced_attendances_count = Attendance.objects.filter(
        lesson__journal=journal, is_synced_to_nz=False, is_ignored_for_nz=False
    ).exclude(student_id__in=ignored_students_set).count()
    unsynced_lessons_count = Lesson.objects.filter(journal=journal, is_synced_to_nz=False, is_ignored_for_nz=False).count()
    unsynced_total = unsynced_grades_count + unsynced_attendances_count + unsynced_lessons_count
    nz_account = journal_nz_account(journal, request.user)
    calculation_date = max(journal.academic_year.start_date, min(timezone.localdate(), journal.academic_year.end_date))
    calculation_semester = selected_semester or next((s for s in semesters if s.start_date <= calculation_date <= s.end_date), None)
    lesson_time_slots = list(LessonTime.objects.filter(school=journal.school, is_active=True))
    create_defaults = {}
    if can_edit and request.GET.get('create_date'):
        try:
            requested_date = datetime.date.fromisoformat(request.GET['create_date'])
            requested_time = datetime.time.fromisoformat(request.GET.get('create_time', ''))
            if journal.academic_year.start_date <= requested_date <= journal.academic_year.end_date and any(
                    slot.start_time == requested_time for slot in lesson_time_slots):
                create_defaults = {'date': requested_date.isoformat(), 'time': requested_time.strftime('%H:%M')}
        except ValueError:
            pass

    integration_start = selected_semester.start_date if selected_semester else journal.academic_year.start_date
    integration_end = selected_semester.end_date if selected_semester else journal.academic_year.end_date
    if selected_month != 'all':
        if '-' in selected_month:
            year_number, month_number = map(int, selected_month.split('-'))
        else:
            month_number = int(selected_month)
            year_number = journal.academic_year.start_date.year if month_number >= journal.academic_year.start_date.month else journal.academic_year.end_date.year
        integration_start = max(integration_start, datetime.date(year_number, month_number, 1))
        integration_end = min(integration_end, datetime.date(year_number, month_number, calendar.monthrange(year_number, month_number)[1]))

    extra_journal = Journal.objects.filter(
        school=journal.school,
        academic_year=journal.academic_year,
        student_class__name=f"{journal.student_class.name} (Позакласні)",
        subject=journal.subject,
        teacher=journal.teacher,
    ).first()

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

    context = {
        'integration_date_from': integration_start.isoformat(),
        'integration_date_to': integration_end.isoformat(),
        'journal': journal,
        'extra_journal': extra_journal,
        'extra_parent': extra_parent(journal),
        'mismatched_connections': mismatched_connections,
        'can_edit': can_edit,
        'can_manage': can_manage,
        'show_withdrawn': show_withdrawn,
        'semesters': semesters,
        'selected_semester': selected_semester,
        'selected_month': selected_month,
        'month_tabs': month_tabs,
        'month_groups': month_groups,
        'legacy_summary_columns': legacy_summary_columns,
        'journal_column_count': (len(lessons) or 1) + len(legacy_summary_columns) + 3,
        'lesson_choices': lesson_choices,
        'lesson_time_slots': lesson_time_slots,
        'create_lesson_defaults': create_defaults,
        'all_lessons': all_lessons,
        'selected_semester_param': selected_semester.pk if selected_semester else 'all',
        'assessment_semester_type': (
            LessonType.SEMESTER_2 if calculation_semester and calculation_semester.number == 2
            else LessonType.SEMESTER_1),
        'lessons': lessons,
        'student_rows': student_rows,
        'ignored_student_ids': list(ignored_students_set),
        'grades': grades,
        'attendances': attendances,
        'summary_grades': summary_grades,
        'student_averages': student_averages,
        'plan_topics': plan_topics,
        'lesson_types': LessonType.choices,
        'summary_grade_types': SummaryGradeType.choices,
        'unsynced_total': unsynced_total,
        'unsynced_grades_count': unsynced_grades_count,
        'unsynced_attendances_count': unsynced_attendances_count,
        'unsynced_lessons_count': unsynced_lessons_count,
        'nz_account': nz_account,
        'external_review_count': ExternalGrade.objects.filter(connection__journal=journal, connection__enabled=True,
            status__in=[ExternalGrade.Status.PENDING, ExternalGrade.Status.CONFLICT]).count(),
    }
    return render(request, 'portal/journal/detail.html', context)


@login_required
@transaction.atomic
def update_grade(request, journal_id):
    """
    HTMX-ендпоінт для внесення або зміни оцінки.
    Перевіряє шкалу (1..12), конфлікти паралельного редагування (409 Conflict)
    та повертає оновлений фрагмент клітинки.
    """
    if request.method != 'POST':
        return HttpResponse("Метод не підтримується", status=405)

    journal = _get_journal_or_403(request.user, journal_id, write=True)
    Journal.objects.select_for_update().get(pk=journal.pk)

    lesson_id = request.POST.get('lesson_id')
    student_id = request.POST.get('student_id')
    raw_value = request.POST.get('value', '').strip()
    client_version = request.POST.get('client_version')

    lesson = get_object_or_404(Lesson, id=lesson_id, journal=journal)
    student = _student_for_journal(journal, student_id)

    if journal.ignored_students.filter(pk=student.pk).exists():
        return HttpResponse(
            "<span class='text-danger fw-bold' title='Учень на сімейній формі навчання / ігнорується для синхронізації'>Заблоковано</span>",
            status=400
        )

    clean_val = raw_value.upper()

    # Співставлення значень відвідуваності та оцінок
    att_mapping = {
        'Н': Attendance.Status.ABSENT_UNEXCUSED,
        'N': Attendance.Status.ABSENT_UNEXCUSED,
        'ABSENT_UNEXCUSED': Attendance.Status.ABSENT_UNEXCUSED,
        'ABSENT': Attendance.Status.ABSENT_UNEXCUSED,
        'ХВ': Attendance.Status.ABSENT_EXCUSED,
        'Х': Attendance.Status.ABSENT_EXCUSED,
        'HV': Attendance.Status.ABSENT_EXCUSED,
        'H': Attendance.Status.ABSENT_EXCUSED,
        'ABSENT_EXCUSED': Attendance.Status.ABSENT_EXCUSED,
        'SICK': Attendance.Status.ABSENT_EXCUSED,
        'ЗП': Attendance.Status.LATE,
        'З': Attendance.Status.LATE,
        'ZP': Attendance.Status.LATE,
        'Z': Attendance.Status.LATE,
        'LATE': Attendance.Status.LATE,
    }

    is_attendance = clean_val in att_mapping
    is_clear = (clean_val == "" or clean_val in ['PRESENT', 'CLEAR', 'ОЧИСТИТИ'])
    is_grade = False

    if not is_attendance and not is_clear:
        if clean_val.isdigit():
            num = int(clean_val)
            if 1 <= num <= 12:
                is_grade = True
            else:
                return HttpResponse(
                    "<span class='text-danger fw-bold' title='Оцінка має бути від 1 до 12!'>Помилка</span>",
                    status=400
                )
        elif clean_val in SPECIAL_GRADE_VALUES:
            is_grade = True
        else:
            return HttpResponse(
                "<span class='text-danger fw-bold' title='Некоректне значення (введіть 1-12 або Н, ХВ, ЗП)'>Помилка</span>",
                status=400
            )

    grade = Grade.objects.filter(lesson=lesson, student=student).first()
    att = Attendance.objects.filter(lesson=lesson, student=student).first()
    if grade and not grade.is_approved:
        return render(request, 'portal/journal/partials/grade_cell.html', {
            'lesson': lesson, 'student': student, 'grade': grade, 'attendance': att, 'can_edit': True,
            'conflict_error': True, 'message': 'Спочатку схваліть або відхиліть попередню оцінку на сторінці «Оцінки з робіт».'}, status=409)

    # Оптимістичне блокування (Conflict Detection)
    if client_version and client_version.strip() not in ["", "0", "0.0"]:
        try:
            client_ts = float(client_version)
            if client_ts > 0:
                latest_server_ts = 0.0
                if grade and grade.updated_at:
                    latest_server_ts = max(latest_server_ts, grade.updated_at.timestamp())
                if att and att.updated_at:
                    latest_server_ts = max(latest_server_ts, att.updated_at.timestamp())
                if latest_server_ts > 0 and (latest_server_ts - client_ts > 1.5):
                    context = {
                        'conflict_error': True,
                        'message': "Конфлікт: дані клітинки були змінені іншим користувачем.",
                        'lesson': lesson,
                        'student': student,
                        'grade': grade,
                        'attendance': att,
                        'can_edit': True,
                    }
                    return render(request, 'portal/journal/partials/grade_cell.html', context, status=409)
        except (ValueError, TypeError):
            pass

    old_grade_val = grade.value if grade else ""
    old_att_status = att.status if att else Attendance.Status.PRESENT

    if grade and grade.import_record_id:
        grade.import_record = None
        grade.save(update_fields=['import_record'])

    if is_attendance:
        new_status = att_mapping[clean_val]
        if att:
            if att.status != new_status:
                att.status = new_status
                att.is_synced_to_nz = False
                att.save()
        else:
            att = Attendance.objects.create(lesson=lesson, student=student, status=new_status, is_synced_to_nz=False)

        # Комірка або оцінка, або відвідування: якщо відсутній — скидаємо оцінку
        if grade and grade.value != "":
            grade.value = ""
            grade.is_synced_to_nz = False
            grade.save()

        if old_att_status != new_status:
            AuditLog.objects.create(
                school=journal.school,
                user=request.user,
                action=AuditLog.Action.UPDATE if old_att_status != Attendance.Status.PRESENT else AuditLog.Action.CREATE,
                target_model='Attendance',
                target_id=str(att.id),
                description=f"Відвідування для {student.get_short_name_ukr()} на уроці {lesson.date.strftime('%d.%m')}: «{old_att_status}» -> «{new_status}»",
                changes={'status': {'old': old_att_status, 'new': new_status}}
            )

    elif is_grade:
        if grade and grade.import_record_id:
            # An explicit manual edit owns the cell even if its value is unchanged.
            grade.import_record = None
            grade.save(update_fields=['import_record'])
        if grade:
            if grade.value != clean_val:
                grade.value = clean_val
                grade.is_synced_to_nz = False
                grade.created_by = request.user
                grade.save()
        else:
            grade = Grade.objects.create(
                lesson=lesson,
                student=student,
                value=clean_val,
                is_synced_to_nz=False,
                created_by=request.user
            )

        # Якщо виставлено оцінку, студент був присутній
        if att and att.status != Attendance.Status.PRESENT:
            att.status = Attendance.Status.PRESENT
            att.is_synced_to_nz = False
            att.save()

        if old_grade_val != clean_val:
            AuditLog.objects.create(
                school=journal.school,
                user=request.user,
                action=AuditLog.Action.UPDATE if old_grade_val else AuditLog.Action.CREATE,
                target_model='Grade',
                target_id=str(grade.id),
                description=f"Оцінка для {student.get_short_name_ukr()} на уроці {lesson.date.strftime('%d.%m')}: «{old_grade_val or '—'}» -> «{clean_val or '—'}»",
                changes={'value': {'old': old_grade_val, 'new': clean_val}}
            )

    else:
        # Очищення комірки (порожній рядок або видалення)
        if grade and grade.value != "":
            grade.value = ""
            grade.is_synced_to_nz = False
            grade.save()
            AuditLog.objects.create(
                school=journal.school,
                user=request.user,
                action=AuditLog.Action.UPDATE,
                target_model='Grade',
                target_id=str(grade.id),
                description=f"Очищено оцінку для {student.get_short_name_ukr()} на уроці {lesson.date.strftime('%d.%m')}",
                changes={'value': {'old': old_grade_val, 'new': ''}}
            )
        if att and att.status != Attendance.Status.PRESENT:
            att.status = Attendance.Status.PRESENT
            att.is_synced_to_nz = False
            att.save()
            AuditLog.objects.create(
                school=journal.school,
                user=request.user,
                action=AuditLog.Action.UPDATE,
                target_model='Attendance',
                target_id=str(att.id),
                description=f"Скинуто відвідуваність (Присутній) для {student.get_short_name_ukr()} на уроці {lesson.date.strftime('%d.%m')}",
                changes={'status': {'old': old_att_status, 'new': Attendance.Status.PRESENT}}
            )

    context = {
        'lesson': lesson,
        'student': student,
        'grade': grade,
        'attendance': att,
        'can_edit': True,
        'just_saved': True,
    }
    return render(request, 'portal/journal/partials/grade_cell.html', context)


@login_required
@transaction.atomic
def update_attendance(request, journal_id):
    """
    HTMX-ендпоінт для швидкого встановлення або перемикання відвідуваності:
    PRESENT, ABSENT_UNEXCUSED ('Н'), ABSENT_EXCUSED ('ХВ'), LATE ('ЗП').
    """
    if request.method != 'POST':
        return HttpResponse("Метод не підтримується", status=405)

    journal = _get_journal_or_403(request.user, journal_id, write=True)
    Journal.objects.select_for_update().get(pk=journal.pk)

    lesson_id = request.POST.get('lesson_id')
    student_id = request.POST.get('student_id')
    status_str = request.POST.get('status', Attendance.Status.PRESENT)
    reason = request.POST.get('reason', '').strip()
    client_version = request.POST.get('client_version')

    lesson = get_object_or_404(Lesson, id=lesson_id, journal=journal)
    student = _student_for_journal(journal, student_id)

    if journal.ignored_students.filter(pk=student.pk).exists():
        return HttpResponse('Учень на сімейній формі навчання / ігнорується для синхронізації.', status=400)

    if status_str not in Attendance.Status.values:
        return HttpResponse('Некоректний статус відвідування.', status=400)

    att = Attendance.objects.filter(lesson=lesson, student=student).first()

    # Conflict check
    if att and client_version:
        try:
            client_ts = float(client_version)
            server_ts = att.updated_at.timestamp()
            if server_ts - client_ts > 1.5:
                context = {
                    'conflict_error': True,
                    'message': f"Конфлікт відвідування: змінено іншим користувачем о {att.updated_at.strftime('%H:%M:%S')}.",
                    'lesson': lesson,
                    'student': student,
                    'attendance': att,
                    'can_edit': True,
                }
                return render(request, 'portal/journal/partials/attendance_cell.html', context, status=409)
        except (ValueError, TypeError):
            pass

    old_status = att.status if att else Attendance.Status.PRESENT

    if att:
        if att.status != status_str or (reason and att.reason != reason):
            att.status = status_str
            att.is_synced_to_nz = False
            if reason:
                att.reason = reason
            att.save()
    else:
        att = Attendance.objects.create(
            lesson=lesson,
            student=student,
            status=status_str,
            reason=reason,
            is_synced_to_nz=False
        )

    if old_status != status_str:
        AuditLog.objects.create(
            school=journal.school,
            user=request.user,
            action=AuditLog.Action.UPDATE,
            target_model='Attendance',
            target_id=str(att.id),
            description=f"Відвідування для {student.get_short_name_ukr()} на уроці {lesson.date.strftime('%d.%m')}: {old_status} -> {status_str}",
            changes={'status': {'old': old_status, 'new': status_str}}
        )

    context = {
        'lesson': lesson,
        'student': student,
        'attendance': att,
        'can_edit': True,
        'just_saved': True,
    }
    return render(request, 'portal/journal/partials/attendance_cell.html', context)


@login_required
@transaction.atomic
def update_summary_grade(request, journal_id):
    """Внесення або редагування підсумкової оцінки (Тематична, Семестрова, Річна)."""
    if request.method != 'POST':
        return HttpResponse("Метод не підтримується", status=405)

    journal = _get_journal_or_403(request.user, journal_id, write=True)
    Journal.objects.select_for_update().get(pk=journal.pk)

    student_id = request.POST.get('student_id')
    grade_type = request.POST.get('grade_type')
    thematic_name = request.POST.get('thematic_name', '').strip()
    raw_val = request.POST.get('value', '').strip()

    student = _student_for_journal(journal, student_id)

    if journal.ignored_students.filter(pk=student.pk).exists():
        return HttpResponse('Учень на сімейній формі навчання / ігнорується для синхронізації.', status=400)

    if grade_type not in SummaryGradeType.values or not _valid_grade(raw_val):
        return HttpResponse('Некоректна підсумкова оцінка.', status=400)

    if request.POST.get('summary_id'):
        sg = get_object_or_404(SummaryGrade, journal=journal, student=student, pk=request.POST['summary_id'])
        created = False
    else:
        sg, created = SummaryGrade.objects.get_or_create(
            journal=journal, student=student, grade_type=grade_type, thematic_name=thematic_name,
            defaults={'value': raw_val}
        )
    old_val = sg.value if not created else ""
    if not created:
        sg.value = raw_val
        if thematic_name:
            sg.thematic_name = thematic_name
        sg.save()

    AuditLog.objects.create(
        school=journal.school,
        user=request.user,
        action=AuditLog.Action.UPDATE if not created else AuditLog.Action.CREATE,
        target_model='SummaryGrade',
        target_id=str(sg.id),
        description=f"Підсумкова оцінка [{sg.get_grade_type_display()}] для {student.get_short_name_ukr()}: «{old_val}» -> «{raw_val}»",
        changes={'value': {'old': old_val, 'new': raw_val}}
    )

    return HttpResponse(f"<span class='text-primary fw-bold'>{sg.value or '—'}</span>")


def _valid_grade(value):
    return not value or value in SPECIAL_GRADE_VALUES or (value.isdigit() and 1 <= int(value) <= 12)


@login_required
@require_POST
def lesson_create(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    if request.POST.get('lesson_type') in SUMMARY_TYPES:
        return _summary_column_create(request, journal)
    form = LessonCreateForm(request.POST, journal=journal)
    if not form.is_valid():
        messages.error(request, 'Перевірте параметри уроку та повторення. ' + str(form.errors.as_text()))
        return redirect('portal:journal_detail', journal_id=journal.pk)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            Journal.objects.select_for_update().get(pk=journal.pk)
            dates = lesson_occurrence_dates(data['date'], data['repeat_frequency'],
                interval_weeks=data['repeat_interval_weeks'],
                count=data['repeat_count'] if data['repeat_end'] == 'count' else None,
                until=data['repeat_until'] if data['repeat_end'] == 'date' else None,
                academic_year=journal.academic_year)
            number = data['lesson_number'] or next_lesson_number(journal, data['date'])
            if data['insert_before_id']:
                before = get_object_or_404(Lesson, pk=data['insert_before_id'], journal=journal)
                if before.date != data['date']:
                    raise ValueError('Для вставлення перед уроком оберіть ту саму дату.')
                number = before.lesson_number
                insert_day_slot(journal, data['date'], number)
            elif journal.lessons.filter(date=data['date'], lesson_number=number).exists():
                raise ValueError('Цей номер на дату вже зайнятий. Оберіть наступний номер або вставлення перед уроком.')
            plan = None
            if data['plan_topic_id']:
                plan = get_object_or_404(LessonPlanTopic, pk=data['plan_topic_id'], journal=journal, is_conducted=False)
            semesters = list(journal.academic_year.semesters.all())
            lessons = []
            for index, lesson_date in enumerate(dates):
                lessons.append(Lesson.objects.create(
                    journal=journal, date=lesson_date,
                    lesson_number=number if index == 0 else next_lesson_number(journal, lesson_date),
                    lesson_time=data['lesson_time'], topic=data['topic'], homework=data['homework'],
                    lesson_type=data['lesson_type'], notes=data['notes'], linked_plan_topic=plan if index == 0 else None,
                    semester=next((s for s in semesters if s.start_date <= lesson_date <= s.end_date), None),
                    is_synced_to_nz=False, created_by=request.user,
                ))
            first = lessons[0]
            set_homework_target(first, data['homework_target'])
            first.save()
            for lesson in lessons[1:]:
                copy_repeated_homework(first, lesson)
                lesson.save()
            if plan:
                plan.is_conducted = True
                plan.planned_date = first.date
                plan.save()
            for index, lesson in enumerate(lessons):
                AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.CREATE,
                    target_model='Lesson', target_id=str(lesson.pk), description='Додано урок до локального журналу.',
                    changes={'series_first_lesson_id': first.pk, 'occurrence': index + 1, 'total': len(lessons)} if len(lessons) > 1 else {})
        messages.success(request, 'Урок додано. Зміни збережено локально.' if len(lessons) == 1 else
            f'Додано уроків: {len(lessons)}, з {dates[0]:%d.%m.%Y} до {dates[-1]:%d.%m.%Y}. Зміни збережено локально.')
    except (ValueError, IntegrityError) as exc:
        messages.error(request, str(exc) if isinstance(exc, ValueError) else 'Урок уже додано іншим користувачем. Оновіть журнал.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


def _summary_column_create(request, journal):
    form = SummaryColumnForm(request.POST, journal=journal)
    if not form.is_valid():
        messages.error(request, 'Перевірте параметри підсумкової колонки. ' + form.errors.as_text())
        return redirect('portal:journal_detail', journal_id=journal.pk)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            Journal.objects.select_for_update().get(pk=journal.pk)
            if not journal.academic_year.start_date <= data['date'] <= journal.academic_year.end_date:
                raise ValueError('Дата підсумкової колонки має бути в межах навчального року.')
            plan = None
            if data['plan_topic_id']:
                plan = get_object_or_404(LessonPlanTopic, pk=data['plan_topic_id'], journal=journal,
                                        is_conducted=False, lesson_type=data['lesson_type'])
            column = Lesson.objects.create(journal=journal, date=data['date'],
                lesson_number=next_lesson_number(journal, data['date']), lesson_time=data['lesson_time'],
                topic=data['title'] or (plan.title if plan else dict(LessonType.choices)[data['lesson_type']]),
                lesson_type=data['lesson_type'], linked_plan_topic=plan,
                semester=journal.academic_year.semesters.filter(start_date__lte=data['date'], end_date__gte=data['date']).first(),
                created_by=request.user, is_synced_to_nz=False)
            if plan:
                plan.is_conducted = True
                plan.planned_date = column.date
                plan.save(update_fields=['is_conducted', 'planned_date', 'updated_at'])
            AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.CREATE,
                target_model='Lesson', target_id=str(column.pk), description='Додано локальну підсумкову колонку.')
        messages.success(request, 'Підсумкову колонку додано локально. Оцінки можна виставити вручну або розрахувати автоматично.')
    except (ValueError, IntegrityError) as exc:
        messages.error(request, str(exc) if isinstance(exc, ValueError) else 'Колонку вже додано іншим користувачем. Оновіть журнал.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def lesson_edit(request, journal_id, lesson_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    original = get_object_or_404(Lesson, pk=lesson_id, journal=journal)
    post = request.POST.copy()
    # Older clients can still edit topic/homework without submitting scheduling fields.
    for field, value in [('date', original.date.isoformat()), ('lesson_number', original.lesson_number),
                         ('lesson_time', original.lesson_time.isoformat() if original.lesson_time else '')]:
        if field not in post:
            post[field] = str(value)
    form = LessonForm(post, journal=journal, preserve_time=original.lesson_time)
    if not form.is_valid():
        messages.error(request, 'Перевірте поля уроку. ' + str(form.errors.as_text()))
        return redirect('portal:journal_detail', journal_id=journal.pk)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            Journal.objects.select_for_update().get(pk=journal.pk)
            lesson = Lesson.objects.select_for_update().get(pk=lesson_id, journal=journal)
            old_date, old_number = lesson.date, lesson.lesson_number
            number = data['lesson_number'] or next_lesson_number(journal, data['date'])
            if lesson.nz_schedule_id and data['date'] != lesson.date:
                raise ValueError('Дату онлайн-уроку змініть у nz.ua та оновіть журнал. Для перенесення теми скористайтеся обміном темами.')
            if lesson.nz_schedule_id and data['lesson_type'] != lesson.lesson_type:
                raise ValueError('Тип онлайн-колонки змініть у nz.ua та завантажте оновлення. Локально можна створити нову підсумкову колонку.')
            if journal.lessons.filter(date=data['date'], lesson_number=number).exclude(pk=lesson.pk).exists():
                raise ValueError('На цю дату номер уже зайнятий. Оберіть інший номер або обмін темами.')
            for field in ('date', 'lesson_time', 'topic', 'homework', 'lesson_type', 'notes'):
                setattr(lesson, field, data[field])
            lesson.lesson_number = number
            lesson.semester = journal.academic_year.semesters.filter(start_date__lte=lesson.date, end_date__gte=lesson.date).first()
            if 'homework_target' in request.POST:
                set_homework_target(lesson, data['homework_target'])
            elif lesson.homework and not (lesson.homework_due_lesson_id or lesson.homework_due_plan_topic_id or lesson.homework_due_date):
                lesson.homework_for_next_lesson = True
            lesson.is_synced_to_nz = False
            lesson.save()
            if lesson.linked_plan_topic_id:
                lesson.linked_plan_topic.planned_date = lesson.date
                lesson.linked_plan_topic.save(update_fields=['planned_date', 'updated_at'])
            AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                target_model='Lesson', target_id=str(lesson.pk), description='Оновлено тему або розміщення уроку.',
                changes={'date': {'old': old_date.isoformat(), 'new': lesson.date.isoformat()},
                         'lesson_number': {'old': old_number, 'new': number}})
        messages.success(request, 'Урок оновлено; оцінки та відвідування збережено.')
    except (ValueError, IntegrityError) as exc:
        messages.error(request, str(exc) if isinstance(exc, ValueError) else 'Розміщення вже зайняте. Оновіть журнал.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def lesson_swap(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    try:
        first, second = swap_topics(journal, int(request.POST.get('first_id', '')), int(request.POST.get('second_id', '')))
        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
            target_model='Lesson', target_id=str(first.pk), description='Обмін темами двох уроків без перенесення оцінок.',
            changes={'other_lesson_id': second.pk})
        messages.success(request, 'Теми обміняно. Оцінки й відвідування залишилися на своїх датах.')
    except (ValueError, TypeError) as exc:
        messages.error(request, str(exc) if str(exc) else 'Оберіть два уроки для обміну темами.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def plan_topic_move(request, journal_id, topic_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    try:
        date_str = request.POST.get('planned_date', '')
        planned_date = datetime.date.fromisoformat(date_str) if date_str else None
        topic = reorder_plan_topic(journal, topic_id, request.POST.get('direction', ''), planned_date)
        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
            target_model='LessonPlanTopic', target_id=str(topic.pk), description='Змінено порядок або дату планової теми.')
        messages.success(request, 'Календарний план оновлено.')
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def lesson_delete(request, journal_id, lesson_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    with transaction.atomic():
        lesson = get_object_or_404(Lesson, pk=lesson_id, journal=journal)
        plan = lesson.linked_plan_topic
        lesson.delete()
        if plan and not plan.real_lessons.exists():
            plan.is_conducted = False
            plan.save(update_fields=['is_conducted', 'updated_at'])
        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.DELETE,
            target_model='Lesson', target_id=str(lesson_id), description='Видалено локальний урок.')
    messages.info(request, 'Урок видалено локально.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
def assessment_calculate(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    all_lessons = list(journal.lessons.order_by('date', 'lesson_number'))
    parameters = request.POST if request.method == 'POST' else request.GET
    compact = parameters.get('modal') == '1'
    requested_type = parameters.get('grade_type', LessonType.THEMATIC)
    if requested_type not in SUMMARY_TYPES:
        requested_type = LessonType.THEMATIC
    try:
        requested_date = datetime.date.fromisoformat(parameters.get('date', ''))
        if not journal.academic_year.start_date <= requested_date <= journal.academic_year.end_date:
            requested_date = None
    except ValueError:
        requested_date = None
    period = assessment_period(journal, requested_type, requested_date, parameters.get('semester_id'))
    is_thematic = requested_type in (LessonType.THEMATIC, LessonType.MODULAR, LessonType.THEMATIC_NUSH)
    source = ('all' if compact or requested_type in (LessonType.SEMESTER_1_NUSH, LessonType.SEMESTER_2_NUSH) else 'semester' if requested_type == LessonType.ANNUAL else
              'current' if is_thematic else 'thematic')
    eligible = period_lessons(all_lessons, period['start_date'], period['end_date'], period['boundary'])
    allowed_types = {choice[0] for choice in assessment_type_choices(source)}
    initial = {
        'grade_type': requested_type, 'source': source, 'rounding': 'nearest',
        'date': period['date'], 'start_date': period['start_date'], 'end_date': period['end_date'],
        'lesson_ids': [str(l.pk) for l in eligible if l.lesson_type in allowed_types],
        'title': parameters.get('title', '')[:255],
    }
    if not compact and requested_type in (LessonType.SEMESTER_1_NUSH, LessonType.SEMESTER_2_NUSH):
        initial['source_types'] = [LessonType.THEMATIC_NUSH]
        initial['lesson_ids'] = [str(l.pk) for l in eligible if l.lesson_type == LessonType.THEMATIC_NUSH]
    try:
        initial['lesson_time'] = datetime.time.fromisoformat(parameters.get('lesson_time', ''))
    except ValueError:
        initial['lesson_time'] = next((l.lesson_time for l in reversed(all_lessons)
                                       if l.date == period['date'] and l.lesson_time), None)
    post_data = request.POST.copy() if request.method == 'POST' else None
    if compact and post_data is not None:
        # Compact forms select by type; derive their lesson IDs on the server.
        post_data['source'] = 'all'
        post_data['source_types_present'] = '1'
        if not is_thematic:
            post_data['start_date'] = period['start_date'].isoformat()
            post_data['end_date'] = period['end_date'].isoformat()
        try:
            start = datetime.date.fromisoformat(post_data.get('start_date', ''))
            end = datetime.date.fromisoformat(post_data.get('end_date', ''))
            selected = period_lessons(all_lessons, start, end, period['boundary'])
        except ValueError:
            selected = []
        post_data.setlist('lesson_ids', [str(l.pk) for l in selected
                                      if l.lesson_type in post_data.getlist('source_types')])
    form = AssessmentForm(post_data, journal=journal, initial=initial, compact=compact)
    rows, preview_token = None, ''
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                Journal.objects.select_for_update().get(pk=journal.pk)
                lesson_ids = [pk for pk in data['lesson_ids'] if pk.isdigit()]
                lessons = list(journal.lessons.filter(pk__in=lesson_ids, date__range=(data['start_date'], data['end_date'])).order_by('date', 'lesson_number'))
                if compact:
                    lessons = period_lessons(lessons, data['start_date'], data['end_date'], period['boundary'])
                groups = legacy_summary_groups(journal)
                summaries = [sg for key, group in groups.items() if f'legacy:{key}' in data['lesson_ids'] for sg in group['grades'].values()]
                summaries = filter_summary_sources(summaries, data['source'])
                if data['source_types']:
                    lessons = [l for l in lessons if l.lesson_type in data['source_types']]
                    summaries = [s for s in summaries if s.grade_type in data['source_types']]
                target = journal.lessons.filter(pk=data['target_lesson']).first() if data['target_lesson'] else None
                if target and target.lesson_type != data['grade_type']:
                    raise ValueError('Тип вибраної колонки має відповідати типу розрахунку.')
                lessons = [l for l in lessons if not target or l.pk != target.pk]
                rows, fingerprint, used_lessons = calculate(journal, lessons, data['source'], data['rounding'], summaries)
                if not used_lessons and not summaries:
                    raise ValueError('У вибраному періоді немає уроків відповідного типу. Змініть дати, джерело або вибір тем.')
                if used_lessons and data['date'] < max(l.date for l in used_lessons):
                    raise ValueError('Підсумкова колонка має бути після останнього уроку, що враховується.')
                snapshot = {
                    'journal': journal.pk, 'fingerprint': fingerprint,
                    'selection': {k: str(v) for k, v in data.items()},
                    'period_boundary': (
                        (period['boundary'].pk, period['boundary'].date.isoformat(), period['boundary'].lesson_number)
                        if compact and period['boundary'] else None),
                    'target_version': target.updated_at.isoformat() if target else None,
                    'target_grades': list(target.grades.order_by('pk').values_list('pk', 'value', 'updated_at')) if target else [],
                }
                # Datetime values are converted before signing, never written to external services.
                snapshot['target_grades'] = [(pk, val, ts.isoformat()) for pk, val, ts in snapshot['target_grades']]
                preview_token = signing.dumps(snapshot, salt='journal-assessment')
                if request.POST.get('action') == 'save':
                    try:
                        previous = signing.loads(request.POST.get('preview_token', ''), salt='journal-assessment', max_age=1800)
                    except signing.BadSignature:
                        raise ValueError('Перегляньте новий розрахунок перед збереженням.')
                    # JSON restores tuples as lists; compare the canonical serialized snapshot.
                    if previous != signing.loads(preview_token, salt='journal-assessment'):
                        raise ValueError('Оцінки або параметри змінилися. Перегляньте оновлений розрахунок і збережіть ще раз.')
                    values = {}
                    for row in rows:
                        value = request.POST.get(f"grade_{row['student'].pk}", row['value']).strip().upper()
                        if not _valid_grade(value):
                            raise ValueError('Підсумкова оцінка має бути від 1 до 12 або спеціальною позначкою.')
                        values[row['student'].pk] = value
                    if not target:
                        target = Lesson.objects.create(journal=journal, date=data['date'],
                            lesson_number=next_lesson_number(journal, data['date']), lesson_time=data['lesson_time'],
                            topic=data['title'] or dict(LessonType.choices)[data['grade_type']], lesson_type=data['grade_type'],
                            semester=journal.academic_year.semesters.filter(start_date__lte=data['date'], end_date__gte=data['date']).first(),
                            created_by=request.user, is_synced_to_nz=False)
                    for row in rows:
                        student = row['student']
                        grade = Grade.objects.filter(lesson=target, student=student).first()
                        value = values[student.pk]
                        if not value and not grade:
                            continue
                        old = grade.value if grade else ''
                        if grade is None:
                            grade = Grade(lesson=target, student=student)
                        if old == value:
                            continue
                        grade.value, grade.created_by, grade.is_synced_to_nz = value, request.user, False
                        grade.save()
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='Grade', target_id=str(grade.pk), description='Збережено розраховану підсумкову оцінку.',
                            changes={'value': {'old': old, 'new': value}, 'source_lessons': [l.pk for l in used_lessons],
                                     'source_summaries': [sg.pk for sg in summaries],
                                     'source': data['source'], 'rounding': data['rounding']})
                    messages.success(request, 'Підсумкові оцінки збережено локально в окремій колонці журналу.')
                    if compact:
                        return JsonResponse({'redirect': reverse('portal:journal_detail', kwargs={'journal_id': journal.pk})})
                    return redirect('portal:journal_detail', journal_id=journal.pk)
        except ValueError as exc:
            form.add_error(None, str(exc))
    return render(request, 'portal/journal/partials/assessment_modal.html' if compact else 'portal/journal/assessment.html', {
        'journal': journal, 'form': form, 'rows': rows, 'preview_token': preview_token,
        'assessment_title': {
            LessonType.THEMATIC: 'Розрахувати тематичну оцінку',
            LessonType.MODULAR: 'Розрахувати модульну оцінку',
            LessonType.THEMATIC_NUSH: 'Розрахувати тематичну оцінку НУШ',
            LessonType.SEMESTER_1: 'Розрахувати семестрову оцінку',
            LessonType.SEMESTER_2: 'Розрахувати семестрову оцінку',
            LessonType.SEMESTER_1_NUSH: 'Розрахувати семестрову оцінку НУШ',
            LessonType.SEMESTER_2_NUSH: 'Розрахувати семестрову оцінку НУШ',
            LessonType.ANNUAL: 'Розрахувати річну оцінку',
        }[requested_type],
        'is_thematic': is_thematic,
        'assessment_period_label': ('Дати оцінок, які враховуються до модульної' if requested_type == LessonType.MODULAR else
                                    'Дати оцінок, які враховуються до тематичної'),
        'assessment_period': period,
        'assessment_periods': {
            'year_start': journal.academic_year.start_date.isoformat(),
            'year_end': journal.academic_year.end_date.isoformat(),
            'semesters': [{'id': s.pk, 'number': s.number, 'start': s.start_date.isoformat(), 'end': s.end_date.isoformat()}
                          for s in journal.academic_year.semesters.all()],
            'boundaries': [{'id': l.pk, 'date': l.date.isoformat(), 'number': l.lesson_number, 'type': l.lesson_type}
                           for l in journal.lessons.filter(lesson_type__in=SUMMARY_TYPES, grades__value__regex=r'\S').distinct()],
        },
        'assessment_sources': [{'id': str(l.pk), 'type': l.lesson_type, 'date': l.date.isoformat()} for l in all_lessons] + [
            {'id': f'legacy:{key}', 'type': next(iter(group['grades'].values())).grade_type}
            for key, group in legacy_summary_groups(journal).items()
        ],
    })


@login_required
@require_http_methods(['GET', 'POST'])
def topics_copy(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    initial_manual = {
        'source_kind': 'lessons', 'source_start': 1, 'target_start': 1,
        'target_kind': 'lessons' if journal.lessons.exclude(lesson_type__in=SUMMARY_TYPES).exists() else 'plan',
        'include_homework': True, 'replace_existing': True
    }
    initial_sync = {
        'direction': 'from_other',
        'include_homework': True,
        'replace_existing': True,
        'sync_mode': 'auto',
    }

    mode = request.POST.get('mode', '')
    if not mode:
        if request.method == 'POST' and ('source_start' in request.POST or ('source_journal' in request.POST and 'other_journal' not in request.POST)):
            mode = 'manual'
        elif request.method == 'GET' and request.GET.get('mode') == 'manual':
            mode = 'manual'
        else:
            mode = 'sync'

    sync_form = CalendarSyncForm(
        request.POST if request.method == 'POST' and mode == 'sync' else None,
        journal=journal, user=request.user, initial=initial_sync
    )
    manual_form = TopicCopyForm(
        request.POST if request.method == 'POST' and mode == 'manual' else None,
        journal=journal, user=request.user, initial=initial_manual
    )

    rows, preview_token, counts, stats, source, target = None, '', None, None, None, None

    if request.method == 'POST':
        if mode == 'sync' and sync_form.is_valid():
            data = sync_form.cleaned_data
            other = data['other_journal']
            if data['direction'] == 'to_other':
                source, target = journal, other
            else:
                source, target = other, journal

            try:
                with transaction.atomic():
                    locked = {j.pk: j for j in Journal.objects.select_for_update().filter(
                        pk__in=[source.pk, target.pk]).order_by('pk')}
                    if len(locked) != 2:
                        raise ValueError('Один із журналів видалено. Оновіть сторінку й оберіть клас ще раз.')
                    source, target = locked[source.pk], locked[target.pk]
                    if not has_journal_access(request.user, source) or not has_journal_access(request.user, target, write=True):
                        raise PermissionDenied('Немає прав на запис у цільовий журнал.')

                    rows, fingerprint, counts, stats = preview_calendar_sync(source, target, data, lock=True)
                    snapshot = {
                        'user': request.user.pk,
                        'source': source.pk,
                        'target': target.pk,
                        'mode': 'sync',
                        'selection': {k: str(v) for k, v in data.items() if k != 'other_journal'},
                        'fingerprint': fingerprint
                    }
                    preview_token = signing.dumps(snapshot, salt='journal-calendar-sync')

                    if request.POST.get('action') == 'save':
                        try:
                            previous = signing.loads(request.POST.get('preview_token', ''), salt='journal-calendar-sync', max_age=1800)
                        except signing.BadSignature:
                            raise ValueError('Спочатку виконайте порівняння тем перед збереженням.') from None
                        if previous != snapshot:
                            raise ValueError('Журнали або параметри змінилися після порівняння. Порівняйте теми ще раз.')
                        if not counts['create'] and not counts['update']:
                            raise ValueError('Немає тем для оновлення чи додавання. Календарні плани вже збігаються.')
                        apply_calendar_sync(source, target, data, rows, request.user)
                        messages.success(
                            request,
                            f'Календарні теми успішно синхронізовано з «{source.student_class.name}» до «{target.student_class.name}». '
                            f'Додано до плану: {counts["create"]}, оновлено: {counts["update"]}, без змін: {counts["skip"]}.'
                        )
                        return redirect('portal:journal_detail', journal_id=target.pk)
                    if request.POST.get('action') != 'preview':
                        raise ValueError('Оберіть порівняння або застосування змін.')
            except (ValueError, IntegrityError) as exc:
                sync_form.add_error(None, str(exc) if isinstance(exc, ValueError) else 'Журнал змінився під час синхронізації. Порівняйте ще раз.')
                rows, preview_token, counts, stats = None, '', None, None

        elif mode == 'manual' and manual_form.is_valid():
            data = manual_form.cleaned_data
            source = data['source_journal']
            target = journal
            try:
                with transaction.atomic():
                    locked = {j.pk: j for j in Journal.objects.select_for_update().filter(
                        pk__in=[source.pk, target.pk]).order_by('pk')}
                    if len(locked) != 2:
                        raise ValueError('Один із журналів видалено. Оновіть сторінку й оберіть джерело ще раз.')
                    source, target = locked[source.pk], locked[target.pk]
                    if not has_journal_access(request.user, source) or not has_journal_access(request.user, target, write=True):
                        raise PermissionDenied('Немає доступу до вибраних журналів.')
                    rows, fingerprint, counts = preview_topic_copy(source, target, data, lock=True)
                    selection = {key: value for key, value in data.items() if key != 'source_journal'}
                    snapshot = {'user': request.user.pk, 'source': source.pk, 'target': target.pk,
                                'selection': selection, 'fingerprint': fingerprint}
                    preview_token = signing.dumps(snapshot, salt='journal-topic-copy')
                    if request.POST.get('action') == 'save':
                        try:
                            previous = signing.loads(request.POST.get('preview_token', ''), salt='journal-topic-copy', max_age=1800)
                        except signing.BadSignature:
                            raise ValueError('Спочатку перегляньте відповідність тем і номерів.') from None
                        if previous != snapshot:
                            raise ValueError('Журнали або параметри змінилися після перегляду. Перегляньте відповідність ще раз.')
                        if not counts['create'] and not counts['update']:
                            raise ValueError('Немає тем для збереження. Перевірте діапазон і дозвіл заміни заповнених тем.')
                        apply_topic_copy(source, target, data, rows, request.user)
                        messages.success(request, f'Теми збережено локально. Додано: {counts["create"]}, оновлено: {counts["update"]}, пропущено: {counts["skip"]}.')
                        return redirect('portal:journal_detail', journal_id=target.pk)
                    if request.POST.get('action') != 'preview':
                        raise ValueError('Оберіть попередній перегляд або збереження тем.')
            except (ValueError, IntegrityError) as exc:
                manual_form.add_error(None, str(exc) if isinstance(exc, ValueError) else 'Журнал змінився під час копіювання. Перегляньте відповідність ще раз.')
                rows, preview_token, counts = None, '', None

    return render(request, 'portal/journal/topic_copy.html', {
        'journal': journal,
        'mode': mode,
        'form': manual_form,
        'manual_form': manual_form,
        'sync_form': sync_form,
        'rows': rows,
        'preview_token': preview_token,
        'counts': counts,
        'stats': stats,
        'source': source,
        'target': target,
    })


@login_required
@require_POST
def toggle_student_sync_ignore(request, journal_id, student_id):
    """
    Перемикання ігнорування учня для синхронізації (сімейна форма, за кордоном тощо).
    Ігноровані учні виключаються з передачі списків, імпорту оцінок та вивантаження nz.ua,
    а також захищені від випадкового виставлення оцінок.
    """
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    student = _student_for_journal(journal, student_id)
    with transaction.atomic():
        journal = Journal.objects.select_for_update().get(pk=journal.pk)
        if not has_journal_access(request.user, journal, write=True):
            raise PermissionDenied('Доступ до журналу змінився.')
        if journal.ignored_students.filter(pk=student.pk).exists():
            journal.ignored_students.remove(student)
            is_ignored = False
            msg = f"Учня {student.get_short_name_ukr()} повернено до синхронізації та оцінювання."
        else:
            journal.ignored_students.add(student)
            is_ignored = True
            msg = f"Учня {student.get_short_name_ukr()} виключено із синхронізації (сімейна форма / за кордоном)."

        AuditLog.objects.create(
            school=journal.school,
            user=request.user,
            action=AuditLog.Action.UPDATE,
            target_model='Journal',
            target_id=str(journal.pk),
            description=msg,
            changes={'ignored_student_id': student.pk, 'is_ignored': is_ignored}
        )

    from apps.journals.services.roster_transfer import send_journal_roster
    from apps.journals.services.external_grades import ExternalAPIError
    warnings = []
    for connection in journal.grade_apis.filter(enabled=True, contract_version=2):
        if 'journal_roster_import' not in connection.roster_cache.get('capabilities', []):
            continue
        try:
            report = send_journal_roster(connection, request.user)
            if report['counts'].get('conflicts') or report['counts'].get('needs_review'):
                warnings.append(f'{connection.name}: перевірте відповідності на сторінці підключення.')
        except ExternalAPIError as error:
            warnings.append(f'{connection.name}: {error} Повторіть передачу списку після відновлення зв’язку.')
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({'status': 'ok', 'is_ignored': is_ignored, 'message': msg, 'warnings': warnings})
    for warning in warnings:
        messages.warning(request, warning)
    messages.success(request, msg)
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def sync_extra_topics(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    parent = extra_parent(journal)
    if not parent:
        messages.error(request, 'Це не окремий позакласний журнал.')
        return redirect('portal:journal_detail', journal_id=journal.pk)
    from apps.journals.services.extra_students import sync_topics_from_main_journal
    count = sync_topics_from_main_journal(journal, parent)
    messages.success(request, f'Теми уроків успішно підтягнуто з основного журналу «{parent.student_class.name}» (додано/оновлено уроків: {count}).')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def merge_journal_student(request, journal_id, student_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    source_student = _student_for_journal(journal, student_id)
    target_student_id = request.POST.get('target_student_id')
    if not target_student_id or not str(target_student_id).isdigit():
        messages.error(request, 'Оберіть учня, з яким потрібно об’єднати.')
        return redirect('portal:journal_detail', journal_id=journal.pk)

    target_student = _student_for_journal(journal, int(target_student_id))
    if source_student.pk == target_student.pk:
        messages.error(request, 'Не можна об’єднати учня із самим собою.')
        return redirect('portal:journal_detail', journal_id=journal.pk)

    source_name = source_student.get_full_name_ukr()
    target_name = target_student.get_full_name_ukr()

    with transaction.atomic():
        # 1. Transfer Grades in this journal's lessons
        for grade in Grade.objects.filter(student=source_student, lesson__journal=journal):
            target_grade = Grade.objects.filter(student=target_student, lesson=grade.lesson).first()
            if not target_grade:
                grade.student = target_student
                grade.save(update_fields=['student', 'updated_at'])
            else:
                if not target_grade.value and grade.value:
                    target_grade.value = grade.value
                    target_grade.comment = grade.comment or target_grade.comment
                    target_grade.save(update_fields=['value', 'comment', 'updated_at'])
                grade.delete()

        # 2. Transfer Attendances
        for att in Attendance.objects.filter(student=source_student, lesson__journal=journal):
            target_att = Attendance.objects.filter(student=target_student, lesson=att.lesson).first()
            if not target_att:
                att.student = target_student
                att.save(update_fields=['student', 'updated_at'])
            else:
                att.delete()

        # 3. Transfer SummaryGrades
        for sgrade in SummaryGrade.objects.filter(student=source_student, journal=journal):
            target_sg = SummaryGrade.objects.filter(
                student=target_student, journal=journal,
                grade_type=sgrade.grade_type, semester=sgrade.semester,
                thematic_name=sgrade.thematic_name
            ).first()
            if not target_sg:
                sgrade.student = target_student
                sgrade.save(update_fields=['student', 'updated_at'])
            else:
                if not target_sg.value and sgrade.value:
                    target_sg.value = sgrade.value
                    target_sg.save(update_fields=['value', 'updated_at'])
                sgrade.delete()

        # 4. Transfer ExternalGrade
        ExternalGrade.objects.filter(student=source_student, lesson__journal=journal).update(student=target_student)

        # 5. Transfer GradeStudentMapping
        from apps.journals.models import GradeStudentMapping, ExtraStudent
        for mapping in GradeStudentMapping.objects.filter(student=source_student):
            if not GradeStudentMapping.objects.filter(connection=mapping.connection, external_id=mapping.external_id, student=target_student).exists():
                mapping.student = target_student
                mapping.save(update_fields=['student'])
            else:
                mapping.delete()

        # 6. Update ExtraStudent references
        ExtraStudent.objects.filter(matched_student=source_student).update(matched_student=target_student)
        ExtraStudent.objects.filter(separate_student=source_student).update(separate_student=target_student)

        # 7. Clean up ignored students
        if source_student in journal.ignored_students.all():
            journal.ignored_students.remove(source_student)

        # 8. Remove ClassEnrollment of source_student in this class
        ClassEnrollment.objects.filter(student=source_student, student_class=journal.student_class).delete()

        # 9. Clean up duplicate Student record if no other references exist
        has_other_enrollments = ClassEnrollment.objects.filter(student=source_student).exists()
        has_other_grades = Grade.objects.filter(student=source_student).exists()
        has_other_att = Attendance.objects.filter(student=source_student).exists()
        has_other_summaries = SummaryGrade.objects.filter(student=source_student).exists()
        deleted_student_record = False
        if not (has_other_enrollments or has_other_grades or has_other_att or has_other_summaries):
            source_student.delete()
            deleted_student_record = True

        AuditLog.objects.create(
            school=journal.school,
            user=request.user,
            action=AuditLog.Action.UPDATE,
            target_model='Student',
            target_id=str(target_student.pk),
            description=f'Об’єднано дублікат учня «{source_name}» в «{target_name}».',
            changes={
                'journal_id': journal.pk,
                'source_student_id': student_id,
                'target_student_id': target_student.pk,
                'deleted_duplicate_record': deleted_student_record
            }
        )

    messages.success(request, f'Учня «{source_name}» успішно об’єднано з «{target_name}». Усі оцінки перенесено.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


@login_required
@require_POST
def delete_journal_student(request, journal_id, student_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    student = _student_for_journal(journal, student_id)
    student_name = student.get_full_name_ukr()

    with transaction.atomic():
        parent = extra_parent(journal)
        if parent:
            from apps.journals.services.extra_students import delete_separate_student
            try:
                delete_separate_student(journal, student, request.user)
                messages.success(request, f'Позакласного учня «{student_name}» та його оцінки видалено з журналу.')
            except Exception as e:
                messages.error(request, str(e))
            return redirect('portal:journal_detail', journal_id=journal.pk)

        # Delete grades, attendances, summaries in this journal
        Grade.objects.filter(student=student, lesson__journal=journal).delete()
        Attendance.objects.filter(student=student, lesson__journal=journal).delete()
        SummaryGrade.objects.filter(student=student, journal=journal).delete()
        ExternalGrade.objects.filter(student=student, lesson__journal=journal).delete()

        if student in journal.ignored_students.all():
            journal.ignored_students.remove(student)

        # Remove class enrollment for this class
        ClassEnrollment.objects.filter(student=student, student_class=journal.student_class).delete()

        # Remove mappings and extra student links for this journal's connections
        from apps.journals.models import GradeStudentMapping, ExtraStudent
        GradeStudentMapping.objects.filter(connection__journal=journal, student=student).delete()
        ExtraStudent.objects.filter(connection__journal=journal, matched_student=student).update(
            matched_student=None, is_separate=True
        )

        # Clean up Student record if no other references exist
        has_other_enrollments = ClassEnrollment.objects.filter(student=student).exists()
        has_other_grades = Grade.objects.filter(student=student).exists()
        has_other_att = Attendance.objects.filter(student=student).exists()
        has_other_summaries = SummaryGrade.objects.filter(student=student).exists()
        if not (has_other_enrollments or has_other_grades or has_other_att or has_other_summaries):
            student.delete()

        AuditLog.objects.create(
            school=journal.school,
            user=request.user,
            action=AuditLog.Action.DELETE,
            target_model='Student',
            target_id=str(student_id),
            description=f'Вилучено учня «{student_name}» з журналу {journal.student_class.name}.',
            changes={'journal_id': journal.pk, 'student_id': student_id}
        )

    messages.success(request, f'Учня «{student_name}» вилучено з цього журналу.')
    return redirect('portal:journal_detail', journal_id=journal.pk)


