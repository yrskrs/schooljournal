"""Journal-only controls: classes, periods and placement; credentials live centrally."""
import datetime as dt
from django.utils import timezone
from django.urls import reverse
from django.db.models import Q
from .class_matching import binding_status
from .connection_setup import group_connections_by_server


def source_panels(journal, user, filtered_range=None):
    from apps.portal.forms_connections import JournalSyncSettingsForm
    bindings = list(journal.grade_apis.select_related('service_connection__last_endpoint__profile').order_by('pk'))
    groups = group_connections_by_server(bindings)
    panels = []
    today = timezone.localdate()
    for source, label, old_label in [('schooltest', 'SchoolTest · Тести', 'Синхронізувати з тестами'),
            ('schoolwork', 'SchoolNet · Завдання', 'Синхронізувати із завданнями')]:
        rows = []
        for group in groups:
            if group['source'] != source: continue
            candidates = group['connections']
            binding = next((c for c in candidates if c.enabled and c.is_default_for_group), None) or next(
                (c for c in candidates if c.enabled), candidates[0])
            end = max(journal.academic_year.start_date, min(today, journal.academic_year.end_date))
            start = max(journal.academic_year.start_date, end - dt.timedelta(days=max(1, binding.default_period_days) - 1))
            if filtered_range: start, end = filtered_range
            logs = binding.pull_logs.order_by('-created_at', '-pk')
            grade_log = logs.filter(Q(counts__operation__isnull=True) | Q(counts__operation='grades')).first()
            counts = grade_log.counts if grade_log else {}
            address = ''
            service = binding.service_connection
            if service and service.last_endpoint:
                from .network_connections import current_journal_host
                try: address = service.last_endpoint.site_url(current_journal_host.get())
                except Exception: pass
            row = binding_status(binding)
            row.update(date_from=start.isoformat(), date_to=end.isoformat(),
                counts=counts, active_address=address,
                settings_form=JournalSyncSettingsForm(instance=binding, prefix=f'connection-{binding.pk}'),
                settings_url=reverse('portal:journal_sync_settings', args=[journal.pk, binding.pk]),
                connected=bool(binding.source_class_id and binding.source_subject_id),
                available=bool(binding.enabled and (not service or service.enabled)),
                can_send='journal_roster_import' in binding.roster_cache.get('capabilities', []))
            rows.append(row)
        panels.append({'source': source, 'label': label, 'old_label': old_label, 'connections': rows})
    return panels
