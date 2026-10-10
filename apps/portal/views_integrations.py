import hashlib
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST, require_GET

from apps.accounts.personal import ensure_personal_account
from apps.journals.models import Journal, Grade, Attendance, StudentAPIGrant, GradeAPIConnection, ExternalGrade, AuditLog
from apps.journals.services.assessment import journal_students
from apps.journals.services.external_grades import fetch_grade_payload, import_grade_records, review_external_grade, ExternalAPIError
from apps.portal.forms_integrations import StudentAPIForm, GradeAPIConnectionForm, PullGradesForm, LocalProfileLinkForm, STUDENT_FIELDS
from apps.portal.views_journal import _get_journal_or_403
from apps.portal.permissions import has_journal_access


@require_GET
def student_list_api(request, journal_id):
    header = request.headers.get('Authorization', '')
    token = header[7:] if header.startswith('Bearer ') else ''
    grant = StudentAPIGrant.objects.filter(journal_id=journal_id, enabled=True).select_related(
        'journal__student_class').first()
    if not grant or not token or not grant.token_hash or not constant_time_compare(
            hashlib.sha256(token.encode()).hexdigest(), grant.token_hash) or (
            grant.expires_at and grant.expires_at <= timezone.now()):
        response = JsonResponse({'error': 'Недійсний, вимкнений або прострочений ключ доступу.'}, status=401)
        response['WWW-Authenticate'] = 'Bearer'
    else:
        students = []
        fields = set(grant.fields) & {key for key, label in STUDENT_FIELDS}
        for student in journal_students(grant.journal).filter(enrollments__student_class=grant.journal.student_class,
                enrollments__is_active=True).distinct():
            values = {'id': student.pk, 'nz_student_id': student.nz_student_id, 'internal_id': student.internal_id,
                'name': student.get_full_name_ukr(), 'class': grant.journal.student_class.name, 'status': student.status}
            students.append({key: values[key] for key in sorted(fields)})
        response = JsonResponse({'schema_version': 1, 'journal_id': grant.journal_id, 'students': students})
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('api_key', 'password')
def journal_integrations(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=request.method == 'POST')
    destination = reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    grant = StudentAPIGrant.objects.filter(journal=journal).first()
    connections = journal.grade_apis.all().order_by('pk')
    edit = request.POST.get('connection_id') if request.method == 'POST' else request.GET.get('edit')
    instance = get_object_or_404(connections, pk=edit) if edit and str(edit).isdigit() else None
    if edit and instance is None:
        return HttpResponseBadRequest('Некоректне підключення.')
    api_form = StudentAPIForm(initial={'enabled': grant.enabled if grant else False,
        'fields': grant.fields if grant else ['id', 'name'], 'expires_at': grant.expires_at if grant else None})
    connection_form = GradeAPIConnectionForm(instance=instance)
    link_form = LocalProfileLinkForm(request=request)
    can_link = bool(journal.nz_owner_id and (journal.nz_owner.user_id == request.user.pk or request.user.is_superadmin_user))
    can_unlink = bool(journal.local_profile_id and (can_link or journal.local_profile_id == request.user.pk))
    action = request.POST.get('action')
    if request.method == 'POST':
        with transaction.atomic():
            journal = Journal.objects.select_for_update().get(pk=journal.pk)
            if not has_journal_access(request.user, journal, write=True):
                raise PermissionDenied('Доступ до журналу змінився.')
            can_link = bool(journal.nz_owner_id and (journal.nz_owner.user_id == request.user.pk or request.user.is_superadmin_user))
            can_unlink = bool(journal.local_profile_id and (can_link or journal.local_profile_id == request.user.pk))
            if action in ('student_api', 'rotate_key', 'revoke_key'):
                if action == 'student_api':
                    api_form = StudentAPIForm(request.POST)
                    if api_form.is_valid():
                        grant, _ = StudentAPIGrant.objects.get_or_create(journal=journal)
                        grant.enabled = api_form.cleaned_data['enabled']
                        grant.fields = api_form.cleaned_data['fields']
                        grant.expires_at = api_form.cleaned_data['expires_at']
                        grant.save()
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='StudentAPIGrant', target_id=str(grant.pk), description='Змінено доступ або поля API списку учнів.')
                        messages.success(request, 'Налаштування API учнів збережено.')
                        return redirect(destination)
                else:
                    grant, _ = StudentAPIGrant.objects.get_or_create(journal=journal)
                    token = secrets.token_urlsafe(32) if action == 'rotate_key' else ''
                    grant.token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ''
                    if not token:
                        grant.enabled = False
                    grant.save()
                    request.session[f'student_api_key_{journal.pk}'] = token
                    AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                        target_model='StudentAPIGrant', target_id=str(grant.pk), description='Замінено або відкликано ключ API списку учнів.')
                    messages.success(request, 'Новий ключ показано один раз. Попередній ключ більше не працює.' if token else 'Ключ відкликано; API вимкнено.')
                    return redirect(destination)
            elif action == 'connection':
                connection_form = GradeAPIConnectionForm(request.POST, instance=instance)
                if connection_form.is_valid():
                    connection = connection_form.save(commit=False)
                    connection.journal = journal
                    connection.save()
                    AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                        target_model='GradeAPIConnection', target_id=str(connection.pk), description='Збережено підключення API оцінок.')
                    messages.success(request, 'Підключення API оцінок збережено.')
                    return redirect(destination)
            elif action == 'link_profile':
                if not can_link:
                    raise PermissionDenied('Прив’язку змінює власник особистого акаунта nz.ua.')
                link_form = LocalProfileLinkForm(request.POST, request=request)
                if link_form.is_valid():
                    account = ensure_personal_account(journal.nz_owner)
                    journal.refresh_from_db()
                    journal.local_profile = link_form.local_user
                    journal.save(update_fields=['local_profile'])
                    AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                        target_model='Journal', target_id=str(journal.pk), description='Підключено додатковий локальний профіль до журналу.')
                    messages.success(request, 'Локальний профіль підключено. Особистий профіль nz.ua зберігає доступ.')
                    return redirect(destination)
            elif action == 'unlink_profile':
                if not can_unlink:
                    raise PermissionDenied('Ви не можете відв’язати цей профіль.')
                ensure_personal_account(journal.nz_owner)
                journal.refresh_from_db()
                journal.local_profile = None
                journal.teacher = journal.nz_owner.user
                journal.save(update_fields=['local_profile', 'teacher'])
                AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                    target_model='Journal', target_id=str(journal.pk), description='Відв’язано локальний профіль; журнал збережено в особистому акаунті nz.ua.')
                messages.success(request, 'Локальний профіль відв’язано. Уроки й оцінки залишилися в особистому профілі nz.ua.')
                return redirect('portal:teacher_dashboard')
            else:
                return HttpResponseBadRequest('Невідома дія.')
    response = render(request, 'portal/journal/integrations.html', {
        'journal': journal, 'grant': grant, 'api_form': api_form, 'connections': connections,
        'connection_form': connection_form, 'editing_connection': instance, 'link_form': link_form,
        'can_link': can_link, 'can_unlink': can_unlink,
        'can_edit': has_journal_access(request.user, journal, write=True),
        'api_url': request.build_absolute_uri(reverse('portal:student_list_api', kwargs={'journal_id': journal.pk})),
        'new_api_key': request.session.pop(f'student_api_key_{journal.pk}', ''),
        'pull_form': PullGradesForm(initial={'date_from': journal.academic_year.start_date,
            'date_to': max(journal.academic_year.start_date, min(timezone.localdate(), journal.academic_year.end_date))}),
    })
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_POST
def pull_external_grades(request, journal_id, connection_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(GradeAPIConnection, pk=connection_id, journal=journal)
    form = PullGradesForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Оберіть коректні початок і кінець періоду.')
    else:
        try:
            start, end = form.cleaned_data['date_from'], form.cleaned_data['date_to']
            rows = fetch_grade_payload(connection, start, end)
            log = import_grade_records(connection, rows, request.user, start, end)
            counts = log.counts
            messages.success(request, f"Додано: {counts['applied']}; очікують схвалення: {counts['pending']}; конфлікти: {counts['conflicts']}; без змін: {counts['unchanged']}; помилки: {counts['errors']}.")
        except ExternalAPIError as error:
            messages.error(request, str(error))
    return redirect('portal:external_grade_review', journal_id=journal.pk)


@login_required
@require_http_methods(['GET', 'POST'])
def external_grade_review(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=request.method == 'POST')
    if request.method == 'POST':
        record = get_object_or_404(ExternalGrade, pk=request.POST.get('record_id') if str(request.POST.get('record_id', '')).isdigit() else 0,
            connection__journal=journal)
        try:
            review_external_grade(record.pk, journal, request.user, request.POST.get('action'),
                request.POST.get('version'), replace=request.POST.get('replace') == '1')
            messages.success(request, 'Оцінку схвалено.' if request.POST.get('action') == 'approve' else 'Оцінку відхилено.')
        except ExternalAPIError as error:
            messages.error(request, str(error))
        return redirect('portal:external_grade_review', journal_id=journal.pk)
    from django.core.paginator import Paginator
    records = ExternalGrade.objects.filter(connection__journal=journal).select_related('connection', 'student', 'lesson', 'reviewed_by')
    for_review = request.GET.get('all') != '1'
    if for_review:
        records = records.filter(status__in=[ExternalGrade.Status.PENDING, ExternalGrade.Status.CONFLICT])
    page = Paginator(records, 50).get_page(request.GET.get('page'))
    grades = {(grade.lesson_id, grade.student_id): grade for grade in Grade.objects.filter(lesson__journal=journal)}
    for record in page:
        record.review_version = record.updated_at.isoformat()
        record.current_grade = grades.get((record.lesson_id, record.student_id))
        record.current_attendance = Attendance.objects.filter(lesson=record.lesson, student=record.student).exclude(status='PRESENT').first()
        record.reviewable = record.status in (ExternalGrade.Status.PENDING, ExternalGrade.Status.CONFLICT)
    return render(request, 'portal/journal/external_grades.html', {'journal': journal, 'page': page,
        'can_edit': has_journal_access(request.user, journal, write=True),
        'for_review': for_review,
        'pull_logs': [log for connection in journal.grade_apis.all() for log in connection.pull_logs.order_by('-created_at')[:3]]})
