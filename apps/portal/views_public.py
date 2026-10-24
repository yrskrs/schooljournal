from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .site_info import release_info


@require_GET
@never_cache
def service_ping(request):
    from apps.journals.models import RosterState
    from school_sync.django_backend import operate
    from school_sync.discovery import ping_payload
    state = RosterState.objects.filter(pk=1).only('data').first()
    identity = state.data.get('origin') if state and isinstance(state.data, dict) else None
    identity = identity or operate(lambda engine, store: engine.origin)
    return JsonResponse(ping_payload('schooljournal', identity, release_info()[0]))


@require_GET
def guide(request):
    return render(request, 'portal/public/guide.html')


@require_GET
def versions(request):
    return render(request, 'portal/public/versions.html', {'releases': release_info()[1]})


@require_GET
@never_cache
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok', 'version': release_info()[0]})
