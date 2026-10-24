"""Current, actionable integration state. Rendering never contacts a source."""
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.urls import reverse

from apps.journals.models import APIInboxResult, ExternalGrade, GradeAPIConnection, GradePullLog, GradeStudentMapping
from apps.journals.services.connection_setup import group_connections_by_server
from apps.portal.permissions import get_user_journals, has_journal_access


def operation_logs():
    logs = GradePullLog.objects.filter(connection_id=OuterRef('pk')).order_by('-created_at', '-pk')
    return {
        'grades': logs.filter(Q(counts__operation__isnull=True) | Q(counts__operation='grades')),
        'roster': logs.filter(counts__operation='roster'),
        'connection': logs.filter(counts__operation='connection'),
    }


def build_overview(journals):
    connections = list(GradeAPIConnection.objects.filter(journal__in=journals).select_related(
        'journal__student_class', 'journal__subject', 'journal__academic_year',
    ).annotate(has_mappings=Exists(GradeStudentMapping.objects.filter(connection_id=OuterRef('pk'))),
               **{f'{operation}_log_id': Subquery(query.values('pk')[:1])
                 for operation, query in operation_logs().items()}).order_by('journal_id', 'source', 'pk'))
    log_ids = {getattr(c, f'{op}_log_id') for c in connections for op in operation_logs()}
    logs = GradePullLog.objects.in_bulk(log_ids - {None})
    connection_ids = [c.pk for c in connections if c.enabled]
    review = dict(ExternalGrade.objects.filter(connection_id__in=connection_ids,
        status__in=[ExternalGrade.Status.PENDING, ExternalGrade.Status.CONFLICT]
    ).values('connection_id').annotate(total=Count('pk')).values_list('connection_id', 'total'))
    inbox = dict(APIInboxResult.objects.filter(connection_id__in=connection_ids, resolved=False, is_ignored=False
    ).values('connection_id').annotate(total=Count('pk')).values_list('connection_id', 'total'))
    notices, groups = [], group_connections_by_server(connections)
    for group in groups:
        items = group['connections']
        primary = next((c for c in items if c.enabled and c.is_default_for_group), None)
        primary = primary or next((c for c in items if c.enabled), items[0])
        journal = primary.journal
        hub = reverse('portal:journal_integrations', args=[journal.pk])
        setup = (reverse('portal:connect_source', args=[journal.pk]) + f'?connection={primary.pk}'
                 if primary.contract_version == 2 else hub + f'?advanced=1&edit={primary.pk}')
        group.update(journal=journal, primary=primary, setup_url=setup,
                     hub_url=hub, notices=[], enabled=any(c.enabled for c in items),
                     automatic=len([c for c in items if c.enabled]) > 1)

        def notice(kind, title, detail, action, url, level='warning'):
            item = {'id': f'api-{primary.pk}-{kind}', 'title': title, 'detail': detail,
                    'action': action, 'url': url, 'level': level, 'journal': journal,
                    'connection_name': group['name'], 'journal_id': journal.pk}
            notices.append(item)
            group['notices'].append(item)

        setup_pending = (primary.contract_version == 2 and not primary.enabled and
                         (not primary.source_class_id or not primary.source_subject_id or
                          (not primary.roster_sent_at and not primary.has_mappings)))
        if not group['enabled'] and not setup_pending:
            group['status_label'] = 'Вимкнено'
            continue
        active = [c for c in items if c.enabled or setup_pending]
        latest = {}
        for operation in operation_logs():
            candidates = [logs.get(getattr(c, f'{operation}_log_id')) for c in active]
            latest[operation] = max((log for log in candidates if log),
                                    key=lambda log: (log.created_at, log.pk), default=None)
        successes = [log for log in latest.values() if log and not log.counts.get('errors')]
        group['last_success'] = max((log.created_at for log in successes), default=None)
        for operation, log in latest.items():
            if not log or not log.counts.get('errors'):
                continue
            record_errors = any(isinstance(e, dict) and ('row' in e or 'id' in e) for e in log.errors)
            label = {'grades': 'Не вдалося отримати всі оцінки', 'roster': 'Список учнів не передано',
                     'connection': 'Не вдалося перевірити зв’язок'}[operation]
            detail = 'Перевірте адресу сайту, ключ і доступність сервера, потім повторіть дію.'
            if log.errors and isinstance(log.errors[0], dict):
                detail = str(log.errors[0].get('message') or detail)[:600]
                for connection in items:
                    if connection.api_key:
                        detail = detail.replace(connection.api_key, '••••')
            error_connection = next(c for c in items if c.pk == log.connection_id)
            error_setup = (reverse('portal:connect_source', args=[journal.pk]) + f'?connection={error_connection.pk}'
                           if error_connection.contract_version == 2 else hub + f'?advanced=1&edit={error_connection.pk}')
            target = error_setup + f'&notice_log={log.pk}' + (f'#sync-log-{log.pk}' if error_connection.contract_version == 2 else '#custom-api')
            action = 'Перевірити адресу та ключ'
            if operation == 'roster':
                target, action = error_setup + f'&notice_log={log.pk}' + (f'#sync-log-{log.pk}' if error_connection.contract_version == 2 else '#custom-api'), 'Перевірити список учнів'
            elif record_errors:
                target, action = hub + f'?advanced=1&tab=diagnostics&log={log.pk}#sync-log-{log.pk}', 'Переглянути причину'
                detail = 'Частину результатів не додано. Відкрийте причину, перевірте учня, дату уроку й оцінку та повторіть синхронізацію.'
            notice(operation, label, detail, action, target, 'danger')
        if primary.contract_version == 2:
            if setup_pending and primary.source_class_id and primary.source_subject_id:
                notice('setup', 'Завершіть підключення',
                       'Адресу та ключ збережено. Передайте список учнів або підтвердьте відповідності.',
                       'Завершити налаштування', setup + '#roster')
            if not primary.source_class_id or not primary.source_subject_id:
                notice('context', 'Підтвердьте клас і предмет',
                       'Оберіть, звідки отримувати результати для цього журналу.',
                       'Вибрати клас і предмет', setup + '#context')
            cache = primary.roster_cache if isinstance(primary.roster_cache, dict) else {}
            source_class = next((row.get('name', '') for row in cache.get('classes', [])
                                 if str(row.get('id')) == primary.source_class_id), '')
            def normalized(value):
                return ''.join(c for c in value.casefold().translate(str.maketrans('aceikmox', 'асеікмох')) if c.isalnum())
            if source_class and normalized(source_class) != normalized(journal.student_class.name):
                notice('class', 'Перевірте підключений клас',
                       f'У журналі «{journal.student_class.name}», на джерелі «{source_class}».',
                       'Перевірити клас', setup + '#context')
            # Use the most recently confirmed report among interchangeable addresses.
            roster_connection = max(active, key=lambda c: (c.roster_sent_at is not None, c.roster_sent_at or c.created_at))
            report = roster_connection.roster_report if isinstance(roster_connection.roster_report, dict) else {}
            counts = report.get('counts', {})
            if counts.get('needs_review') or counts.get('conflicts'):
                roster_setup = reverse('portal:connect_source', args=[journal.pk]) + f'?connection={roster_connection.pk}'
                notice('students', 'Список учнів потребує підтвердження',
                       f"Перевірте схожі імена та розбіжності: {counts.get('needs_review', 0) + counts.get('conflicts', 0)}.",
                       'Перевірити відповідності', roster_setup + '#roster')
        review_count = sum(review.get(c.pk, 0) for c in active)
        inbox_count = sum(inbox.get(c.pk, 0) for c in active)
        if review_count:
            notice('review', f'Оцінки очікують рішення: {review_count}',
                   'Схваліть попередні оцінки або вирішіть конфлікт із наявною оцінкою.',
                   'Переглянути оцінки', reverse('portal:external_grade_review', args=[journal.pk]))
        if inbox_count:
            notice('inbox', f'Результати потребують перевірки: {inbox_count}',
                   'Перевірте учня, дату уроку або залиште результат для окремого учня.',
                   'Перевірити результати', reverse('portal:api_inbox', args=[journal.pk]) + f'?source={primary.source}')
        group['status_label'] = 'Потребує уваги' if group['notices'] else ('Обмін працював' if group['last_success'] else 'Підключено · ще не перевірено обмін')
    notices.sort(key=lambda n: n['level'] != 'danger')
    return {'groups': groups, 'notices': notices}


def request_overview(request):
    if not hasattr(request, '_api_overview'):
        journals = get_user_journals(request.user).filter(is_archived=False).select_related('nz_owner__user')
        ids = [j.pk for j in journals if has_journal_access(request.user, j)]
        request._api_overview = build_overview(ids)
    return request._api_overview
