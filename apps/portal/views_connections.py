"""One owner-scoped place for credentials and network configuration."""
import hashlib
import copy
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.journals.models import (GradeAPIConnection, NetworkProfile, NetworkSelection,
    ServiceConnection, ServiceEndpoint)
from apps.journals.services.connection_setup import fetch_source_roster
from apps.journals.services.external_grades import ExternalAPIError
from apps.journals.services.network_connections import (check_service, inspect_address,
    resolve_endpoint, remember_endpoint, current_journal_host, ConnectionFailure)
from apps.journals.services.network_profiles import ensure_automatic_profiles, unique_name
from .forms_connections import ServiceSetupForm, NetworkProfileForm, ServiceEditForm, EndpointForm
from .permissions import get_user_journals, has_journal_access
from .views_journal import _get_journal_or_403

SOURCE_LABELS = {'schooltest': 'SchoolTest · Тести', 'schoolwork': 'SchoolNet · Завдання'}


def _owned_service(request, pk):
    if not str(pk).isdecimal(): raise Http404('Підключення не знайдено.')
    return get_object_or_404(ServiceConnection.objects.select_related('last_endpoint__profile'),
        owner=request.user, pk=pk)


def _time(value):
    return timezone.localtime(value).strftime('%d.%m.%Y %H:%M') if value else 'Ще не виконувалася'


def service_card(service, selection=None):
    endpoint = service.last_endpoint
    if selection and selection.selected_profile_id:
        endpoint = service.endpoints.filter(profile_id=selection.selected_profile_id,
            profile__owner_id=service.owner_id).select_related('profile').first()
    active_url = ''
    if endpoint and endpoint.profile.owner_id == service.owner_id:
        try: active_url = endpoint.site_url(current_journal_host.get())
        except ValidationError: pass
    same_mode = not selection or not selection.selected_profile_id or bool(
        endpoint and endpoint.profile_id == selection.selected_profile_id)
    ready = bool(service.enabled and service.last_checked_at and same_mode and endpoint and endpoint.enabled and endpoint.profile.enabled
        and endpoint.last_status == 'online' and service.token_status == 'accepted' and not service.last_error)
    times = service.journal_bindings.aggregate(roster=Max('roster_sent_at'), grades=Max('last_pulled_at'))
    return {'id': service.pk, 'name': service.name, 'source': service.source,
        'source_label': SOURCE_LABELS[service.source], 'enabled': service.enabled,
        'ready': ready, 'status_label': 'Працює' if ready else 'Вимкнено' if not service.enabled else
            'Потребує уваги' if service.last_error else 'Потрібна перевірка',
        'version': service.version or 'Ще не перевірена',
        'instance_short': str(service.instance_id)[:8] if service.instance_id else 'Ще не підтверджений',
        'instance_id': str(service.instance_id) if service.instance_id else '',
        'token_label': 'Ключ прийнято' if service.token_status == 'accepted' else
            'Ключ не прийнято' if service.token_status == 'unauthorized' else 'Ключ потребує перевірки',
        'can_export': service.permissions.get('export'), 'can_import': service.permissions.get('roster_import'),
        'last_checked': _time(service.last_checked_at), 'last_roster': _time(times['roster']),
        'last_grades': _time(times['grades']), 'error': service.last_error,
        'active_url': active_url, 'profile_name': endpoint.profile.name if endpoint else '',
        'journal_count': service.journal_bindings.values('journal_id').distinct().count(),
        'check_url': reverse('portal:connection_check', args=[service.pk]),
        'edit_url': reverse('portal:connection_edit', args=[service.pk]),
        'matching_url': reverse('portal:connections_matching') + f'?service={service.pk}'}


@login_required
@never_cache
@require_GET
def dashboard(request):
    ensure_automatic_profiles(request.user)
    selection, _ = NetworkSelection.objects.get_or_create(owner=request.user)
    services = list(ServiceConnection.objects.filter(owner=request.user).select_related('last_endpoint__profile'))
    cards = [service_card(service, selection) for service in services]
    from .api_workspace import request_overview
    overview = request_overview(request)
    legacy_groups = [group for group in overview['groups'] if not group['primary'].service_connection_id]
    for group in legacy_groups:
        group['setup_url'] = reverse('portal:connection_legacy_edit', args=[group['primary'].pk])
    response = render(request, 'portal/api/connections.html', {
        'cards': cards, 'profiles': request.user.network_profiles.all(), 'selection': selection,
        'missing_sources': [{'source': source, 'label': label} for source, label in SOURCE_LABELS.items()
            if not any(service.source == source for service in services)],
        'legacy_groups': legacy_groups, 'api_groups': overview['groups'], 'api_notices': overview['notices'],
        'workspace_tab': 'connections',
    })
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@never_cache
@require_POST
def choose_network(request):
    value = request.POST.get('profile', '')
    profile = get_object_or_404(NetworkProfile, owner=request.user, pk=value, enabled=True) if value.isdecimal() else None
    if value and not value.isdecimal():
        raise PermissionDenied('Оберіть свою мережу або автовизначення.')
    selection, _ = NetworkSelection.objects.get_or_create(owner=request.user)
    selection.selected_profile = profile
    selection.save(update_fields=['selected_profile'])
    ServiceConnection.objects.filter(owner=request.user).update(last_checked_at=None, last_error='')
    messages.success(request, f'Мережа: {profile.name if profile else "Автовизначення"}. Ключі та прив’язки збережено.')
    return redirect('portal:connections')


@login_required
@never_cache
@require_POST
def connection_check(request, service_id):
    service = _owned_service(request, service_id)
    error = ''
    try: check_service(service)
    except ConnectionFailure as failure: error = str(failure)
    service.refresh_from_db()
    selection = NetworkSelection.objects.filter(owner=request.user).first()
    response = JsonResponse({'ok': not bool(error), 'message': error or 'Сайт і ключ підтверджено.',
        'service': service_card(service, selection)})
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@never_cache
@sensitive_post_parameters('api_key', 'address')
@require_http_methods(['GET', 'POST'])
def connect_service(request):
    ensure_automatic_profiles(request.user)
    initial = {'source': request.GET.get('source', 'schooltest')}
    form = ServiceSetupForm(request.POST if request.method == 'POST' else None,
        owner=request.user, journal_host=current_journal_host.get(), initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            info = inspect_address(data['source'], data['site_url'], data['api_key'])
            fingerprint = hashlib.sha256(data['api_key'].encode()).hexdigest()
            parsed = data['parsed_address']
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            prefix = parsed.path.rstrip('/')[:-len('/api/v2/journal/grades')]
            with transaction.atomic():
                request.user.__class__.objects.select_for_update().get(pk=request.user.pk)
                service = ServiceConnection.objects.filter(owner=request.user, source=data['source'],
                    instance_id=info['instance_id'], token_fingerprint=fingerprint).first()
                if service is None:
                    service = ServiceConnection(owner=request.user, source=data['source'],
                        name=data['name'] or SOURCE_LABELS[data['source']], instance_id=info['instance_id'],
                        identity_key=f'instance:{info["instance_id"]}:{fingerprint}')
                    service.set_token(data['api_key'])
                service.version, service.permissions = info['version'], info['permissions']
                service.token_status, service.enabled = 'accepted', True
                service.save()
                profile = data.get('profile') or NetworkProfile.objects.filter(owner=request.user,
                    host=parsed.hostname, scheme=parsed.scheme, enabled=True, use_journal_host=False).first()
                if profile is None:
                    profile = NetworkProfile(owner=request.user,
                        name=unique_name(request.user, data['profile_name'] or f'Мережа {parsed.hostname}'),
                        host=parsed.hostname, scheme=parsed.scheme)
                    from django.conf import settings
                    profile.schooltest_port, profile.schoolwork_port = settings.LOCAL_SCHOOLTEST_PORT, settings.LOCAL_SCHOOLWORK_PORT
                    setattr(profile, data['source'] + '_port', port)
                    profile.full_clean(); profile.save()
                endpoint = next((e for e in service.endpoints.filter(profile=profile, path_prefix=prefix)
                    if (e.port_override or getattr(profile, service.source + '_port')) == port), None)
                if endpoint is None:
                    endpoint = ServiceEndpoint.objects.create(connection=service, profile=profile,
                        port_override=port, path_prefix=prefix)
                endpoint.enabled, endpoint.last_status = True, 'online'
                endpoint.last_checked_at = timezone.now()
                endpoint.save(update_fields=['enabled', 'last_status', 'last_checked_at'])
                remember_endpoint(service, endpoint, info)
                ensure_automatic_profiles(request.user)
            messages.success(request, 'Сервіс підключено. Тепер зіставте класи — повторно вводити адресу й ключ не потрібно.')
            return redirect(reverse('portal:connections') + f'#connection-{service.pk}')
        except (ConnectionFailure, ValidationError) as error:
            form.add_error(None, str(error) if isinstance(error, ConnectionFailure) else 'Перевірте адресу й назву мережі. Збережені підключення не змінено.')
    response = render(request, 'portal/api/connect_service.html', {'form': form})
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def profile_edit(request, profile_id=None):
    profile = get_object_or_404(NetworkProfile, owner=request.user, pk=profile_id) if profile_id else None
    kwargs = {'instance': profile} if profile else {}
    form = NetworkProfileForm(request.POST if request.method == 'POST' else None, owner=request.user, **kwargs)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            request.user.__class__.objects.select_for_update().get(pk=request.user.pk)
            profile = form.save()
            profile.endpoints.update(last_status='unknown', last_error='')
            for service in ServiceConnection.objects.filter(owner=request.user):
                if not service.endpoints.filter(profile=profile).exists():
                    template = service.endpoints.exclude(profile__use_journal_host=True).exclude(
                        profile__host='host.docker.internal').first()
                    ServiceEndpoint.objects.create(connection=service, profile=profile,
                        port_override=template.port_override if template else None,
                        path_prefix=template.path_prefix if template else '')
        messages.success(request, 'Мережу збережено. Ключі й відповідності учнів залишилися чинними.')
        return redirect('portal:connections')
    return render(request, 'portal/api/connection_form.html', {'form': form, 'form_kind': 'profile',
        'heading': 'Редагувати мережу' if profile else 'Додати мережу'})


@login_required
@never_cache
@sensitive_post_parameters('api_key')
@require_http_methods(['GET', 'POST'])
def connection_edit(request, service_id):
    service = _owned_service(request, service_id)
    form = ServiceEditForm(request.POST if request.method == 'POST' else None,
        initial={'name': service.name, 'enabled': service.enabled})
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        info, cache = None, None
        try:
            if data['api_key']:
                # Validate the new key before replacing the working encrypted key.
                checking = copy.copy(service)
                checking.enabled = True
                endpoint, _ = resolve_endpoint(checking)
                info = inspect_address(service.source, endpoint.site_url(current_journal_host.get()),
                    data['api_key'], service.instance_id)
                cache = fetch_source_roster(endpoint.api_url('grades', current_journal_host.get()), data['api_key'], service.source)
                classes, subjects = {r['id'] for r in cache['classes']}, {r['id'] for r in cache['subjects']}
                if any((b.source_class_id and b.source_class_id not in classes) or
                        (b.source_subject_id and b.source_subject_id not in subjects) for b in service.journal_bindings.all()):
                    raise ConnectionFailure('Новий ключ не має доступу до пов’язаних класів або предметів. Створіть ключ того самого вчителя. Попередній ключ збережено.', 'forbidden')
                fingerprint = hashlib.sha256(data['api_key'].encode()).hexdigest()
                if ServiceConnection.objects.filter(owner=request.user, source=service.source,
                        instance_id=service.instance_id, token_fingerprint=fingerprint).exclude(pk=service.pk).exists():
                    raise ConnectionFailure('Цей ключ уже використовується в іншому вашому підключенні. Оберіть його на сторінці зіставлення; поточні прив’язки збережено.', 'duplicate')
            with transaction.atomic():
                fresh = ServiceConnection.objects.select_for_update().get(pk=service.pk, owner=request.user)
                if fresh.token_fingerprint != service.token_fingerprint:
                    raise ConnectionFailure('Підключення вже змінилося. Оновіть сторінку й повторіть дію.', 'stale')
                fresh.name, fresh.enabled = data['name'], data['enabled']
                if info:
                    fresh.set_token(data['api_key'])
                    fresh.identity_key = f'instance:{fresh.instance_id}:{fresh.token_fingerprint}'
                    fresh.permissions, fresh.version, fresh.token_status = info['permissions'], info['version'], 'accepted'
                    fresh.roster_cache = cache
                fresh.save()
                if info:
                    fresh.journal_bindings.update(api_key=data['api_key'])
            messages.success(request, 'Підключення збережено. Класи, відповідності та оцінки залишилися на місці.')
            return redirect(reverse('portal:connections') + f'#connection-{service.pk}')
        except ExternalAPIError as error: form.add_error('api_key', str(error))
    return render(request, 'portal/api/connection_form.html', {'form': form, 'form_kind': 'service',
        'heading': 'Редагувати підключення', 'service': service,
        'endpoints': service.endpoints.filter(profile__owner=request.user).select_related('profile')})


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def endpoint_edit(request, service_id, endpoint_id):
    service = _owned_service(request, service_id)
    endpoint = get_object_or_404(service.endpoints, pk=endpoint_id, profile__owner=request.user)
    form = EndpointForm(request.POST if request.method == 'POST' else None, service=service, instance=endpoint)
    if request.method == 'POST' and form.is_valid():
        endpoint = form.save(commit=False)
        endpoint.last_status, endpoint.last_error = 'unknown', ''
        endpoint.save()
        messages.success(request, 'Адресу збережено. Перед обміном сайт знову підтвердить свою ідентичність.')
        return redirect('portal:connection_edit', service_id=service.pk)
    return render(request, 'portal/api/connection_form.html', {'form': form, 'form_kind': 'endpoint',
        'heading': 'Додаткові параметри адреси', 'service': service})


@login_required
@never_cache
@require_GET
def matching(request):
    from apps.journals.services.extra_students import extra_parent
    journals = [j for j in get_user_journals(request.user).filter(is_archived=False).select_related(
        'student_class', 'subject', 'academic_year', 'nz_owner').order_by('-academic_year__start_date',
        'student_class__name', 'subject__name') if has_journal_access(request.user, j, write=True) and not extra_parent(j)]
    if request.GET.get('journal'):
        selected = _get_journal_or_403(request.user, request.GET['journal'], write=True) if request.GET['journal'].isdecimal() else None
        journals = [j for j in journals if selected and j.student_class_id == selected.student_class_id]
    services = ServiceConnection.objects.filter(owner=request.user, enabled=True)
    if request.GET.get('service'):
        selected_service = _owned_service(request, request.GET['service'])
        services = services.filter(pk=selected_service.pk)
    from school_sync.django_backend import operate
    from apps.journals.models import RosterState
    state = RosterState.objects.filter(pk=1).only('data').first()
    origin = state.data.get('origin') if state and isinstance(state.data, dict) else None
    origin = origin or operate(lambda engine, store: engine.origin)
    from apps.journals.services.class_matching import matrix_rows
    return render(request, 'portal/api/matching.html', {'matrix': matrix_rows(journals, list(services), origin),
        'services': services})


@login_required
@never_cache
@require_POST
def refresh_service_roster(request, service_id):
    service = _owned_service(request, service_id)
    try:
        endpoint, _ = resolve_endpoint(service)
        payload = fetch_source_roster(endpoint.api_url('grades', current_journal_host.get()), service.token, service.source)
        if payload.get('instance_id') != str(service.instance_id):
            raise ConnectionFailure('За цією адресою інший сервіс. Збережені класи залишилися чинними.', 'identity_mismatch')
        with transaction.atomic():
            fresh = ServiceConnection.objects.select_for_update().get(pk=service.pk, owner=request.user)
            if fresh.token_fingerprint != service.token_fingerprint: raise ConnectionFailure('Ключ змінився. Повторіть дію.', 'stale')
            fresh.roster_cache = payload; fresh.save(update_fields=['roster_cache'])
        messages.success(request, 'Назви класів і предметів оновлено. Учнів та оцінки не змінено.')
    except ExternalAPIError as error: messages.error(request, str(error))
    return redirect('portal:connections_matching')


@login_required
@never_cache
@sensitive_post_parameters('api_key', 'connection_code')
@require_http_methods(['GET', 'POST'])
def legacy_edit(request, connection_id):
    binding = get_object_or_404(GradeAPIConnection.objects.filter(journal__in=get_user_journals(request.user)), pk=connection_id)
    if binding.service_connection_id:
        return redirect('portal:connection_edit', service_id=binding.service_connection_id)
    from .views_connection_setup import connect_source
    if request.method == 'POST':
        data = request.POST.copy(); data['connection_id'] = str(binding.pk); request.POST = data
    else:
        data = request.GET.copy(); data['connection'] = str(binding.pk); request.GET = data
    return connect_source(request, binding.journal_id, allow_legacy_address_form=True)


@login_required
@never_cache
@require_POST
def bind_service(request, journal_id):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    service = _owned_service(request, request.POST.get('service'))
    owner_id = (journal.nz_owner.user_id if journal.nz_owner_id else None) or journal.local_profile_id or journal.teacher_id
    if owner_id != service.owner_id: raise PermissionDenied('Налаштування підключення змінює власник журналу.')
    try:
        endpoint, ping = resolve_endpoint(service)
        url = endpoint.api_url('grades', current_journal_host.get())
        payload = fetch_source_roster(url, service.token, service.source)
        if payload.get('instance_id') != str(service.instance_id):
            raise ConnectionFailure('Ідентичність джерела змінилася. Журнал і відповідності збережено.', 'identity_mismatch')
        class_id, subject_id = request.POST.get('source_class_id', ''), request.POST.get('source_subject_id', '')
        create_class = request.POST.get('action') == 'create_class'
        if class_id and class_id not in {r['id'] for r in payload['classes']}:
            raise ConnectionFailure('Клас більше не доступний. Оновіть назви та оберіть його повторно.', 'stale')
        if subject_id and subject_id not in {r['id'] for r in payload['subjects']}:
            raise ConnectionFailure('Предмет більше не доступний. Оновіть назви.', 'stale')
        if create_class:
            class_id, subject_id = '', ''
            if 'journal_roster_import' not in payload.get('capabilities', []):
                raise ConnectionFailure('Це джерело ще не підтримує створення класу з журналу.', 'unsupported')
        with transaction.atomic():
            from .views_connection_setup import _lock
            journal = _lock(journal, request.user)
            fresh = ServiceConnection.objects.select_for_update().get(pk=service.pk, owner=request.user)
            if fresh.token_fingerprint != service.token_fingerprint: raise ConnectionFailure('Ключ змінився. Повторіть дію.', 'stale')
            binding = journal.grade_apis.filter(service_connection=service).order_by('-is_default_for_group', 'pk').first()
            if binding is None:
                binding = GradeAPIConnection.objects.create(journal=journal, source=service.source,
                    contract_version=2, service_connection=service, name=service.name, url=url,
                    api_key=service.token, roster_cache=payload, roster_checked_at=timezone.now())
            else:
                binding.roster_cache, binding.roster_checked_at = payload, timezone.now()
                binding.save(update_fields=['roster_cache', 'roster_checked_at'])
            if create_class and binding.source_class_id:
                raise ConnectionFailure('Клас уже пов’язаний. Передайте учнів через чинну прив’язку; копія класу не потрібна.', 'already_bound')
            if class_id or subject_id:
                if not class_id or not subject_id:
                    raise ConnectionFailure('Оберіть і клас, і предмет. Для порожнього джерела скористайтеся створенням класу.', 'context')
                if (binding.source_class_id, binding.source_subject_id) != (class_id, subject_id) and (
                        binding.records.exists() or binding.student_mappings.exists()):
                    raise ConnectionFailure('Для чинної прив’язки вже є учні або історія. Перевірте її окремо — перепризначення не виконано.', 'history')
                binding.source_class_id, binding.source_subject_id = class_id, subject_id
                binding.save(update_fields=['source_class_id', 'source_subject_id'])
            fresh.roster_cache = payload; fresh.save(update_fields=['roster_cache'])
        if create_class:
            from apps.journals.services.connection_setup import exchange_with_fallback
            from apps.journals.services.roster_transfer import send_journal_roster
            _, report = exchange_with_fallback([binding], lambda c: send_journal_roster(c, request.user))
            messages.success(request, f"Клас створено й пов’язано за ID. Нових учнів: {report['counts']['created']}; потребують перевірки: {report['counts']['needs_review']}.")
        return redirect(reverse('portal:connect_source', args=[journal.pk]) + f'?connection={binding.pk}#roster')
    except ExternalAPIError as error:
        messages.error(request, str(error))
        return redirect('portal:connections_matching')
