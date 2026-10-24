from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.http import HttpResponseBadRequest
from django.views.decorators.http import require_GET

from apps.portal.api_workspace import build_overview, request_overview
from apps.portal.permissions import get_user_journals, has_journal_access
from apps.portal.views_journal import _get_journal_or_403


def render_workspace(request, journal=None, notifications=False):
    journals = get_user_journals(request.user).filter(is_archived=False).select_related('nz_owner__user').order_by(
        '-academic_year__start_date', 'student_class__name', 'subject__name')
    journals = [j for j in journals if has_journal_access(request.user, j)]
    selected = request.GET.get('journal')
    if journal is None and selected:
        if not selected.isdigit():
            return HttpResponseBadRequest('Оберіть журнал зі списку.')
        journal = _get_journal_or_403(request.user, selected)
    if request.GET.get('action') == 'connect' and journal:
        _get_journal_or_403(request.user, journal.pk, write=True)
        return redirect(reverse('portal:connections_matching') + f'?journal={journal.pk}')
    overview = build_overview([journal.pk]) if journal else request_overview(request)
    tab = 'notifications' if notifications else request.GET.get('tab', 'connections')
    if tab not in ('connections', 'notifications', 'roster'):
        tab = 'connections'
    base = reverse('portal:journal_integrations', args=[journal.pk]) if journal else reverse('portal:api_workspace')
    response = render(request, 'portal/api/workspace.html', {
        'journal': journal, 'workspace_journals': journals, 'api_groups': overview['groups'],
        'api_notices': overview['notices'], 'workspace_tab': tab, 'workspace_url': base,
        'can_edit': not journal or has_journal_access(request.user, journal, write=True),
        'new_api_key': '',
    })
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_GET
def api_workspace(request):
    if request.GET.get('tab', 'connections') == 'connections' and not request.GET.get('journal'):
        from .views_connections import dashboard
        return dashboard(request)
    return render_workspace(request)


@login_required
@require_GET
def api_notifications(request):
    return render_workspace(request, notifications=True)
