"""Read-only, allowlisted diagnostics suitable for sharing without pupil data."""
import re
from uuid import UUID, uuid5

from django.utils import timezone
from apps.portal.site_info import release_info

from apps.curriculum.models import ClassEnrollment, Student, StudentClass
from apps.journals.models import GradePullLog, RosterState
from .assessment import journal_students
from .roster_transfer import confirmed_roster_ids


COUNT_FIELDS = {'added', 'applied', 'updated', 'pending', 'conflicts', 'unchanged',
                'ignored', 'errors', 'created', 'needs_review'}
ERROR_CODES = {'roster_http_error', 'roster_validation_error',
               'roster_transport_or_response_error'}


def safe_log(log):
    """Never serialize error messages, rows, names, URLs, keys or raw payloads."""
    counts = log.counts if isinstance(log.counts, dict) else {}
    errors = []
    for error in log.errors if isinstance(log.errors, list) else []:
        error = error if isinstance(error, dict) else {'message': error}
        code = error.get('code')
        if code not in ERROR_CODES:
            text = str(error.get('message', ''))
            if counts.get('operation') == 'roster':
                code = 'roster_http_error' if re.search(r'HTTP\s+[1-5]\d\d\b', text) else 'roster_validation_error'
            elif 'цілим' in text or 'шкали' in text:
                code = 'grade_value_invalid'
            else:
                code = 'grade_import_error'
        status = error.get('http_status')
        if type(status) is not int or not 100 <= status <= 599:
            match = re.search(r'HTTP\s+([1-5]\d\d)\b', str(error.get('message', '')))
            status = int(match[1]) if match else None
        errors.append({'code': code, 'http_status': status})
    return {'time': log.created_at.isoformat(),
            'operation': 'roster' if counts.get('operation') == 'roster' else 'grades',
            'counts': {key: value for key, value in counts.items()
                       if key in COUNT_FIELDS and type(value) is int and value >= 0},
            'errors': errors}


def journal_diagnostics(journal, reference):
    students = list(journal_students(journal))
    ignored = set(journal.ignored_students.values_list('pk', flat=True))
    active = set(ClassEnrollment.objects.filter(student_class=journal.student_class,
        is_active=True, student__status=Student.Status.ACTIVE).values_list('student_id', flat=True))
    data = {'reference': reference, 'archived': journal.is_archived,
            'students': len(students), 'active_students': len(active),
            'ignored_students': len(ignored),
            'invalid_student_names': sum(not s.first_name.strip() or not s.last_name.strip() for s in students),
            'connections': []}
    state = RosterState.objects.filter(pk=1).values_list('data', flat=True).first() or {}
    try:
        namespace = UUID(state.get('origin', ''))
    except (ValueError, TypeError, AttributeError):
        namespace = None
    for index, connection in enumerate(journal.grade_apis.order_by('pk'), 1):
        cache = connection.roster_cache if isinstance(connection.roster_cache, dict) else {}
        mappings = list(connection.student_mappings.all())
        numeric = {}
        for mapping in mappings:
            identity = mapping.external_id
            if identity.isascii() and identity.isdecimal() and int(identity) > 0:
                numeric.setdefault(mapping.student_id, set()).add(identity)
        confirmed = confirmed_roster_ids(connection)
        restored = ambiguous = 0
        for student in students:
            ref = str(uuid5(namespace, f'student:{student.pk}')) if namespace else ''
            identities = numeric.get(student.pk, set())
            restored += int(ref in confirmed and confirmed[ref] not in identities)
            ambiguous += int(ref not in confirmed and len(identities) > 1)
        issues = []
        if restored:
            issues.append({'code': 'roster_identity_preserved_from_confirmation', 'count': restored})
        if ambiguous:
            issues.append({'code': 'roster_identity_needs_review', 'count': ambiguous})
        if connection.source_class_id and not any(str(r.get('id')) == connection.source_class_id for r in cache.get('classes', []) if isinstance(r, dict)):
            issues.append({'code': 'selected_class_missing_from_cache'})
        if connection.source_subject_id and not any(str(r.get('id')) == connection.source_subject_id for r in cache.get('subjects', []) if isinstance(r, dict)):
            issues.append({'code': 'selected_subject_missing_from_cache'})
        data['connections'].append({
            'reference': f'{reference}-A{index}',
            'source': connection.source if connection.source in ('schooltest', 'schoolwork') else 'generic',
            'enabled': connection.enabled, 'contract_version': connection.contract_version,
            'roster_supported': 'journal_roster_import' in cache.get('capabilities', []),
            'has_confirmed_roster': bool(confirmed), 'mapping_count': len(mappings),
            'result_alias_count': sum(not (m.external_id.isascii() and m.external_id.isdecimal() and int(m.external_id) > 0) for m in mappings),
            'issues': issues,
            'recent_calls': [safe_log(log) for log in GradePullLog.objects.filter(connection=connection).order_by('-created_at', '-pk')[:25]],
        })
    return data


def build_diagnostic_report(journals, *, all_records=False):
    journals = list(journals)
    report = {'schema_version': 1, 'app_version': release_info()[0],
              'diagnostics_revision': 'sync-repair-2026-10-18', 'generated_at': timezone.now().isoformat(),
              'privacy': 'No names, grades, student IDs, credentials, hostnames or raw responses.',
              'scope': 'all_local_records' if all_records else 'authorized_journals',
              'network_requests': 0,
              'code_guide': {
                  'roster_identity_preserved_from_confirmation': 'ID списку відновлено з підтвердженого звіту; псевдоніми оцінок збережено.',
                  'roster_identity_needs_review': 'Кілька числових ID без підтвердження: потрібна перевірка відповідностей.',
                  'roster_http_error': 'Джерело відхилило передачу: звірте HTTP-статус, клас, ID та права ключа.',
                  'roster_validation_error': 'Некоректний список або відповідь; перевірте локальні відповідності.',
                  'roster_transport_or_response_error': 'Не вдалося підтвердити відповідь джерела.',
                  'grade_value_invalid': 'Значення не відповідає шкалі; автоматично не змінюється.',
                  'grade_import_error': 'Помилка отримання або імпорту оцінки. Деталі доступні власнику в журналі.',
                  'selected_class_missing_from_cache': 'Обраного класу немає у кеші джерела.',
                  'selected_subject_missing_from_cache': 'Обраного предмета немає у кеші джерела.',
              },
              'journals': [journal_diagnostics(journal, f'J{index}') for index, journal in enumerate(journals, 1)]}
    report['totals'] = {'journals': len(journals), 'connections': sum(len(j['connections']) for j in report['journals'])}
    if all_records:
        # Administrative command only; the web route never broadens its scope.
        students = Student.objects.all()
        enrollments = ClassEnrollment.objects.select_related('student', 'student_class')
        report['classes'] = []
        for index, group in enumerate(StudentClass.objects.select_related('academic_year').order_by('pk'), 1):
            members = list(ClassEnrollment.objects.filter(student_class=group).select_related('student'))
            report['classes'].append({'reference': f'C{index}',
                'service_class': group.letter == 'ПК' and group.name.endswith(' (Позакласні)'),
                'students': len(members), 'active_enrollments': sum(m.is_active for m in members),
                'invalid_student_names': sum(not m.student.first_name.strip() or not m.student.last_name.strip() for m in members),
                'valid_class_fields': bool(group.name.strip()) and 1 <= group.grade_level <= 12,
                'school_matches_academic_year': group.school_id == group.academic_year.school_id})
        report['totals'].update(classes=StudentClass.objects.count(), students=students.count(),
            enrollments=enrollments.count(),
            invalid_student_names=sum(not s.first_name.strip() or not s.last_name.strip() for s in students),
            enrollment_school_mismatches=sum(e.student.school_id != e.student_class.school_id for e in enrollments))
    return report
