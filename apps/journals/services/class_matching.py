"""Read-only matrix. A name is a hint, never a class identity."""
from difflib import SequenceMatcher
from uuid import UUID, uuid5
from .connection_setup import selected_students


def binding_status(binding):
    cache = binding.roster_cache if isinstance(binding.roster_cache, dict) else {}
    classes = {str(row['id']): row['name'] for row in cache.get('classes', [])}
    known = set(binding.student_mappings.values_list('external_id', flat=True))
    missing = sum(row['id'] not in known for row in selected_students(binding))
    linked = bool(binding.source_class_id and binding.source_subject_id)
    review = not linked or binding.source_class_id not in classes or bool(missing) or (
        binding.source_subject_id not in {str(row['id']) for row in cache.get('subjects', [])})
    return {'binding': binding, 'class_name': classes.get(binding.source_class_id, 'Клас ще не обрано'),
        'class_id': binding.source_class_id, 'mapped': len(known), 'missing': missing,
        'status': 'Потребує перевірки' if review else 'Зв’язано', 'review': review}


def matrix_rows(journals, services, origin):
    groups = {}
    for journal in journals:
        # StudentClass is already scoped to an academic year. Never group by text.
        group = groups.setdefault(journal.student_class_id, {'student_class': journal.student_class,
            'year': journal.academic_year, 'uuid': str(uuid5(UUID(origin), f'class:{journal.student_class_id}')),
            'journals': []})
        row = {'journal': journal, 'cells': []}
        for source in ('schooltest', 'schoolwork'):
            bindings = list(journal.grade_apis.filter(source=source, contract_version=2).select_related(
                'service_connection').order_by('-is_default_for_group', 'pk'))
            # Multiple former network addresses keep their IDs, but one cell per service.
            seen, statuses = set(), []
            for binding in bindings:
                identity = binding.service_connection_id or ('legacy', binding.pk)
                if identity in seen: continue
                seen.add(identity); statuses.append(binding_status(binding))
            choices = []
            for service in services:
                if service.source != source: continue
                cache = service.roster_cache if isinstance(service.roster_cache, dict) else {}
                classes = [{**c, 'hint': SequenceMatcher(None, journal.student_class.name.casefold(), c['name'].casefold()).ratio() >= .68}
                    for c in cache.get('classes', [])]
                choices.append({'service': service, 'classes': classes, 'subjects': cache.get('subjects', [])})
            row['cells'].append({'source': source, 'bindings': statuses, 'choices': choices})
        group['journals'].append(row)
    return list(groups.values())
