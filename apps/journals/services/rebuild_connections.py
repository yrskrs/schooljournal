"""Replace credentials and bindings without cascading through imported history."""
import copy
from collections import defaultdict

from django.db import transaction

from apps.journals.models import GradeAPIConnection


@transaction.atomic
def recreate_bindings(owner, services):
    """One new working binding per context; retain duplicate history read-only.

    services contains new, already owner-scoped connections. All dependent
    record IDs remain intact, including manual review, extras and audit logs.
    Historical copies remain disabled in the same logical group so their
    unique source IDs and teacher decisions never need destructive merging.
    """
    old = list(GradeAPIConnection.objects.select_for_update().filter(
        service_connection__owner=owner).select_related('journal').order_by('pk'))
    if any(c.source not in services or services[c.source].owner_id != owner.pk for c in old):
        raise ValueError('Потрібні нові підключення того самого власника до кожного джерела.')
    groups, replacements = defaultdict(list), {}
    for previous in old:
        service = services[previous.source]
        values = {f.attname: copy.deepcopy(getattr(previous, f.attname))
                  for f in GradeAPIConnection._meta.concrete_fields
                  if not f.primary_key and f.name != 'created_at'}
        values.update(service_connection=service, api_key=service.token,
                      service_connection_id=service.pk, server_group=service.name,
                      name=service.name, is_default_for_group=False, enabled=False)
        # Assign the FK once via its storage attribute.
        values.pop('service_connection')
        current = GradeAPIConnection.objects.create(**values)
        for relation in GradeAPIConnection._meta.related_objects:
            field = relation.field
            if not relation.one_to_many:
                raise ValueError('Непідтримувана залежність підключення; зміни скасовано.')
            relation.related_model.objects.filter(**{field.attname: previous.pk}).update(**{field.attname: current.pk})
        replacements[previous.pk] = current.pk
        key = (previous.journal_id, previous.source, previous.source_class_id, previous.source_subject_id)
        groups[key].append((previous, current))
    active = []
    for pairs in groups.values():
        eligible = [(old, new) for old, new in pairs if old.enabled]
        candidates = eligible or pairs
        old, canonical = max(candidates, key=lambda pair: (
            bool(pair[0].is_default_for_group),
            pair[0].roster_sent_at or pair[0].created_at, pair[0].pk))
        canonical.enabled = bool(eligible)
        canonical.is_default_for_group = True
        canonical.save(update_fields=['enabled', 'is_default_for_group'])
        if canonical.enabled:
            active.append(canonical.pk)
    GradeAPIConnection.objects.filter(pk__in=replacements).delete()
    return {'replacements': replacements, 'active': active, 'logical_contexts': len(groups)}
