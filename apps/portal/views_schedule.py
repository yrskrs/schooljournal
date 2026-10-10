import datetime
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseBadRequest, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, LessonTime, WeeklyScheduleEntry
from apps.curriculum.timetable import build_week_schedule
from apps.journals.models import Journal, AuditLog
from apps.portal.forms_schedule import LessonTimeForm, WeeklyScheduleForm
from apps.portal.permissions import get_user_accessible_schools
from apps.schools.models import School


def _selected(queryset, value):
    try:
        pk = int(value)
        if pk <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise Http404('Некоректний вибір.') from None
    return get_object_or_404(queryset, pk=pk)


def _school_scope(request):
    schools = get_user_accessible_schools(request.user).order_by('name', 'pk')
    parameters = request.POST if request.method == 'POST' else request.GET
    school_id = parameters.get('school_id')
    school = _selected(schools, school_id) if school_id else schools.first()
    if not school:
        raise PermissionDenied('Для роботи з розкладом оберіть або створіть школу.')
    return school, schools


def _url(name, **parameters):
    return reverse('portal:' + name) + '?' + urlencode({key: value for key, value in parameters.items() if value not in (None, '')})


@login_required
@require_http_methods(['GET', 'POST'])
def lesson_times(request):
    school, schools = _school_scope(request)
    slots = LessonTime.objects.filter(school=school)
    parameters = request.POST if request.method == 'POST' else request.GET
    action = request.POST.get('action', 'save')
    edit_id = parameters.get('slot_id') if request.method == 'POST' else parameters.get('edit')
    instance = _selected(slots, edit_id) if edit_id else None
    destination = _url('lesson_times', school_id=school.pk)
    if request.method == 'POST' and action == 'delete':
        if not instance:
            return HttpResponseBadRequest('Оберіть час уроку.')
        try:
            with transaction.atomic():
                School.objects.select_for_update().get(pk=school.pk)
                instance.delete()
                AuditLog.objects.create(school=school, user=request.user, action=AuditLog.Action.DELETE,
                    target_model='LessonTime', target_id=str(edit_id), description='Видалено час уроку зі шкільного списку.')
            messages.success(request, 'Час уроку видалено. Час уже створених уроків збережено.')
        except ProtectedError:
            messages.error(request, 'Цей час використовується в тижневому розкладі. Зніміть позначку «Доступний для нових уроків» або спочатку змініть розклад.')
        return redirect(destination)
    if request.method == 'POST' and action != 'save':
        return HttpResponseBadRequest('Невідома дія.')
    initial = {'number': max(slots.values_list('number', flat=True), default=0) + 1}
    form = LessonTimeForm(request.POST if request.method == 'POST' else None, instance=instance, school=school, initial=initial)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                School.objects.select_for_update().get(pk=school.pk)
                if form.is_valid():
                    slot = form.save()
                    AuditLog.objects.create(school=school, user=request.user,
                        action=AuditLog.Action.UPDATE if instance else AuditLog.Action.CREATE,
                        target_model='LessonTime', target_id=str(slot.pk), description='Збережено час уроку для школи.')
                    messages.success(request, 'Час уроку збережено. Список спільний для всіх учителів школи.')
                    return redirect(destination)
        except IntegrityError:
            form.add_error(None, 'Такий номер або час уроку вже додано. Оновіть список.')
    return render(request, 'portal/teacher/lesson_times.html', {
        'school': school, 'schools': schools, 'slots': slots, 'form': form, 'editing_slot': instance,
        'schedule_url': _url('weekly_schedule', school_id=school.pk), 'times_url': destination,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def weekly_schedule(request):
    school, schools = _school_scope(request)
    parameters = request.POST if request.method == 'POST' else request.GET
    teachers = User.objects.filter(school=school, role=User.Role.TEACHER).order_by('last_name', 'first_name', 'pk')
    if request.user.is_school_admin_user:
        teacher = _selected(teachers, parameters['teacher_id']) if parameters.get('teacher_id') else teachers.first()
    else:
        if parameters.get('teacher_id') and parameters['teacher_id'] != str(request.user.pk):
            raise PermissionDenied('Ви можете змінювати лише власний розклад.')
        teacher = request.user
    journals = Journal.objects.filter(school=school).filter(Q(teacher=teacher) | Q(nz_owner__user=teacher) | Q(local_profile=teacher)).select_related('academic_year', 'student_class', 'subject')
    years = AcademicYear.objects.filter(school=school, pk__in=journals.values('academic_year_id')).order_by('-start_date', '-pk')
    today = timezone.localdate()
    if parameters.get('year_id'):
        year = _selected(years, parameters['year_id'])
    else:
        year = next((y for y in years if y.start_date <= today <= y.end_date), None) or years.filter(is_current=True).first() or years.first()
    journals = journals.filter(academic_year=year) if year else journals.none()
    editable = journals.filter(is_archived=False)
    date = max(year.start_date, min(today, year.end_date)) if year else today
    try:
        if parameters.get('week'):
            date = datetime.date.fromisoformat(parameters['week'])
        week_start = date - datetime.timedelta(days=date.weekday())
        week_end = week_start + datetime.timedelta(days=6)
        previous_week = week_start - datetime.timedelta(days=7)
        next_week = week_start + datetime.timedelta(days=7)
    except (ValueError, OverflowError):
        return HttpResponseBadRequest('Оберіть коректну дату тижня.')
    scope = {'school_id': school.pk, 'teacher_id': teacher.pk if teacher else None, 'year_id': year.pk if year else None}
    destination = _url('weekly_schedule', **scope, week=week_start.isoformat())
    entries = WeeklyScheduleEntry.objects.filter(journal__in=journals).select_related(
        'journal__student_class', 'journal__subject', 'lesson_time')
    edit_id = parameters.get('entry_id') if request.method == 'POST' else parameters.get('edit')
    instance = _selected(entries.filter(journal__in=editable), edit_id) if edit_id else None
    action = request.POST.get('action', 'save')
    if request.method == 'POST' and action == 'delete':
        if not instance:
            return HttpResponseBadRequest('Оберіть урок розкладу.')
        with transaction.atomic():
            School.objects.select_for_update().get(pk=school.pk)
            instance.delete()
            AuditLog.objects.create(school=school, user=request.user, action=AuditLog.Action.DELETE,
                target_model='WeeklyScheduleEntry', target_id=str(edit_id), description='Видалено урок із тижневого розкладу.')
        messages.success(request, 'Урок прибрано з розкладу. Записи в журналі збережено.')
        return redirect(destination)
    if request.method == 'POST' and action != 'save':
        return HttpResponseBadRequest('Невідома дія.')
    form = WeeklyScheduleForm(request.POST if request.method == 'POST' else None, instance=instance,
        journals=editable, school=school,
        initial={'start_date': year.start_date if year else None, 'end_date': year.end_date if year else None})
    if request.method == 'POST':
        try:
            with transaction.atomic():
                School.objects.select_for_update().get(pk=school.pk)
                if form.is_valid():
                    entry = form.save()
                    AuditLog.objects.create(school=school, user=request.user,
                        action=AuditLog.Action.UPDATE if instance else AuditLog.Action.CREATE,
                        target_model='WeeklyScheduleEntry', target_id=str(entry.pk), description='Збережено урок у тижневому розкладі.')
                    messages.success(request, 'Урок додано до тижневого розкладу.' if not instance else 'Розклад оновлено.')
                    return redirect(destination)
        except IntegrityError:
            form.add_error(None, 'Розклад змінив інший користувач. Оновіть сторінку.')
    days, rows = build_week_schedule(school, journals, week_start)
    today_date = max(year.start_date, min(today, year.end_date)) if year else today
    return render(request, 'portal/teacher/weekly_schedule.html', {
        'school': school, 'schools': schools, 'teachers': teachers, 'selected_teacher': teacher,
        'years': years, 'selected_year': year, 'scope': scope,
        'week_start': week_start, 'week_end': week_end, 'today': today,
        'days': days, 'schedule_rows': rows, 'entries': entries, 'form': form, 'editing_entry': instance,
        'can_configure': editable.exists(), 'has_times': LessonTime.objects.filter(school=school, is_active=True).exists(),
        'schedule_url': destination, 'times_url': _url('lesson_times', school_id=school.pk),
        'previous_url': _url('weekly_schedule', **scope, week=previous_week.isoformat()),
        'next_url': _url('weekly_schedule', **scope, week=next_week.isoformat()),
        'current_url': _url('weekly_schedule', **scope, week=today_date.isoformat()),
    })
