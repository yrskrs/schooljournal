import datetime as dt
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import requests
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from apps.journals.models import Journal, Grade, Attendance, ExternalGrade, GradePullLog, AuditLog, Lesson, LessonType
from apps.journals.services.assessment import journal_students
from apps.journals.services.scheduling import SUMMARY_TYPES


class ExternalAPIError(Exception):
    pass


def json_field(value, path, default=None):
    if not path:
        return value
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def validate_api_url(url, resolve=False):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addresses = []
        try:
            addresses = [ipaddress.ip_address(parsed.hostname)]
        except ValueError:
            if parsed.hostname.lower() in ('localhost', 'localhost.localdomain'):
                raise ValidationError('Вкажіть адресу сервера робіт у мережі, а не localhost журналу.')
            if resolve:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)]
        addresses = [address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address for address in addresses]
        if any(address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast for address in addresses):
            raise ValidationError('Цю службову адресу не можна використовувати для API робіт.')
    except (ValueError, OSError) as error:
        raise ValidationError('Некоректна або недоступна адреса API.') from error


def fetch_grade_payload(connection, date_from, date_to):
    if connection.contract_version == 2:
        from .school_api import fetch_v2_grades
        return fetch_v2_grades(connection, date_from, date_to)
    if not connection.enabled:
        raise ExternalAPIError('Підключення вимкнене.')
    try:
        validate_api_url(connection.url, resolve=True)
        with requests.Session() as session:
            session.trust_env = False
            headers = {'Accept': 'application/json'}
            if connection.api_key:
                headers['Authorization'] = 'Bearer ' + connection.api_key
            with session.get(connection.url, headers=headers, params={
                'journal_id': connection.journal_id, 'nz_journal_id': connection.journal.nz_journal_id,
                'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
            }, timeout=(5, 20), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ExternalAPIError(f'API повернув HTTP {response.status_code}. Перевірте адресу й ключ доступу.')
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise ExternalAPIError('Відповідь завелика. Оберіть коротший період (до 2 МБ).')
                payload = json.loads(content)
    except ValidationError as error:
        raise ExternalAPIError(' '.join(error.messages)) from error
    except (requests.RequestException, ValueError) as error:
        # Never render request URLs, authorization headers or upstream HTML.
        raise ExternalAPIError('Не вдалося отримати коректну JSON відповідь від API робіт.') from error
    records = json_field(payload, connection.records_key)
    if not isinstance(records, list) or len(records) > 1000:
        raise ExternalAPIError('Очікується список до 1000 оцінок. Перевірте назву поля списку JSON.')
    return records


def _boolean(row, key, default):
    value = json_field(row, key, default)
    if not isinstance(value, bool):
        raise ValueError(f'Поле {key} має бути true або false.')
    return value


def _extract_session_id(row):
    raw_id = str(row.get('id') or '')
    if raw_id.startswith('schooltest:session:'):
        parts = raw_id.split(':')
        if len(parts) >= 3 and parts[2].isdigit():
            return f"schooltest:session:{parts[2]}"
    elif raw_id.startswith('schoolwork:assignment:'):
        parts = raw_id.split(':')
        if len(parts) >= 3 and parts[2].isdigit():
            return f"schoolwork:assignment:{parts[2]}"
    return None


def _resolve_target_lesson(target_journal, journal, connection, row, date, number, user,
                           session_id, work_id, batch_session_lessons, batch_work_lessons):
    lessons = target_journal.lessons.filter(date=date).exclude(lesson_type__in=SUMMARY_TYPES)
    if number is not None:
        if isinstance(number, bool) or not str(number).isdigit() or int(number) < 1:
            raise ValueError('Некоректний номер уроку на дату.')
        lessons = lessons.filter(lesson_number=int(number))

    session_key = (target_journal.pk, session_id)
    work_key = (target_journal.pk, work_id)
    # A known session wins over a different lesson on the completion date.
    # Every cache entry belongs to a journal, including separate pupil journals.
    if session_id:
        prev = ExternalGrade.objects.filter(
            connection=connection,
            lesson__journal=target_journal,
            external_id__startswith=f'{session_id}:'
        ).select_related('lesson').first()
        if prev and prev.lesson:
            batch_session_lessons[session_key] = prev.lesson
            return prev.lesson
        if session_key in batch_session_lessons:
            return batch_session_lessons[session_key]

    # Tests can be reused in independent sessions. Their shared work_id must
    # never override an explicit session identity.
    if work_id and not session_id:
        prev = ExternalGrade.objects.filter(
            connection=connection,
            lesson__journal=target_journal,
            source_payload__work_id=row.get('work_id')
        ).select_related('lesson').first()
        if prev and prev.lesson:
            batch_work_lessons[work_key] = prev.lesson
            return prev.lesson
        if work_key in batch_work_lessons:
            return batch_work_lessons[work_key]

    main_session_lesson = None
    if target_journal != journal and session_id:
        previous = ExternalGrade.objects.filter(connection=connection,
            lesson__journal=journal, external_id__startswith=f'{session_id}:').select_related('lesson').first()
        main_session_lesson = previous.lesson if previous else batch_session_lessons.get((journal.pk, session_id))

    if lessons.count() == 1 and not main_session_lesson:
        lesson = lessons.first()
        if session_id:
            batch_session_lessons[session_key] = lesson
        elif work_id:
            batch_work_lessons[work_key] = lesson
        return lesson

    # Copy the main session's lesson into the separate journal; never return
    # the main journal's lesson for a separate pupil.
    if target_journal != journal:
        main_lesson = main_session_lesson or journal.lessons.filter(date=date).first()
        if main_lesson:
            date = main_lesson.date
        l_num = main_lesson.lesson_number if main_session_lesson else (int(number) if number is not None else (main_lesson.lesson_number if main_lesson else 1))
        topic = main_lesson.topic if main_lesson else (row.get('title') if connection.contract_version == 2 else json_field(row, connection.title_key, 'Позакласна робота'))
        homework = main_lesson.homework if main_lesson else ''
        l_type = main_lesson.lesson_type if main_lesson else LessonType.ORDINARY
        sem = main_lesson.semester if main_lesson else None
        lesson, _ = Lesson.objects.get_or_create(
            journal=target_journal,
            date=date,
            lesson_number=l_num,
            defaults={
                'topic': topic or 'Позакласна робота',
                'homework': homework,
                'lesson_type': l_type,
                'semester': sem,
                'created_by': user,
            }
        )
        if session_id:
            batch_session_lessons[session_key] = lesson
        elif work_id:
            batch_work_lessons[work_key] = lesson
        return lesson

    # 7. Check if test title matches a lesson topic in target journal
    raw_title = row.get('title') if isinstance(row, dict) else None
    title = (str(raw_title) if raw_title is not None else '').strip()
    if title and len(title) >= 3:
        for l in target_journal.lessons.exclude(lesson_type__in=SUMMARY_TYPES).order_by('-date'):
            l_top = (l.topic or '').strip()
            if l_top and len(l_top) >= 3 and (l_top.lower() in title.lower() or title.lower() in l_top.lower()):
                if session_id:
                    batch_session_lessons[session_key] = l
                elif work_id:
                    batch_work_lessons[work_key] = l
                return l

    raise ValueError('Не знайдено однозначний урок на дату. Створіть урок або надайте lesson_number.')


@transaction.atomic
def import_grade_records(connection, rows, user, date_from, date_to):
    journal = Journal.objects.select_for_update().get(pk=connection.journal_id)
    from apps.portal.permissions import has_journal_access
    if not has_journal_access(user, journal, write=True):
        raise ExternalAPIError('Доступ до журналу змінився. Оновіть сторінку.')
    from .grade_placement import assignment_map, peer_connections, work_key, validate_lesson
    placement_peers = peer_connections(connection)
    assigned_lessons = assignment_map(connection, peers=placement_peers)
    ignored_student_ids = set(journal.ignored_students.values_list('pk', flat=True))
    students = {}
    for student in journal_students(journal):
        if student.pk in ignored_student_ids:
            continue
        identifier = str(getattr(student, connection.student_identifier) or '')
        if identifier:
            students.setdefault(identifier, []).append(student)
    counts = {'applied': 0, 'added': 0, 'updated': 0, 'pending': 0, 'conflicts': 0, 'unchanged': 0, 'ignored': 0, 'errors': 0}
    errors = []
    seen = set()
    if not isinstance(rows, list) or len(rows) > (50000 if connection.contract_version == 2 else 1000):
        raise ExternalAPIError('Завелика або некоректна відповідь API.')
    batch_session_lessons = {}
    batch_work_lessons = {}
    # Pre-scan rows to detect lessons for sessions and works present in this batch
    for r in (rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        if connection.contract_version == 2 and (
                r.get('source') != connection.source
                or str(r.get('class_id')) != connection.source_class_id
                or str(r.get('subject_id')) != connection.source_subject_id
                or r.get('status') != 'final' or r.get('approved_by_teacher') is not True):
            continue
        r_date_text = r.get('date') if connection.contract_version == 2 else json_field(r, connection.date_key)
        if not isinstance(r_date_text, str) or len(r_date_text) != 10:
            continue
        try:
            r_date = dt.date.fromisoformat(r_date_text)
        except ValueError:
            continue
        if not (date_from <= r_date <= date_to and journal.academic_year.start_date <= r_date <= journal.academic_year.end_date):
            continue
        r_num = r.get('lesson_number') if connection.contract_version == 2 else json_field(r, connection.lesson_key)
        if r_num is not None and (isinstance(r_num, bool) or not str(r_num).isdigit() or int(r_num) < 1):
            continue
        matching_ls = journal.lessons.filter(date=r_date).exclude(lesson_type__in=SUMMARY_TYPES)
        if r_num is not None and str(r_num).isdigit():
            matching_ls = matching_ls.filter(lesson_number=int(r_num))
        if matching_ls.count() == 1:
            matched_l = matching_ls.first()
            s_id = _extract_session_id(r)
            s_key = (journal.pk, s_id)
            if s_id and (s_key not in batch_session_lessons or matched_l.date < batch_session_lessons[s_key].date):
                batch_session_lessons[s_key] = matched_l
            w_id = str(r.get('work_id')) if r.get('work_id') is not None else (str(json_field(r, 'work_id')) if json_field(r, 'work_id') is not None else None)
            w_key = (journal.pk, w_id)
            if w_id and not s_id and w_key not in batch_work_lessons:
                batch_work_lessons[w_key] = matched_l

    for index, row in enumerate(rows, 1):
        inbox = None
        target_journal = journal
        try:
            if not isinstance(row, dict):
                raise ValueError('Очікується об’єкт оцінки.')
            source_payload = {}
            if connection.contract_version == 2:
                from .api_inbox import keep_result, resolve_student
                inbox = keep_result(connection, row, date_from, date_to)
                if inbox is None or inbox.is_ignored:
                    counts['ignored'] += 1
                    continue
                if (inbox.extra_student and inbox.extra_student.is_separate
                        and not inbox.extra_student.matched_student_id and not inbox.extra_student.separate_student_id):
                    inbox.resolved = True
                    inbox.reason = 'Збережено окремо. Підтвердьте відповідність учня за ID.'
                    inbox.save(update_fields=['resolved', 'reason'])
                    counts['added' if inbox.payload_created else 'updated' if inbox.payload_changed else 'unchanged'] += 1
                    counts['outside'] = counts.get('outside', 0) + 1
                    continue
                if inbox.extra_student and inbox.extra_student.separate_journal_id:
                    target_journal = Journal.objects.select_for_update().get(pk=inbox.extra_student.separate_journal_id)
                    if not has_journal_access(user, target_journal, write=True):
                        raise ValueError('Немає права редагувати окремий журнал позакласного учня.')
                    if target_journal.ignored_students.filter(pk=inbox.extra_student.separate_student_id).exists():
                        inbox.resolved, inbox.reason = True, 'Учня ігноровано в окремому журналі (сімейна форма).'
                        inbox.save(update_fields=['resolved', 'reason'])
                        counts['ignored'] += 1
                        continue
                elif inbox.extra_student and inbox.extra_student.matched_student_id:
                    if not journal_students(journal).filter(pk=inbox.extra_student.matched_student_id).exists():
                        from apps.curriculum.models import ClassEnrollment
                        other_enr = ClassEnrollment.objects.filter(student_id=inbox.extra_student.matched_student_id, is_active=True).first()
                        if other_enr:
                            other_j = Journal.objects.filter(
                                school=journal.school,
                                academic_year=journal.academic_year,
                                student_class=other_enr.student_class,
                                subject=journal.subject,
                                teacher=journal.teacher,
                            ).first()
                            if other_j and has_journal_access(user, other_j, write=True):
                                target_journal = other_j
                if ignored_student_ids:
                    try:
                        mapped_student_id = resolve_student(connection, row)
                    except ValueError:
                        mapped_student_id = None
                    if mapped_student_id in ignored_student_ids:
                        inbox.resolved = True
                        inbox.reason = 'Учня ігноровано в цьому журналі (сімейна форма).'
                        inbox.save(update_fields=['resolved', 'reason'])
                        counts['ignored'] += 1
                        continue
                from .school_api import normalize_v2_grade
                row, source_payload = normalize_v2_grade(connection, row)
            raw_id = row.get('id') if connection.contract_version == 2 else json_field(row, connection.id_key)
            if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool) or not str(raw_id).strip() or len(str(raw_id)) > 200:
                raise ValueError('Потрібен стабільний ID оцінки / роботи.')
            external_id = str(raw_id)
            if external_id in seen:
                raise ValueError('Повторний ID оцінки в одній відповіді API.')
            seen.add(external_id)
            if connection.contract_version == 2:
                if row.get('student_id') in ignored_student_ids:
                    inbox.resolved = True
                    inbox.reason = 'Учня ігноровано в цьому журналі (сімейна форма).'
                    inbox.save(update_fields=['resolved', 'reason'])
                    counts['ignored'] += 1
                    continue
                candidates = [student for student in journal_students(target_journal)
                    if student.pk == row['student_id']]
                if not candidates and inbox.extra_student and inbox.extra_student.matched_student_id == row.get('student_id'):
                    candidates = [inbox.extra_student.matched_student]
            else:
                raw_student_id = str(json_field(row, connection.student_key))
                candidates = students.get(raw_student_id, [])
                if not candidates:
                    # Check if student was intentionally ignored
                    ignored_matches = [s for s in journal_students(journal)
                        if s.pk in ignored_student_ids and str(getattr(s, connection.student_identifier) or '') == raw_student_id]
                    if ignored_matches:
                        counts['ignored'] += 1
                        continue
            if len(candidates) != 1:
                raise ValueError('ID учня відсутній у журналі або не є унікальним.')
            student = candidates[0]
            try:
                date_text = row.get('date') if connection.contract_version == 2 else json_field(row, connection.date_key)
                if not isinstance(date_text, str) or len(date_text) != 10:
                    raise ValueError
                date = dt.date.fromisoformat(date_text)
            except ValueError:
                raise ValueError('Дата має бути у форматі YYYY-MM-DD.') from None
            number = row.get('lesson_number') if connection.contract_version == 2 else json_field(row, connection.lesson_key)
            session_id = _extract_session_id(row)
            work_id = str(row.get('work_id')) if row.get('work_id') is not None else (str(json_field(row, 'work_id')) if json_field(row, 'work_id') is not None else None)
            selected_key = work_key(connection, source_payload or row)
            selected_lesson = assigned_lessons.get(selected_key)
            if selected_key in assigned_lessons and selected_lesson is None:
                raise ValueError('Збережено різні уроки для цієї роботи. Відкрийте «Обрати урок» і підтвердьте потрібний.')
            previous = ExternalGrade.objects.filter(connection__in=placement_peers, external_id=external_id).select_related('lesson').order_by('pk').first() if selected_lesson else None
            if selected_lesson:
                validate_lesson(journal, selected_lesson)
            if selected_lesson and previous:
                # Teacher choices place new results; existing grade history is
                # never moved, cleared or duplicated by changing the selection.
                if previous.student_id != student.pk:
                    raise ValueError('Цей результат уже належить іншому учню. Історію збережено; перевірте відповідності.')
                lesson = previous.lesson
            elif selected_lesson and target_journal.pk == journal.pk:
                lesson = selected_lesson
            elif selected_lesson:
                lesson, _ = Lesson.objects.get_or_create(journal=target_journal,
                    date=selected_lesson.date, lesson_number=selected_lesson.lesson_number,
                    defaults={'topic': selected_lesson.topic, 'homework': selected_lesson.homework,
                        'lesson_type': selected_lesson.lesson_type, 'semester': selected_lesson.semester,
                        'created_by': user})
            else:
                lesson = _resolve_target_lesson(
                    target_journal=target_journal,
                    journal=journal,
                    connection=connection,
                    row=row,
                    date=date,
                    number=number,
                    user=user,
                    session_id=session_id,
                    work_id=work_id,
                    batch_session_lessons=batch_session_lessons,
                    batch_work_lessons=batch_work_lessons,
                )
            if not (date_from <= date <= date_to or date_from <= lesson.date <= date_to):
                raise ValueError('Дата оцінки поза вибраним періодом.')
            if not (journal.academic_year.start_date <= lesson.date <= journal.academic_year.end_date):
                raise ValueError('Дата уроку поза навчальним роком цього журналу.')
            raw_value = row.get('value') if connection.contract_version == 2 else json_field(row, connection.value_key)
            if isinstance(raw_value, bool) or not str(raw_value).isdigit() or not 1 <= int(raw_value) <= 12:
                raise ValueError('Оцінка має бути цілим числом від 1 до 12.')
            value = str(int(raw_value))
            is_ai = _boolean(row, 'is_ai' if connection.contract_version == 2 else connection.ai_key, True)
            externally_approved = _boolean(row, 'approved_by_teacher' if connection.contract_version == 2 else connection.approved_key, False)
            fingerprint = hashlib.sha256(json.dumps([student.pk, lesson.pk, value, is_ai, externally_approved]).encode()).hexdigest()
            record = ExternalGrade.objects.filter(connection=connection, external_id=external_id).first()
            if record and source_payload:
                from .school_api import check_revision
                check_revision(record.source_payload, source_payload)
            grade = Grade.objects.filter(lesson=lesson, student=student).first()
            attendance = Attendance.objects.filter(lesson=lesson, student=student).first()
            # A teacher may already have entered this mark manually, or another
            # connection to the same server may have imported it. Keep ownership
            # and NZ sync flags intact; an identical approved cell needs no vote.
            if (grade and grade.value == value and grade.is_approved
                    and (not attendance or attendance.status == Attendance.Status.PRESENT)
                    and not (record and record.status == ExternalGrade.Status.REJECTED)
                    and (not record or (record.lesson_id, record.student_id) == (lesson.pk, student.pk))):
                ExternalGrade.objects.update_or_create(connection=connection, external_id=external_id,
                    defaults={'student': student, 'lesson': lesson, 'value': value, 'is_ai': is_ai,
                        'externally_approved': externally_approved, 'status': ExternalGrade.Status.APPLIED,
                        'fingerprint': fingerprint, 'source_payload': source_payload})
                counts['unchanged'] += 1
                if inbox:
                    inbox.resolved, inbox.reason = True, ''
                    inbox.save(update_fields=['resolved', 'reason'])
                continue
            if record and record.fingerprint == fingerprint:
                if source_payload:
                    record.source_payload = source_payload
                    record.save(update_fields=['source_payload', 'updated_at'])
                counts['unchanged'] += 1
                if inbox:
                    inbox.resolved, inbox.reason = True, ''
                    inbox.save(update_fields=['resolved', 'reason'])
                continue
            previous_value = record.value if record else None
            previous_cell = (record.lesson_id, record.student_id) if record else None
            if record and previous_cell != (lesson.pk, student.pk):
                raise ValueError('API змінив учня або урок для існуючого ID. Надайте новий ID оцінки.')
            can_apply = not grade or (not grade.value and grade.is_synced_to_nz) or (
                record and grade.import_record_id == record.pk and grade.value == previous_value and (
                    not grade.is_approved or (connection.contract_version == 2 and record.reviewed_by_id is None
                        and record.status == ExternalGrade.Status.APPLIED)))
            if attendance and attendance.status != Attendance.Status.PRESENT:
                can_apply = False
            pending = is_ai and not externally_approved
            status = ExternalGrade.Status.PENDING if pending else ExternalGrade.Status.APPLIED
            if not can_apply:
                status = ExternalGrade.Status.CONFLICT
            record, _ = ExternalGrade.objects.update_or_create(connection=connection, external_id=external_id,
                defaults={'student': student, 'lesson': lesson, 'value': value, 'is_ai': is_ai,
                    'externally_approved': externally_approved, 'status': status, 'fingerprint': fingerprint,
                    'reviewed_by': None, 'reviewed_at': None, 'source_payload': source_payload})
            if can_apply:
                if not grade:
                    grade = Grade(lesson=lesson, student=student)
                grade.value = value
                grade.is_approved = not pending
                grade.import_record = record
                grade.created_by = user
                grade.is_synced_to_nz = False
                grade.save()
                counts['added' if previous_value is None else 'updated'] += 1
            counts['conflicts' if not can_apply else 'pending' if pending else 'applied'] += 1
            if inbox:
                inbox.resolved, inbox.reason = True, ''
                inbox.save(update_fields=['resolved', 'reason'])
        except (ValueError, TypeError, OverflowError) as error:
            counts['errors'] += 1
            errors.append({'row': index, 'message': str(error)})
            if inbox:
                inbox.resolved, inbox.reason = False, str(error)[:500]
                inbox.save(update_fields=['resolved', 'reason'])
    connection.last_pulled_at = timezone.now()
    connection.save(update_fields=['last_pulled_at'])
    log = GradePullLog.objects.create(connection=connection, user=user, counts=counts, errors=errors)
    AuditLog.objects.create(school=journal.school, user=user, action=AuditLog.Action.IMPORT,
        target_model='GradePullLog', target_id=str(log.pk), description='Отримано оцінки із зовнішнього API.', changes=counts)
    return log


@transaction.atomic
def review_external_grade(record_id, journal, user, action, version, replace=False):
    from apps.portal.permissions import has_journal_access
    journal = Journal.objects.select_for_update().get(pk=journal.pk)
    if not has_journal_access(user, journal, write=True):
        raise ExternalAPIError('Немає права редагувати цей журнал.')
    record = ExternalGrade.objects.select_for_update().get(pk=record_id, connection__journal=journal)
    if not has_journal_access(user, record.lesson.journal, write=True):
        raise ExternalAPIError('Немає права редагувати журнал цієї оцінки.')
    if record.updated_at.isoformat() != version:
        raise ExternalAPIError('Оцінка змінилася після перегляду. Оновіть сторінку.')
    if action not in ('approve', 'reject') or record.status not in (ExternalGrade.Status.PENDING, ExternalGrade.Status.CONFLICT):
        raise ExternalAPIError('Оцінку вже розглянуто або дія некоректна.')
    grade = Grade.objects.filter(lesson=record.lesson, student=record.student).first()
    attendance = Attendance.objects.filter(lesson=record.lesson, student=record.student).first()
    same_pending = grade and grade.import_record_id == record.pk and not grade.is_approved
    if action == 'approve':
        if ((grade and grade.value and not same_pending) or (attendance and attendance.status != Attendance.Status.PRESENT)) and not replace:
            raise ExternalAPIError('У клітинці є інший запис. Для заміни поставте окрему позначку «Замінити запис у клітинці».')
        if not grade:
            grade = Grade(lesson=record.lesson, student=record.student)
        grade.value = record.value
        grade.is_approved = True
        grade.import_record = record
        grade.created_by = user
        grade.is_synced_to_nz = False
        grade.save()
        if attendance and attendance.status != Attendance.Status.PRESENT:
            attendance.status = Attendance.Status.PRESENT
            attendance.is_synced_to_nz = False
            attendance.save()
        record.status = ExternalGrade.Status.APPLIED
    else:
        if same_pending:
            # Tentative marks were never sent; removing one must not clear an online mark.
            grade.delete()
        record.status = ExternalGrade.Status.REJECTED
    record.reviewed_by = user
    record.reviewed_at = timezone.now()
    record.save()
    AuditLog.objects.create(school=journal.school, user=user, action=AuditLog.Action.UPDATE,
        target_model='ExternalGrade', target_id=str(record.pk), description='Розглянуто імпортовану оцінку.',
        changes={'status': record.status, 'replace': replace})
    return record
