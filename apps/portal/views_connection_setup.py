from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import Journal, GradeAPIConnection, GradeStudentMapping, AuditLog, StudentAPIGrant
from apps.journals.services.assessment import journal_students
from apps.journals.services.connection_setup import fetch_source_roster, roster_version, selected_students, SOURCES, ensure_same_source
from apps.journals.services.external_grades import ExternalAPIError
from apps.journals.services.roster_transfer import send_journal_roster
from apps.journals.services.api_inbox import keep_roster_extras
from apps.portal.forms_connection_setup import ConnectSourceForm, SourceContextForm, ConfirmStudentsForm
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
            return ''.join(c for c in value.casefold().translate(str.maketrans('abcehiktmx', 'авсенік тмх'.replace(' ', ''))) if c.isalnum())
        matches = [row['id'] for row in rows if normalized(row['name']) == normalized(name)]
        values[field] = current if any(row['id'] == current for row in rows) else matches[0] if len(matches) == 1 else ''
    return values


@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('api_key', 'connection_code')
def connect_source(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    destination = reverse('portal:connect_source', kwargs={'journal_id': journal.pk})
    connection_id = request.POST.get('connection_id') if request.method == 'POST' else request.GET.get('connection')
    connection = get_object_or_404(journal.grade_apis, pk=connection_id, contract_version=2) if str(connection_id or '').isdigit() else None
    if connection_id and connection is None:
        raise PermissionDenied('Некоректне підключення.')
    setup = ConnectSourceForm(initial={'source': request.GET.get('source', 'schooltest')})
    if connection:
        setup = ConnectSourceForm(initial={'source': connection.source, 'site_url': connection.url.rsplit('/api/v2/journal/grades/', 1)[0]})
    context = mapping = None
    available = []
    known = set()
    for item in GradeAPIConnection.objects.filter(contract_version=2).filter(
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
                    setup = ConnectSourceForm(request.POST)
                if setup.is_valid():
                    data = setup.cleaned_data
                    if not conn_name:
                        conn_name = data.get('key_name') or SOURCES[data['source']]
                    payload = fetch_source_roster(data['grades_url'], data['api_key'], data['source'])
                    with transaction.atomic():
                        journal = _lock(journal, request.user)
                        if connection:
                            connection = journal.grade_apis.select_for_update().get(pk=connection.pk)
                            if connection.source != data['source']:
                                raise ExternalAPIError('Це підключення належить іншому джерелу. Поверніться до журналу та підключіть нове джерело окремо.')
                        else:
                            connection = journal.grade_apis.filter(source=data['source'], contract_version=2, url=data['grades_url']).order_by('pk').first()
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
                        if 'journal_roster_import' in payload.get('capabilities', []):
                            initial = _initial_context(connection)
                            if not connection.source_class_id:
                                connection.source_class_id = initial['source_class_id']
                            if not connection.source_subject_id:
                                connection.source_subject_id = initial['source_subject_id']
                        connection.save()
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='GradeAPIConnection', target_id=str(connection.pk), description='Підключено джерело та отримано назви класів, предметів і учнів. Оцінки не імпортувалися.')

                    apply_to_all = request.POST.get('apply_to_all_classes') == '1'
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
                            if not has_journal_access(request.user, other_j, write=True):
                                continue
                            try:
                                with transaction.atomic():
                                    other_conn = other_j.grade_apis.filter(
                                        source=data['source'], contract_version=2, url=data['grades_url']
                                    ).order_by('pk').first()
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
                payload = fetch_source_roster(connection.url, connection.api_key, connection.source)
                with transaction.atomic():
                    _lock(journal, request.user)
                    connection = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
                    ensure_same_source(connection, payload)
                    connection.roster_cache = payload
                    connection.roster_checked_at = timezone.now()
                    connection.save(update_fields=['roster_cache', 'roster_checked_at'])
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
            elif action == 'students' and connection and connection.source_class_id and connection.source_subject_id:
                mapping = ConfirmStudentsForm(request.POST, connection=connection)
                if mapping.is_valid():
                    with transaction.atomic():
                        journal = _lock(journal, request.user)
                        connection = GradeAPIConnection.objects.select_for_update().get(pk=connection.pk)
                        if mapping.cleaned_data['version'] != roster_version(connection):
                            raise ExternalAPIError('Списки оновилися. Оновіть сторінку перед підтвердженням.')
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
                            source=item.source, url=item.url, api_key=item.api_key,
                            journal__school=journal.school
                        ).filter(
                            Q(journal__teacher=request.user) | Q(journal__nz_owner__user=request.user) | Q(journal__local_profile=request.user)
                        )
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
    response = render(request, 'portal/journal/connect_source.html', {'journal': journal, 'connection': connection,
        'setup_form': setup, 'context_form': context, 'mapping_form': mapping,
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
    with transaction.atomic():
        _lock(journal, request.user)
        conn_name = connection.name or connection.get_source_display()
        url = connection.url
        AuditLog.objects.create(
            school=journal.school, user=request.user, action=AuditLog.Action.DELETE,
            target_model='GradeAPIConnection', target_id=str(connection.pk),
            description=f'Видалено підключення API {conn_name} ({url}) для журналу {journal.student_class.name}.'
        )
        connection.delete()
    messages.success(request, f'Підключення «{conn_name}» видалено.')
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('portal:connect_source', journal_id=journal.pk)


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

    journals = get_user_journals(request.user).filter(
        Q(teacher=request.user) | Q(nz_owner__user=request.user) | Q(local_profile=request.user)
    ).select_related('student_class', 'subject', 'academic_year', 'school').order_by('academic_year', 'student_class__name', 'subject__name')

    all_conns = GradeAPIConnection.objects.filter(journal__in=journals).select_related(
        'journal', 'journal__student_class', 'journal__subject'
    ).order_by('source', 'name', 'pk')

    # Групуємо унікальні ключі за (source, url, api_key)
    key_groups = {}
    for conn in all_conns:
        ident = (conn.source, conn.url, conn.api_key)
        if ident not in key_groups:
            key_groups[ident] = {
                'id': conn.pk,
                'source': conn.source,
                'source_display': conn.get_source_display(),
                'name': conn.name or conn.get_source_display(),
                'url': conn.url,
                'api_key': conn.api_key,
                'masked_key': conn.masked_key,
                'capabilities_description': conn.capabilities_description,
                'can_send': 'journal_roster_import' in (conn.roster_cache.get('capabilities', []) if isinstance(conn.roster_cache, dict) else []),
                'connections': [],
                'journals_count': 0,
                'last_checked': conn.roster_checked_at,
                'last_pulled': conn.last_pulled_at,
            }
        grp = key_groups[ident]
        grp['connections'].append(conn)
        grp['journals_count'] += 1
        if conn.roster_checked_at and (not grp['last_checked'] or conn.roster_checked_at > grp['last_checked']):
            grp['last_checked'] = conn.roster_checked_at
        if conn.last_pulled_at and (not grp['last_pulled'] or conn.last_pulled_at > grp['last_pulled']):
            grp['last_pulled'] = conn.last_pulled_at

    student_grants = StudentAPIGrant.objects.filter(journal__in=journals).select_related(
        'journal', 'journal__student_class', 'journal__subject'
    ).order_by('journal__student_class__name')

    return render(request, 'portal/teacher/api_keys.html', {
        'key_groups': list(key_groups.values()),
        'all_connections': all_conns,
        'student_grants': student_grants,
        'journals': journals,
        'total_connections_count': all_conns.count(),
        'unique_keys_count': len(key_groups),
    })


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
            matching = GradeAPIConnection.objects.filter(
                source=conn.source, url=conn.url, api_key=conn.api_key, journal__in=journals
            )
            count = matching.count()
            name = conn.name or conn.get_source_display()
            for item in matching:
                AuditLog.objects.create(
                    school=item.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                    target_model='GradeAPIConnection', target_id=str(item.pk),
                    description=f'Групове видалення ключа API {name} ({item.url}).'
                )
            matching.delete()
            messages.success(request, f'Ключ «{name}» успішно видалено з усіх {count} журналів.')
        else:
            name = conn.name or conn.get_source_display()
            class_name = conn.journal.student_class.name
            AuditLog.objects.create(
                school=conn.journal.school, user=request.user, action=AuditLog.Action.DELETE,
                target_model='GradeAPIConnection', target_id=str(conn.pk),
                description=f'Видалено підключення API {name} ({conn.url}) для журналу {class_name}.'
            )
            conn.delete()
            messages.success(request, f'Підключення «{name}» видалено з журналу {class_name}.')

    return redirect('portal:teacher_api_keys')

