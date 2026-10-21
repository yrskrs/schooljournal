"""Preview API works without importing; confirm a lesson for one work at a time."""
import datetime as dt
import json
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_http_methods

from apps.journals.models import AuditLog, ExternalGrade, GradeAPIConnection, GradeLessonAssignment, Journal
from apps.journals.services.connection_setup import (
    exchange_with_fallback, find_online_connection_in_group, group_connections_by_server,
)
from apps.journals.services.external_grades import ExternalAPIError, fetch_grade_payload, import_grade_records, json_field
from apps.journals.services.grade_placement import assignment_map, connection_version, peer_connections, work_key
from apps.portal.forms_integrations import GradeLessonForm, PullGradesForm
from apps.portal.views_journal import _get_journal_or_403

PREVIEW_SESSION_KEY = 'grade_lesson_preview'
PREVIEW_LIFETIME = 20 * 60
MAX_PREVIEW_ROWS = 2000


def _saved_preview(request, journal, source):
    preview = request.session.get(PREVIEW_SESSION_KEY)
    if not isinstance(preview, dict) or preview.get('journal') != journal.pk or preview.get('source') != source:
        return None
    if timezone.now().timestamp() - preview.get('created', 0) > PREVIEW_LIFETIME:
        request.session.pop(PREVIEW_SESSION_KEY, None)
        return None
    return preview


def _source_groups(journal, source, connection_id):
    groups = group_connections_by_server(journal.grade_apis.filter(source=source, enabled=True))
    if connection_id:
        groups = [g for g in groups if any(str(c.pk) == str(connection_id) for c in g['connections'])]
    return groups


def _make_preview(request, journal, source, start, end, connection_id):
    batches, total = [], 0
    groups = _source_groups(journal, source, connection_id)
    if not groups:
        raise ExternalAPIError('Немає активного підключення. Спершу підключіть сайт у розділі «API та обмін».')
    for group in groups:
        # Preview only reads grades. In particular it must not transfer rosters
        # or create pupil mappings before the teacher confirms the operation.
        connection, rows = exchange_with_fallback(group['connections'],
            lambda candidate: fetch_grade_payload(candidate, start, end),
            preferred_id=connection_id, selector=find_online_connection_in_group)
        if not isinstance(rows, list):
            raise ExternalAPIError('Джерело повернуло некоректний список оцінок.')
        total += len(rows)
        if total > MAX_PREVIEW_ROWS:
            raise ExternalAPIError('Для попереднього перегляду оберіть коротший період: не більше 2000 результатів.')
        works = {}
        for row in rows:
            key = work_key(connection, row)
            if key is None:
                continue
            if connection.contract_version == 2 and (
                    row.get('source') != connection.source or str(row.get('class_id')) != connection.source_class_id
                    or str(row.get('subject_id')) != connection.source_subject_id):
                continue
            if key not in works:
                works[key] = {'id': secrets.token_urlsafe(16), 'work_key': key,
                    'title': str(row.get('title') or 'Робота без назви')[:200], 'rows': []}
            works[key]['rows'].append(row)
        batches.append({'connection': connection.pk, 'version': connection_version(connection),
                        'server_key': group['key'], 'works': list(works.values())})
    preview = {'token': secrets.token_urlsafe(32), 'created': timezone.now().timestamp(),
        'journal': journal.pk, 'source': source, 'scope_connection': connection_id,
        'start': start.isoformat(), 'end': end.isoformat(), 'batches': batches}
    if len(json.dumps(preview, ensure_ascii=False, allow_nan=False).encode()) > 3 * 1024 * 1024:
        raise ExternalAPIError('Попередній перегляд завеликий. Оберіть коротший період.')
    return preview


def _cards(journal, preview):
    cards = []
    if not preview:
        return cards
    for batch in preview['batches']:
        connection = journal.grade_apis.filter(pk=batch['connection'], enabled=True).first()
        if not connection:
            continue
        peers = peer_connections(connection)
        choices = assignment_map(connection, peers=peers)
        all_ids = [str(row.get('id') if connection.contract_version == 2 else json_field(row, connection.id_key))
                   for work in batch['works'] for row in work['rows']]
        known_ids = set(ExternalGrade.objects.filter(connection__in=peers, external_id__in=all_ids).values_list('external_id', flat=True))
        for work in batch['works']:
            dates, ids = [], []
            for row in work['rows']:
                raw_date = row.get('date') if connection.contract_version == 2 else json_field(row, connection.date_key)
                try:
                    dates.append(dt.date.fromisoformat(raw_date))
                except (ValueError, TypeError):
                    pass
                ids.append(str(row.get('id') if connection.contract_version == 2 else json_field(row, connection.id_key)))
            known = len(set(ids) & known_ids)
            selected = choices.get(work['work_key'])
            form = GradeLessonForm(journal=journal, initial={'lesson': selected.pk if selected else None},
                auto_id=f'lesson_{work["id"]}_%s')
            cards.append({'id': work['id'], 'title': work['title'], 'connection': connection.name,
                'count': len(work['rows']), 'known': known, 'date_from': min(dates) if dates else None,
                'date_to': max(dates) if dates else None, 'form': form})
    return cards


@login_required
@require_http_methods(['GET', 'POST'])
def choose_grade_lesson(request, journal_id, source):
    journal = _get_journal_or_403(request.user, journal_id, write=True)
    if source not in ('schooltest', 'schoolwork', 'generic'):
        return HttpResponseBadRequest('Невідоме джерело.')
    preview = _saved_preview(request, journal, source)
    initial = {'date_from': preview['start'] if preview else journal.academic_year.start_date,
        'date_to': preview['end'] if preview else max(journal.academic_year.start_date, min(timezone.localdate(), journal.academic_year.end_date))}
    if request.method == 'GET':
        for field in ('date_from', 'date_to'):
            if request.GET.get(field):
                initial[field] = request.GET[field]
    form = PullGradesForm(initial=initial)
    connection_id = request.POST.get('connection_id') or request.GET.get('connection_id') or (preview.get('scope_connection', '') if preview else '')
    if request.method == 'POST' and request.POST.get('action') == 'preview':
        form = PullGradesForm(request.POST)
        if form.is_valid():
            start, end = form.cleaned_data['date_from'], form.cleaned_data['date_to']
            if not journal.academic_year.start_date <= start <= end <= journal.academic_year.end_date:
                form.add_error(None, 'Оберіть період завершення робіт у межах навчального року.')
            else:
                try:
                    preview = _make_preview(request, journal, source, start, end, connection_id)
                    request.session[PREVIEW_SESSION_KEY] = preview
                    return redirect('portal:choose_grade_lesson', journal_id=journal.pk, source=source)
                except (ExternalAPIError, ValueError) as error:
                    form.add_error(None, str(error) if isinstance(error, ExternalAPIError) else 'Некоректна відповідь API. Оберіть коротший період або перевірте джерело.')
    elif request.method == 'POST' and request.POST.get('action') == 'apply':
        if not preview or not constant_time_compare(request.POST.get('preview_token', ''), preview['token']):
            messages.error(request, 'Попередній перегляд застарів. Отримайте список робіт ще раз.')
            return redirect('portal:choose_grade_lesson', journal_id=journal.pk, source=source)
        work_id = request.POST.get('work_id')
        selected = next(((batch, work) for batch in preview['batches'] for work in batch['works'] if work['id'] == work_id), None)
        if not selected:
            return HttpResponseBadRequest('Оберіть роботу з поточного попереднього перегляду.')
        batch, work = selected
        lesson_form = GradeLessonForm(request.POST, journal=journal)
        if not lesson_form.is_valid():
            messages.error(request, 'Оберіть наявний звичайний урок саме цього журналу.')
        else:
            connection = journal.grade_apis.filter(pk=batch['connection'], source=source, enabled=True).first()
            groups = _source_groups(journal, source, batch['connection'])
            if (not connection or not groups or groups[0]['key'] != batch['server_key']
                    or connection_version(connection) != batch['version']):
                messages.error(request, 'Налаштування підключення змінилися. Отримайте список робіт ще раз.')
            else:
                try:
                    with transaction.atomic():
                        Journal.objects.select_for_update().get(pk=journal.pk)
                        journal = _get_journal_or_403(request.user, journal.pk, write=True)
                        lesson = lesson_form.cleaned_data['lesson']
                        assignments = []
                        for peer in groups[0]['connections']:
                            if lesson:
                                assignment, _ = GradeLessonAssignment.objects.update_or_create(connection=peer,
                                    work_key=work['work_key'], defaults={'lesson': lesson, 'selected_by': request.user})
                                assignments.append(assignment.pk)
                            else:
                                GradeLessonAssignment.objects.filter(connection=peer, work_key=work['work_key']).delete()
                        log = import_grade_records(connection, work['rows'], request.user,
                            dt.date.fromisoformat(preview['start']), dt.date.fromisoformat(preview['end']))
                        AuditLog.objects.create(school=journal.school, user=request.user, action=AuditLog.Action.UPDATE,
                            target_model='GradeLessonAssignment', target_id=str(connection.pk),
                            description='Учитель обрав урок для оцінок зовнішньої роботи.' if lesson else 'Учитель повернув автоматичний вибір уроку.',
                            changes={'work_key': work['work_key'], 'lesson_id': lesson.pk if lesson else None,
                                     'assignments': assignments, 'counts': log.counts})
                    counts = log.counts
                    level = messages.WARNING if any(counts.get(k) for k in ('errors', 'conflicts', 'pending')) else messages.SUCCESS
                    destination = f'за {lesson.date:%d.%m.%Y}, урок №{lesson.lesson_number}' if lesson else 'автоматично за сесією та датою'
                    messages.add_message(request, level, f'«{work["title"]}»: нові оцінки {destination}. '
                        f'Додано {counts["added"]}; оновлено {counts["updated"]}; без змін {counts["unchanged"]}; '
                        f'конфлікти {counts["conflicts"]}; помилки {counts["errors"]}.')
                    batch['works'].remove(work)
                    request.session[PREVIEW_SESSION_KEY] = preview
                except ExternalAPIError as error:
                    messages.error(request, str(error))
        return redirect('portal:choose_grade_lesson', journal_id=journal.pk, source=source)
    response = render(request, 'portal/journal/choose_grade_lesson.html', {'journal': journal, 'source': source,
        'source_title': {'schooltest': 'Тести', 'schoolwork': 'Завдання', 'generic': 'Зовнішні роботи'}[source],
        'pull_form': form, 'connection_id': connection_id, 'preview': preview,
        'cards': Paginator(_cards(journal, preview), 20).get_page(request.GET.get('page')),
        'has_lessons': GradeLessonForm(journal=journal).fields['lesson'].queryset.exists()})
    response['Cache-Control'] = 'no-store'
    return response
