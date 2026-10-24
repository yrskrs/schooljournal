"""Idempotent conversion of legacy addresses; no HTTP and no pupil mutations."""
import hashlib
from urllib.parse import urlsplit
from uuid import UUID


def migrate_legacy_connections(apps, schema_editor=None):
    Binding = apps.get_model('journals', 'GradeAPIConnection')
    Service = apps.get_model('journals', 'ServiceConnection')
    Profile = apps.get_model('journals', 'NetworkProfile')
    Endpoint = apps.get_model('journals', 'ServiceEndpoint')
    Selection = apps.get_model('journals', 'NetworkSelection')
    for binding in Binding.objects.select_related('journal__nz_owner').order_by('pk').iterator():
        # Re-save through EncryptedTokenField, including generic/legacy API v1.
        if binding.api_key:
            Binding.objects.filter(pk=binding.pk).update(api_key=binding.api_key)
        if binding.service_connection_id or binding.contract_version != 2 or binding.source not in ('schooltest', 'schoolwork'):
            continue
        journal = binding.journal
        owner_id = (journal.nz_owner.user_id if journal.nz_owner_id else None) or journal.local_profile_id or journal.teacher_id
        if not owner_id:
            continue
        parsed = urlsplit(binding.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            continue  # Keep unusual legacy configuration intact and visible.
        suffix = '/api/v2/journal/grades'
        path = parsed.path.rstrip('/')
        if not path.endswith(suffix) or parsed.query or parsed.fragment:
            continue
        cache = binding.roster_cache if isinstance(binding.roster_cache, dict) else {}
        try:
            instance = UUID(cache.get('instance_id', ''))
        except (ValueError, TypeError, AttributeError):
            instance = None
        fingerprint = hashlib.sha256(binding.api_key.encode()).hexdigest()
        legacy_group = binding.server_group.strip() or binding.url
        identity = (f'instance:{instance}:{fingerprint}' if instance else
            'legacy:' + hashlib.sha256((legacy_group + ':' + fingerprint).encode()).hexdigest())
        service, _ = Service.objects.get_or_create(owner_id=owner_id, source=binding.source, identity_key=identity,
            defaults={'name': binding.server_group or binding.name or binding.get_source_display(), 'instance_id': instance,
                'token': binding.api_key, 'token_fingerprint': fingerprint, 'roster_cache': cache,
                'last_checked_at': binding.roster_checked_at, 'enabled': binding.enabled})
        if binding.enabled and not service.enabled:
            service.enabled = True
            service.save(update_fields=['enabled'])
        profile = Profile.objects.filter(owner_id=owner_id, host=parsed.hostname, scheme=parsed.scheme,
            use_journal_host=False).first()
        if profile is None:
            label = binding.location_tag.strip() or f'Мережа {parsed.hostname}'
            label = label[:100]
            if Profile.objects.filter(owner_id=owner_id, name=label).exists():
                label = f'{label[:70]} ({parsed.hostname})'[:100]
            counter, base_label = 2, label
            while Profile.objects.filter(owner_id=owner_id, name=label).exists():
                label = f'{base_label[:90]} · {counter}'
                counter += 1
            profile = Profile.objects.create(owner_id=owner_id, name=label, host=parsed.hostname, scheme=parsed.scheme)
        endpoint, _ = Endpoint.objects.get_or_create(connection_id=service.pk, profile_id=profile.pk,
            port_override=parsed.port or (443 if parsed.scheme == 'https' else 80), path_prefix=path[:-len(suffix)],
            defaults={'enabled': binding.enabled})
        if binding.enabled and not endpoint.enabled:
            endpoint.enabled = True
            endpoint.save(update_fields=['enabled'])
        selection, _ = Selection.objects.get_or_create(owner_id=owner_id)
        if binding.is_default_for_group or not selection.last_profile_id:
            selection.last_profile_id = profile.pk
            selection.save(update_fields=['last_profile'])
        if binding.is_default_for_group or not service.last_endpoint_id:
            service.last_endpoint_id = endpoint.pk
            service.save(update_fields=['last_endpoint'])
        Binding.objects.filter(pk=binding.pk).update(service_connection_id=service.pk)


def restore_plaintext_legacy_tokens(apps, schema_editor):
    """Allow an explicit schema rollback to the old plaintext field implementation."""
    Binding = apps.get_model('journals', 'GradeAPIConnection')
    table = schema_editor.quote_name(Binding._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        for binding in Binding.objects.only('pk', 'api_key').iterator():
            cursor.execute(f'UPDATE {table} SET api_key = %s WHERE id = %s', [binding.api_key, binding.pk])
