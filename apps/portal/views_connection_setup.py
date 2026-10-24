from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError, models
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import Journal, GradeAPIConnection, GradeStudentMapping, AuditLog, StudentAPIGrant, GradePullLog
from apps.journals.services.assessment import journal_students
from apps.journals.services.connection_setup import (
    fetch_source_roster, fetch_connection_roster, roster_version, selected_students, SOURCES,
    ensure_same_source, existing_source_connection, safely_delete_grade_api_connection,
    group_connections_by_server, find_online_connection_in_group, exchange_with_fallback
)
from apps.journals.services.external_grades import ExternalAPIError
from apps.journals.services.roster_transfer import send_journal_roster
from apps.journals.services.api_inbox import keep_roster_extras
from apps.journals.services.api_messages import log_error_rows
from apps.portal.forms_connection_setup import ConnectSourceForm, SourceContextForm, ConfirmStudentsForm, EditConnectionForm
from apps.portal.permissions import has_journal_access, get_user_journals
from apps.portal.views_journal import _get_journal_or_403


def _lock(journal, user):
    journal = Journal.objects.select_for_update().get(pk=journal.pk)
    if not has_journal_access(user, journal, write=True):
        raise PermissionDenied('Доступ до журналу змінився.')
    return journal


def _initial_context(connection):
    values = {'version': roster_version(connection) if connection.pk else ''}
    for field, kind, current, name in [
        ('source_class_id', 'classes', connection.source_class_id, connection.journal.student_class.name),
        ('source_subject_id', 'subjects', connection.source_subject_id, connection.journal.subject.name),
    ]:
        rows = connection.roster_cache.get(kind, [])
        def normalized(value):
            # Only exact homoglyphs; do not map Latin 'b' to Cyrillic 'в' because
            # Ukrainian classes use letter B for Б or В, and b->в causes 8-B to bind to 8-В!
            table = str.maketrans('aceikmox', 'асеікмох')
            return ''.join(c for c in value.casefold().translate(table) if c.isalnum())
        matches = [row['id'] for row in rows if normalized(row['name']) == normalized(name)]
        values[field] = current if any(row['id'] == current for row in rows) else matches[0] if len(matches) == 1 else ''
    return values


@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('api_key', 'connection_code')
def connect_source(request, journal_id, allow_legacy_address_form=False):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    from apps.journals.services.extra_students import extra_parent
    parent = extra_parent(journal)
    if parent:
        messages.info(request, 'Підключення налаштовується для основного журналу. Позакласні результати зберігаються окремими учнями.')
        return redirect(reverse('portal:connect_source', kwargs={'journal_id': parent.pk}))
    destination = reverse('portal:connect_source', kwargs={'journal_id': journal.pk})
    backup_id = request.POST.get('backup_for') if request.method == 'POST' else request.GET.get('backup_for')
    backup = get_object_or_404(journal.grade_apis, pk=backup_id, contract_version=2) if str(backup_id or '').isdigit() else None
    if backup_id and backup is None:
        raise PermissionDenied('Некоректне основне підключення.')
    connection_id = request.POST.get('connection_id') if request.method == 'POST' else request.GET.get('connection')
    connection = get_object_or_404(journal.grade_apis, pk=connection_id, contract_version=2) if str(connection_id or '').isdigit() else None
    if connection_id and connection is None:
        raise PermissionDenied('Некоректне підключення.')
    if request.method == 'GET' and not connection and not backup and not allow_legacy_address_form:
        return redirect(reverse('portal:connections_matching') + f'?journal={journal.pk}')
    if backup and backup.service_connection_id and not allow_legacy_address_form:
        return redirect('portal:connection_edit', service_id=backup.service_connection_id)
    if (connection and connection.service_connection_id and request.method == 'POST'
            and request.POST.get('action') in ('connect', 'reuse')):
        messages.info(request, 'Адреса й ключ налаштовуються один раз на сторінці «Підключення».')
        return redirect('portal:connection_edit', service_id=connection.service_connection_id)
    setup = ConnectSourceForm(initial={'source': request.GET.get('source', 'schooltest')})
    if backup:
        setup = ConnectSourceForm(initial={'source': backup.source, 'key_name': backup.name[:80] + ' (резервна)'})
    if connection:
        setup = ConnectSourceForm(initial={'source': connection.source, 'key_name': connection.name,
            'site_url': connection.url.rsplit('/api/v2/journal/grades/', 1)[0]})
        setup.fields['api_key'].help_text = 'Залиште порожнім, щоб використати збережений ключ.'
    context = mapping = None
    available = []
    known = set()
    for item in GradeAPIConnection.objects.filter(contract_version=2, journal__is_archived=False).filter(
            Q(journal__teacher=request.user) | Q(journal__nz_owner__user=request.user) | Q(journal__local_profile=request.user)).exclude(journal=journal).select_related('journal'):
        identity = (item.source, item.url, item.api_key)
        if identity not in known and has_journal_access(request.user, item.journal, write=True):
            known.add(identity)
            available.append(item)
    action = request.POST.get('action')
    if request.method == 'POST':
        try:
            if action in ('connect', 'reuse'):
                conn_name = None
                if action == 'reuse':
                    item = next((row for row in available if str(row.pk) == request.POST.get('reuse_connection')), None)
                    if item is None:
                        raise PermissionDenied('Підключення недоступне.')
                    setup = ConnectSourceForm({'source': item.source, 'site_url': item.url, 'api_key': item.api_key})
                    conn_name = item.name
                else:
                    setup_data = request.POST.copy()
                    if connection and not setup_data.get('api_key', '').strip() and not setup_data.get('connection_code', '').strip():
                        setup_data['api_key'] = connection.api_key
                    setup = ConnectSourceForm(setup_data)
                if setup.is_valid():
                    data = setup.cleaned_data
                    if not conn_name:
                        conn_name = data.get('key_name') or SOURCES[data['source']]
                    payload = fetch_source_roster(data['grades_url'], data['api_key'], data['source'])
                    if backup:
                        if data['source'] != backup.source or not backup.roster_cache.get('instance_id'):
                            raise ExternalAPIError('Резервна адреса має належати тому самому сайту з підтвердженою ідентичністю. Спершу перевірте зв’язок основного підключення.')
                        ensure_same_source(backup, payload)
                        if data['grades_url'] == backup.url:
                            raise ExternalAPIError('Цю адресу вже збережено. Вкажіть іншу адресу того самого сайту, наприклад у шкільній мережі.')
                    with transaction.atomic():
                        journal = _lock(journal, request.user)
                        if connection:
                            connection = journal.grade_apis.select_for_update().get(pk=connection.pk)
                            if connection.source != data['source']:
                                raise ExternalAPIError('Це підключення належить іншому джерелу. Поверніться до журналу та підключіть нове джерело окремо.')
                        elif backup:
                            connection = journal.grade_apis.filter(source=backup.source, url=data['grades_url'],
                                contract_version=2, source_class_id=backup.source_class_id,
                                source_subject_id=backup.source_subject_id).first()
                        else:
                            connection = existing_source_connection(journal, data['source'], data['grades_url'], payload)
                        if connection is None:
                            connection = GradeAPIConnection(journal=journal, source=data['source'], contract_version=2,
                                name=conn_name, url=data['grades_url'], enabled=False)
                        else:
                            connection.name = conn_name
                        ensure_same_source(connection, payload)
                        connection.api_key = data['api_key']
                        connection.url = data['grades_url']
                        connection.roster_cache = payload
                        connection.roster_checked_at = timezone.now()
                        if backup:
                            connection.source_class_id = backup.source_class_id
                            connection.source_subject_id = backup.source_subject_id
                            connection.enabled = backup.enabled
                            if not connection.roster_report:
                                connection.roster_report = backup.roster_report
                                connection.roster_sent_at = backup.roster_sent_at
                        if 'journal_roster_import' in payload.get('capabilities', []):
                            initial = _initial_context(connection)
                            if not connection.source_class_id:
                                connection.source_class_id = initial['source_class_id']
                            if not connection.source_subject_id:
                                connection.source_subject_id = initial['source_subject_id']
                        if data.get('server_group'):
                            connection.server_group = data['server_group'].strip()
                        if data.get('location_tag'):
                            connection.location_tag = data['location_tag'].strip()
                        connection.save()
                        GradePullLog.objects.create(connection=connection, user=request.user,
                            counts={'operation': 'connection', 'errors': 0})
                        if backup:
                            for mapping_row in backup.student_mappings.all():
                                copied, _ = GradeStudentMapping.objects.get_or_create(connection=connection,
                                    external_id=mapping_row.external_id, defaults={'student_id': mapping_row.student_id})
                                if copied.student_id != mapping_row.student_id:
                                    raise ExternalAPIError('Ця резервна адреса має інші відповідності учнів. Перевірте наявне підключення перед об’єднанням.')
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='GradeAPIConnection', target_id=str(connection.pk), description='Підключено джерело та отримано назви класів, предметів і учнів. Оцінки не імпортувалися.')

                    apply_to_all = request.POST.get('apply_to_all_classes') == '1' and not backup
                    all_classes_count = 0
                    if apply_to_all:
                        other_journals = list(Journal.objects.filter(
                            school=journal.school,
                            academic_year=journal.academic_year,
                            is_archived=False,
                        ).filter(
                            Q(teacher=request.user) | Q(nz_owner__user=request.user) | Q(local_profile=request.user)
                        ).exclude(pk=journal.pk).select_related('student_class', 'subject'))

                        for other_j in other_journals:
                            from apps.journals.services.extra_students import extra_parent
                            if extra_parent(other_j):
                                continue
                            if not has_journal_access(request.user, other_j, write=True):
                                continue
                            try:
                                with transaction.atomic():
                                    _lock(other_j, request.user)
                                    other_conn = existing_source_connection(other_j, data['source'], data['grades_url'], payload)
                                    if other_conn is None:
                                        other_conn = GradeAPIConnection(
                                            journal=other_j, source=data['source'], contract_version=2,
                                            name=conn_name, url=data['grades_url'], enabled=False
                                        )
                                    else:
                                        other_conn.name = conn_name
                                    other_conn.api_key = data['api_key']
                                    other_conn.url = data['grades_url']
                                    other_conn.roster_cache = payload
                                    other_conn.roster_checked_at = timezone.now()
                                    if 'journal_roster_import' in payload.get('capabilities', []):
                                        other_initial = _initial_context(other_conn)
                                        if not other_conn.source_class_id:
                                            other_conn.source_class_id = other_initial['source_class_id']
                                        if not other_conn.source_subject_id:
                                            other_conn.source_subject_id = other_initial['source_subject_id']
                                    other_conn.save()
                                    AuditLog.objects.create(
                                        school=other_j.school, user=request.user, action=AuditLog.Action.UPDATE,
                                        target_model='GradeAPIConnection', target_id=str(other_conn.pk),
                                        description='Групове підключення джерела для журналу. Оцінки не імпортувалися.'
                                    )
                                if 'journal_roster_import' in payload.get('capabilities', []):
                                    try:
                                        send_journal_roster(other_conn, request.user)
                                    except ExternalAPIError:
                                        pass
                                all_classes_count += 1
                            except Exception:
                                pass

                    if 'journal_roster_import' in payload.get('capabilities', []):
                        try:
                            report = send_journal_roster(connection, request.user)
                            if apply_to_all and all_classes_count > 0:
                                messages.success(request, f'Підключено для поточного журналу та ще {all_classes_count} ваших журналів. Копії учнів передано на сайт.')
                            else:
                                messages.success(request, 'Підключено. Копію учнів журналу передано на сайт. Схожі імена перевірте нижче; оцінки ще не імпортувалися.')
                        except ExternalAPIError as error:
                            if apply_to_all and all_classes_count > 0:
                                messages.warning(request, f'Підключено для {all_classes_count + 1} журналів. {error}')
                            else:
                                messages.warning(request, f'Адресу й ключ збережено. {error}')
                    else:
                        if apply_to_all and all_classes_count > 0:
                            messages.success(request, f'Зв’язок установлено для всіх ваших класів ({all_classes_count + 1} журналів). Оберіть клас і предмет за назвою.')
                        else:
                            messages.success(request, 'Зв’язок установлено. Оберіть клас і предмет за назвою. Для передачі учнів оновіть джерело з GitHub.')
                    return redirect(destination + '?connection=' + str(connection.pk))
            elif action == 'send_roster' and connection:
                resolutions = {}
                for row in connection.roster_report.get('conflicts', []):
                    ref = row.get('journal_id', '')
                    choice = request.POST.get('resolve_' + ref)
                    if choice in ('local', 'journal'):
                        resolutions[ref] = {'choice': choice, 'version': request.POST.get('version_' + ref, '')}
                report = send_journal_roster(connection, request.user, resolutions)
                counts = report['counts']
                messages.success(request, f"Список передано: додано {counts['created']}, оновлено {counts['updated']}, виключено ігнорованих {counts.get('ignored', 0)}, потребують відповідності {counts['needs_review']}, конфлікти {counts['conflicts']}. Оцінки не імпортувалися.")
                return redirect(destination + '?connection=' + str(connection.pk))
            elif action == 'refresh' and connection:
                payload = (fetch_connection_roster(connection) if connection.service_connection_id else
                    fetch_source_roster(connection.url, connection.api_key, connection.source))
                with transaction.atomic():
                    _lock(journal, request.user)
                    connection = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
                    ensure_same_source(connection, payload)
                    connection.roster_cache = payload
                    connection.roster_checked_at = timezone.now()
                    connection.save(update_fields=['roster_cache', 'roster_checked_at'])
                    GradePullLog.objects.create(connection=connection, user=request.user,
                        counts={'operation': 'connection', 'errors': 0})
                    keep_roster_extras(connection)
                messages.success(request, 'Назви класів, предметів та учнів оновлено. Оцінки не імпортувалися.')
                return redirect(destination + '?connection=' + str(connection.pk))
            elif action == 'context' and connection:
                context = SourceContextForm(request.POST, connection=connection)
                if context.is_valid():
                    with transaction.atomic():
                        _lock(journal, request.user)
                        connection = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
                        if context.cleaned_data['version'] != roster_version(connection):
                            raise ExternalAPIError('Списки оновилися. Перезавантажте сторінку та перевірте вибір.')
                        changed = any(getattr(connection, field) != context.cleaned_data[field] for field in ('source_class_id', 'source_subject_id'))
                        if changed and connection.records.exists():
                            raise ExternalAPIError('Для цього підключення вже є оцінки. Їхній клас і предмет змінити не можна.')
                        if changed and connection.source_class_id and connection.source_class_id != context.cleaned_data['source_class_id']:
                            connection.student_mappings.all().delete()
                        connection.source_class_id = context.cleaned_data['source_class_id']
                        connection.source_subject_id = context.cleaned_data['source_subject_id']
                        if changed:
                            connection.enabled = False
                        connection.save(update_fields=['source_class_id', 'source_subject_id', 'enabled'])
                    messages.success(request, 'Клас і предмет вибрано. Перевірте відповідність учнів.')
                    return redirect(destination + '?connection=' + str(connection.pk) + '#students')
            elif action in ('students', 'confirm_exact') and connection and connection.source_class_id and connection.source_subject_id:
                mapping_data = request.POST
                if action == 'confirm_exact':
                    mapping_data = request.POST.copy()
                    exact = ConfirmStudentsForm(connection=connection).exact_matches(connection)
                    for row in selected_students(connection):
                        mapping_data['pupil_' + row['id']] = exact.get(row['id'], '')
                mapping = ConfirmStudentsForm(mapping_data, connection=connection)
                if mapping.is_valid():
                    current_payload = fetch_connection_roster(connection) if connection.service_connection_id else None
                    with transaction.atomic():
                        journal = _lock(journal, request.user)
                        connection = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
                        if mapping.cleaned_data['version'] != roster_version(connection):
                            raise ExternalAPIError('Списки оновилися. Оновіть сторінку перед підтвердженням.')
                        if current_payload is not None:
                            ensure_same_source(connection, current_payload)
                            if current_payload != connection.roster_cache:
                                raise ExternalAPIError('Список на джерелі змінився. Натисніть «Оновити назви класів та учнів» і перевірте відповідності повторно.')
                        local = {str(student.pk): student for student in journal_students(journal)}
                        proposals = []
                        for row in selected_students(connection):
                            choice = mapping.cleaned_data.get('pupil_' + row['id'])
                            if not choice:
                                continue
                            if choice == 'new':
                                parts = row['name'].split()
                                if len(parts) < 2:
                                    raise ExternalAPIError('Для нового учня потрібні прізвище та ім’я. Створіть його у списку журналу й оберіть тут.')
                                student = Student(school=journal.school, last_name=parts[0], first_name=parts[1], middle_name=' '.join(parts[2:]))
                                student.full_clean()
                                student.save()
                                ClassEnrollment.objects.create(student=student, student_class=journal.student_class,
                                    start_date=journal.academic_year.start_date, is_active=True)
                            else:
                                student = local.get(choice)
                                if student is None:
                                    raise ExternalAPIError('Список учнів журналу змінився. Оновіть сторінку.')
                            existing = connection.student_mappings.filter(external_id=row['id']).first()
                            if existing and existing.student_id != student.pk:
                                if connection.records.filter(student_id=existing.student_id).exists():
                                    raise ExternalAPIError('Для цього учня вже є імпортовані оцінки. Переприв’язування заборонене; історію збережено.')
                            proposals.append((row['id'], student))
                        # Remove only changed mappings without imported history, under the journal lock.
                        for external_id, student in proposals:
                            connection.student_mappings.filter(external_id=external_id).exclude(student=student).delete()
                        for external_id, student in proposals:
                            GradeStudentMapping.objects.update_or_create(connection=connection, external_id=external_id, defaults={'student': student})
                        connection.enabled = True
                        connection.save(update_fields=['enabled'])
                        keep_roster_extras(connection)
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='GradeAPIConnection', target_id=str(connection.pk), description=f'Учитель підтвердив {len(proposals)} відповідностей учнів. Оцінки не імпортувалися.')
                    if 'journal_roster_import' in connection.roster_cache.get('capabilities', []):
                        try:
                            send_journal_roster(connection, request.user)
                        except ExternalAPIError as error:
                            messages.warning(request, str(error))
                    messages.success(request, f'Підключення готове. Підтверджено учнів: {len(proposals)}. У журналі натисніть відповідну кнопку синхронізації оцінок.')
                    return redirect('portal:journal_detail', journal_id=journal.pk)
            elif action == 'delete_connection':
                del_id = request.POST.get('connection_id') or (connection.pk if connection else None)
                if not del_id:
                    raise ExternalAPIError('Оберіть підключення для видалення.')
                target_conn = get_object_or_404(journal.grade_apis, pk=del_id)
                with transaction.atomic():
                    _lock(journal, request.user)
                    conn_name = target_conn.name or target_conn.get_source_display()
                    AuditLog.objects.create(
                        school=journal.school, user=request.user, action=AuditLog.Action.DELETE,
                        target_model='GradeAPIConnection', target_id=str(target_conn.pk),
                        description=f'Видалено підключення API {conn_name} ({target_conn.url}) для журналу {journal.student_class.name}.'
                    )
                    target_conn.delete()
                messages.success(request, f'Підключення «{conn_name}» успішно видалено з журналу.')
                return redirect(destination)
            elif action == 'delete_saved_key':
                reuse_id = request.POST.get('reuse_connection')
                if not reuse_id:
                    raise ExternalAPIError('Оберіть збережений ключ для видалення.')
                item = next((row for row in available if str(row.pk) == str(reuse_id)), None)
                if item is None:
                    raise PermissionDenied('Збережений ключ не знайдено або доступ заборонено.')
                del_all = request.POST.get('delete_all_matching') == '1'
                with transaction.atomic():
                    if del_all:
                        matching = GradeAPIConnection.objects.filter(
                            source=item.source, url=item.url,
                            journal__school=journal.school
                        ).filter(
                            Q(journal__teacher=request.user) | Q(journal__nz_owner__user=request.user) | Q(journal__local_profile=request.user)
                        )
                        matching = matching.filter(pk__in=[m.pk for m in matching if m.api_key == item.api_key])
                        count = matching.count()
                        name = item.name or item.get_source_display()
                        for m in matching:
                            AuditLog.objects.create(
                                school=m.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                                target_model='GradeAPIConnection', target_id=str(m.pk),
                                description=f'Групове видалення збереженого ключа API {name} ({m.url}).'
                            )
                        matching.delete()
                        messages.success(request, f'Збережений ключ «{name}» видалено з усіх {count} журналів.')
                    else:
                        name = item.name or item.get_source_display()
                        AuditLog.objects.create(
                            school=item.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                            target_model='GradeAPIConnection', target_id=str(item.pk),
                            description=f'Видалено збережений ключ API {name} ({item.url}).'
                        )
                        item.delete()
                        messages.success(request, f'Збережений ключ «{name}» видалено.')
                return redirect(destination)
            else:
                raise ExternalAPIError('Оберіть дію на сторінці підключення.')
        except (ExternalAPIError, ValidationError, IntegrityError) as error:
            messages.error(request, str(error) if isinstance(error, ExternalAPIError) else 'Не вдалося виконати дію. Перевірте дані; попередні налаштування збережено.')
    if connection:
        context = context or SourceContextForm(connection=connection, initial=_initial_context(connection))
        if connection.source_class_id and connection.source_subject_id:
            mapping = mapping or ConfirmStudentsForm(connection=connection, initial={'version': roster_version(connection)})
    mapped_keys = set(connection.student_mappings.values_list('external_id', flat=True)) if connection else set()
    source_class_name = None
    is_class_mismatched = False
    if connection and connection.source_class_id and connection.roster_cache:
        classes = connection.roster_cache.get('classes', [])
        source_class_name = next((c['name'] for c in classes if str(c['id']) == str(connection.source_class_id)), None)
        if source_class_name and not connection.service_connection_id:
            def _cl(s): return ''.join(ch for ch in s.casefold() if ch.isalnum())
            is_class_mismatched = _cl(source_class_name) != _cl(journal.student_class.name)
    notice_log = None
    if connection:
        log_query = GradePullLog.objects.filter(connection=connection).select_related('connection')
        requested_log = request.GET.get('notice_log')
        if requested_log:
            if not requested_log.isdigit():
                from django.http import HttpResponseBadRequest
                return HttpResponseBadRequest('Некоректний номер звіту.')
            notice_log = get_object_or_404(log_query, pk=requested_log)
        else:
            candidate = log_query.order_by('-created_at', '-pk').first()
            if candidate and candidate.counts.get('errors'):
                notice_log = candidate
    response = render(request, 'portal/journal/connect_source.html', {'journal': journal, 'connection': connection,
        'backup_connection': backup, 'notice_log': notice_log, 'notice_errors': log_error_rows(notice_log),
        'setup_form': setup, 'context_form': context, 'mapping_form': mapping,
        'allow_legacy_address_form': allow_legacy_address_form,
        'source_class_name': source_class_name, 'is_class_mismatched': is_class_mismatched,
        'exact_count': len(mapping.exact_matches(connection)) if mapping else 0,
        'connections': journal.grade_apis.filter(contract_version=2).order_by('source', 'pk'), 'available_sources': available,
        'needs_mapping': bool(connection and any(row['id'] not in mapped_keys for row in selected_students(connection)))})
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_POST
def delete_source_connection(request, journal_id, connection_id):
    """Швидке видалення підключення до API з журналу."""
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id)
    conn_name = connection.name or connection.get_source_display()
    with transaction.atomic():
        _lock(journal, request.user)
        url = connection.url
        AuditLog.objects.create(
            school=journal.school, user=request.user, action=AuditLog.Action.DELETE,
            target_model='GradeAPIConnection', target_id=str(connection.pk),
            description=f'Видалено підключення API {conn_name} ({url}) для журналу {journal.student_class.name}.'
        )
        safely_delete_grade_api_connection(connection)
    messages.success(request, f'Підключення «{conn_name}» видалено. Виставлені оцінки журналу збережено.')
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('portal:connect_source', journal_id=journal.pk)


@login_required
@require_POST
def test_source_connection(request, journal_id, connection_id):
    """
    Діагностика підключення: перевірка доступності сервера, правильності ключа
    та сумісності контракту з чітким та зрозумілим поясненням для вчителя.
    """
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json'

    try:
        if connection.contract_version == 1:
            from apps.journals.services.external_grades import fetch_grade_payload
            fetch_grade_payload(connection, journal.academic_year.start_date, journal.academic_year.end_date)
            GradePullLog.objects.create(connection=connection, user=request.user,
                counts={'operation': 'connection', 'errors': 0})
            msg = 'Зв’язок успішний. API повернув коректний список оцінок. Оцінки не імпортувалися.'
            if is_ajax:
                return JsonResponse({'success': True, 'status': 'ok', 'message': msg})
            messages.success(request, msg)
            return redirect(request.POST.get('next') or reverse('portal:journal_integrations', args=[journal.pk]))
        payload = (fetch_connection_roster(connection) if connection.service_connection_id else
            fetch_source_roster(connection.url, connection.api_key, source=None))
        payload_source = payload.get('source')
        source_switched = False
        if payload_source and payload_source != connection.source:
            if not connection.records.exists() and payload_source in ('schooltest', 'schoolwork'):
                connection.source = payload_source
                source_switched = True
            else:
                source_labels = {'schooltest': 'Сайт тестів (SchoolTest)', 'schoolwork': 'Сайт завдань (SchoolWork)'}
                raise ExternalAPIError(
                    f"Невідповідність типу сайту: це підключення налаштовано як «{connection.get_source_display()}», "
                    f"але сервер повернув дані як «{source_labels.get(payload_source, payload_source)}». "
                    f"Змініть тип джерела у формі редагування підключення."
                )

        with transaction.atomic():
            _lock(journal, request.user)
            connection = journal.grade_apis.select_for_update().get(pk=connection.pk)
            ensure_same_source(connection, payload)
            connection.roster_cache = payload
            connection.roster_checked_at = timezone.now()
            fields_to_update = ['roster_cache', 'roster_checked_at']
            if source_switched:
                connection.source = payload_source
                fields_to_update.append('source')
            connection.save(update_fields=fields_to_update)
            GradePullLog.objects.create(connection=connection, user=request.user,
                counts={'operation': 'connection', 'errors': 0})

        caps = payload.get('capabilities', [])
        can_import = 'journal_roster_import' in caps
        classes_cnt = len(payload.get('classes', []))
        subjects_cnt = len(payload.get('subjects', []))
        students_cnt = len(payload.get('students', []))

        msg = (
            f"✅ Зв'язок успішний! Сервер «{connection.name or connection.get_source_display()}» онлайн. "
            f"Отримано: {classes_cnt} класів, {subjects_cnt} предметів, {students_cnt} учнів. "
            f"{'Підтримується двостороння передача списків.' if can_import else 'Працює на отримання оцінок.'}"
        )
        if source_switched:
            msg += f" (Тип підключення автоматично оновлено на «{connection.get_source_display()}»)"
        if is_ajax:
            return JsonResponse({
                'success': True,
                'status': 'ok',
                'message': msg,
                'classes_count': classes_cnt,
                'subjects_count': subjects_cnt,
                'students_count': students_cnt,
                'can_send_roster': can_import,
                'checked_at': timezone.localtime(connection.roster_checked_at).strftime('%d.%m.%Y %H:%M:%S'),
            })
        messages.success(request, msg)
    except ExternalAPIError as err:
        err_msg = str(err)
        hint = ""
        low = err_msg.lower()
        if any(w in low for w in ('не відповідає', 'не вдалося', 'connection', 'refused', 'failed', 'timeout')):
            hint = "Перевірте, чи запущено сайт тестів/завдань та чи правильно вказано IP-адресу й порт (наприклад http://192.168.1.13:8080)."
        elif any(w in low for w in ('неприйнятий ключ', 'відхилив ключ', 'недійсний ключ', 'невірний ключ', '401', '403', 'авториз')) or 'ключ не' in low:
            hint = "Сайт відхилив ключ API. Перейдіть у налаштування сайту тестування/завдань та створіть новий ключ для цього журналу."
        elif '404' in low or 'не знайдено' in low:
            hint = "Маршрут API не знайдено. Перевірте адресу сайту та оновіть джерело з GitHub."

        full_msg = f"{err_msg} {hint}".strip() if hint and hint not in err_msg else err_msg
        GradePullLog.objects.create(connection=connection, user=request.user,
            counts={'operation': 'connection', 'errors': 1}, errors=[{'message': full_msg}])
        if is_ajax:
            return JsonResponse({
                'success': False,
                'status': 'error',
                'message': full_msg,
                'raw_error': err_msg,
                'hint': hint,
            }, status=400)
        messages.error(request, full_msg)
    except Exception:
        full_msg = 'Помилка перевірки зв’язку. Перевірте адресу сервера та повторіть дію.'
        GradePullLog.objects.create(connection=connection, user=request.user,
            counts={'operation': 'connection', 'errors': 1}, errors=[{'message': full_msg}])
        if is_ajax:
            return JsonResponse({'success': False, 'status': 'error', 'message': full_msg}, status=400)
        messages.error(request, full_msg)

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    return redirect(next_url)


@login_required
@require_POST
def quick_send_roster(request, journal_id, connection_id):
    """Швидка передача актуального списку учнів журналу на сайт джерела в один клік."""
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id, enabled=True)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json'

    try:
        groups = group_connections_by_server(journal.grade_apis.filter(source=connection.source, enabled=True))
        group = next((g for g in groups if any(c.pk == connection.pk for c in g['connections'])), None)
        connection, report = exchange_with_fallback(group['connections'] if group else [connection],
            lambda candidate: send_journal_roster(candidate, request.user),
            preferred_id=connection.pk, selector=find_online_connection_in_group)
        connection.refresh_from_db()
        counts = report['counts']
        msg = (
            f"Список учнів успішно передано на «{connection.name or connection.get_source_display()}»: "
            f"додано {counts['created']}, оновлено {counts['updated']}, "
            f"виключено ігнорованих {counts.get('ignored', 0)}, "
            f"потребують підтвердження {counts['needs_review']}, "
            f"конфліктів {counts['conflicts']}."
        )
        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': msg,
                'level': 'warning' if counts.get('conflicts') or counts.get('needs_review') else 'success',
                'operation': 'roster',
                'attention_url': reverse('portal:journal_integrations', args=[journal.pk]) + '?tab=notifications',
                'counts': counts,
                'sent_at': timezone.localtime(connection.roster_sent_at).strftime('%d.%m.%Y %H:%M:%S') if connection.roster_sent_at else None
            })
        messages.success(request, msg)
    except ExternalAPIError as err:
        msg = f"Помилка передачі списку: {err}"
        GradePullLog.objects.create(connection=connection, user=request.user,
            counts={'operation': 'roster', 'errors': 1}, errors=[{'message': str(err)}])
        if is_ajax:
            return JsonResponse({'success': False, 'operation': 'roster', 'message': msg,
                'attention_url': reverse('portal:journal_integrations', args=[journal.pk]) + '?tab=notifications'}, status=400)
        messages.error(request, msg)
    except Exception:
        msg = 'Не вдалося підтвердити передачу списку. Перевірте діагностику в Центрі API; місцеві дані збережено.'
        if is_ajax:
            return JsonResponse({'success': False, 'operation': 'roster', 'message': msg}, status=400)
        messages.error(request, msg)

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    return redirect(next_url)


@login_required
@require_POST
@sensitive_post_parameters('api_key')
def edit_source_connection(request, journal_id, connection_id):
    """Зручне редагування параметрів підключення: назва, адреса, ключ, активність, клас, предмет."""
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id)
    form = EditConnectionForm(request.POST, connection=connection)

    if form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            _lock(journal, request.user)
            connection = journal.grade_apis.select_for_update().get(pk=connection.pk)
            connection.name = data['name']
            connection.url = data['grades_url']
            if data.get('source') and data['source'] != connection.source:
                if connection.records.exists():
                    messages.error(request, 'Для цього підключення вже є збережені оцінки. Тип джерела змінити не можна.')
                    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
                    return redirect(next_url)
                connection.source = data['source']
            if data.get('api_key'):
                connection.api_key = data['api_key']
            connection.enabled = data['enabled']
            connection.server_group = data.get('server_group', '').strip()
            connection.location_tag = data.get('location_tag', '').strip()
            connection.is_default_for_group = data.get('is_default_for_group', False)
            if connection.is_default_for_group and connection.server_group:
                # Знімаємо прапорець основної адреси з інших адрес цієї ж групи
                journal.grade_apis.filter(server_group=connection.server_group).exclude(pk=connection.pk).update(is_default_for_group=False)

            # Якщо змінено клас і для нього ще немає збережених оцінок
            if data['source_class_id'] and data['source_class_id'] != connection.source_class_id:
                if connection.records.exists():
                    messages.error(request, 'Для цього підключення вже є збережені оцінки. Клас змінити не можна.')
                    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
                    return redirect(next_url)
                connection.source_class_id = data['source_class_id']
                connection.student_mappings.all().delete()

            if data['source_subject_id'] and data['source_subject_id'] != connection.source_subject_id:
                if connection.records.exists():
                    messages.error(request, 'Для цього підключення вже є збережені оцінки. Предмет змінити не можна.')
                    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
                    return redirect(next_url)
                connection.source_subject_id = data['source_subject_id']

            connection.save()
            AuditLog.objects.create(
                school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                target_model='GradeAPIConnection', target_id=str(connection.pk),
                description=f'Оновлено налаштування підключення API {connection.name} ({connection.url}).'
            )
        messages.success(request, f'Налаштування підключення «{connection.name}» збережено.')
    else:
        err_msg = "; ".join(f"{f}: {', '.join(e)}" for f, e in form.errors.items())
        messages.error(request, f'Помилка валідації налаштувань: {err_msg}')

    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    return redirect(next_url)


@login_required
@require_POST
def toggle_source_connection(request, journal_id, connection_id):
    """Швидке увімкнення / вимкнення підключення до API."""
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id)
    with transaction.atomic():
        _lock(journal, request.user)
        connection = journal.grade_apis.select_for_update().get(pk=connection.pk)
        connection.enabled = not connection.enabled
        connection.save(update_fields=['enabled'])
        state_str = 'увімкнено' if connection.enabled else 'тимчасово вимкнено'
        AuditLog.objects.create(
            school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
            target_model='GradeAPIConnection', target_id=str(connection.pk),
            description=f'Підключення API {connection.name} {state_str}.'
        )
    messages.success(request, f'Підключення «{connection.name or connection.get_source_display()}» {state_str}.')
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    return redirect(next_url)



@login_required
def teacher_api_keys(request):
    """
    Окрема сторінка кабінету вчителя для перегляду та керування всіма підключеними API ключами:
    - Зовнішні API підключення (тести schooltest, завдання schoolwork тощо);
    - Групування однакових ключів та перегляд списку прив'язаних журналів;
    - Видалення недійсних або застарілих ключів (як для одного журналу, так і масово для всіх);
    - Керування ключами доступу до списку учнів (StudentAPIGrant).
    """
    if request.user.is_superadmin_user:
        return redirect('portal:superadmin_dashboard')

    return redirect('portal:connections')


@login_required
@require_POST
def delete_api_key_group(request):
    """
    Видалення ключа API зі сторінки 'API ключі' (для одного журналу або з усіх журналів одразу).
    """
    conn_id = request.POST.get('connection_id')
    delete_all = request.POST.get('delete_all') == '1'
    if not conn_id or not str(conn_id).isdigit():
        messages.error(request, 'Некоректний ідентифікатор ключа.')
        return redirect('portal:teacher_api_keys')

    journals = get_user_journals(request.user).filter(
        Q(teacher=request.user) | Q(nz_owner__user=request.user) | Q(local_profile=request.user)
    )
    conn = get_object_or_404(GradeAPIConnection.objects.filter(journal__in=journals), pk=conn_id)

    with transaction.atomic():
        if delete_all:
            matching = [item for item in GradeAPIConnection.objects.filter(
                source=conn.source, url=conn.url, journal__in=journals
            ) if item.api_key == conn.api_key]
            count = len(matching)
            name = conn.name or conn.get_source_display()
            for item in matching:
                AuditLog.objects.create(
                    school=item.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                    target_model='GradeAPIConnection', target_id=str(item.pk),
                    description=f'Групове видалення ключа API {name} ({item.url}).'
                )
                safely_delete_grade_api_connection(item)
            messages.success(request, f'Ключ «{name}» успішно видалено з усіх {count} журналів. Виставлені оцінки збережено.')
        else:
            name = conn.name or conn.get_source_display()
            class_name = conn.journal.student_class.name
            AuditLog.objects.create(
                school=conn.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                target_model='GradeAPIConnection', target_id=str(conn.pk),
                description=f'Видалено підключення API {name} ({conn.url}) для журналу {class_name}.'
            )
            safely_delete_grade_api_connection(conn)
            messages.success(request, f'Підключення «{name}» видалено з журналу {class_name}. Виставлені оцінки збережено.')

    return redirect('portal:teacher_api_keys')


@login_required
@require_POST
def quick_group_connections(request, journal_id):
    """
    Швидке об'єднання підключень одного джерела в логічну групу сервера (для роботи з різними IP).
    """
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    group_name = request.POST.get('group_name', '').strip()
    source = request.POST.get('source', '').strip()
    conn_ids = request.POST.getlist('connection_ids')
    active_id = request.POST.get('active_connection_id')
    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})

    if not group_name:
        messages.error(request, 'Вкажіть назву групи сервера (наприклад: Сервер тестування).')
        return redirect(next_url)

    connections = journal.grade_apis.filter(pk__in=conn_ids) if conn_ids else (
        journal.grade_apis.filter(source=source) if source else journal.grade_apis.none()
    )

    if not connections.exists():
        messages.error(request, 'Не вибрано жодного підключення для об’єднання в групу.')
        return redirect(next_url)

    with transaction.atomic():
        _lock(journal, request.user)
        for c in connections:
            c.server_group = group_name
            if active_id and str(c.pk) == str(active_id):
                c.is_default_for_group = True
            c.save(update_fields=['server_group', 'is_default_for_group'])
        AuditLog.objects.create(
            school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
            target_model='GradeAPIConnection', target_id=str(journal.pk),
            description=f"Об'єднано {connections.count()} підключень у групу «{group_name}»."
        )

    messages.success(request, f"Підключення ({connections.count()} шт.) успішно об'єднано в групу «{group_name}». Тепер журнал автоматично обиратиме активну адресу в мережі!")
    return redirect(next_url)


@login_required
@require_POST
def set_active_connection(request, journal_id, connection_id):
    """
    Встановлює пріоритетне підключення для групи серверів в один клік.
    """
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    connection = get_object_or_404(journal.grade_apis, pk=connection_id)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json'

    with transaction.atomic():
        _lock(journal, request.user)
        if connection.server_group:
            journal.grade_apis.filter(server_group=connection.server_group).update(is_default_for_group=False)
        connection.is_default_for_group = True
        connection.enabled = True
        connection.save(update_fields=['is_default_for_group', 'enabled'])

    msg = f"«{connection.name}» ({connection.host_display}) встановлено як активну адресу."
    if is_ajax:
        return JsonResponse({'success': True, 'message': msg, 'connection_id': connection.pk})

    messages.success(request, msg)
    next_url = request.POST.get('next') or reverse('portal:journal_integrations', kwargs={'journal_id': journal.pk})
    return redirect(next_url)


