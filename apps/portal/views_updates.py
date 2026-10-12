from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST, require_GET

from apps.accounts.models import User
from apps.portal.services.updates import (
    get_current_local_version,
    check_updates_available,
    get_update_status,
    start_update,
    start_rollback,
)


def _user_can_manage_updates(user):
    return user.is_authenticated and (
        user.is_superuser or user.is_school_admin_user or user.role in (User.Role.SUPERADMIN, User.Role.SCHOOL_ADMIN)
    )


@login_required
def system_updates_view(request):
    current_version = get_current_local_version()
    status_info = get_update_status()
    can_update = _user_can_manage_updates(request.user)

    context = {
        'current_version': current_version,
        'status_info': status_info,
        'can_update': can_update,
    }
    return render(request, 'portal/admin/updates.html', context)


@login_required
def check_updates_api_view(request):
    result = check_updates_available()
    return JsonResponse(result)


@login_required
@require_POST
def start_update_api_view(request):
    if not _user_can_manage_updates(request.user):
        return HttpResponseForbidden('Тільки адміністратор може запускати оновлення системи.')

    target_ref = request.POST.get('ref', 'origin/main')
    target_ver = request.POST.get('version')
    result = start_update(target_ref=target_ref, target_ver=target_ver)
    return JsonResponse(result)


@login_required
@require_GET
def update_status_api_view(request):
    status_info = get_update_status()
    return JsonResponse(status_info)


@login_required
@require_POST
def rollback_api_view(request):
    if not _user_can_manage_updates(request.user):
        return HttpResponseForbidden('Тільки адміністратор може запускати відновлення системи.')

    backup_file = request.POST.get('backup_file')
    result = start_rollback(backup_file=backup_file)
    return JsonResponse(result)
