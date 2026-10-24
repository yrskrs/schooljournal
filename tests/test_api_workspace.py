from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.journals.models import Grade, GradeAPIConnection, GradePullLog, GradeStudentMapping
from apps.journals.services.connection_setup import exchange_with_fallback, group_connections_by_server
from apps.journals.services.external_grades import ExternalAPIError, import_grade_records
from apps.portal.api_workspace import build_overview
from tests import test_simple_exchange as exchange


class APIWorkspaceTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row

    def log(self, connection, operation='grades', errors=0, **kwargs):
        return GradePullLog.objects.create(connection=connection, user=self.teacher,
            counts={'operation': operation, 'errors': errors, **kwargs},
            errors=[{'message': 'Ключ не прийнято.'}] if errors else [])

    def overview(self):
        return build_overview([self.journal.pk])

    def test_workspace_and_journal_open_simple_view_and_keep_technical_settings_collapsed(self):
        self.connection()
        for url in [reverse('portal:api_workspace'), reverse('portal:journal_integrations', args=[self.journal.pk])]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'portal/api/connections.html' if url == reverse('portal:api_workspace') else 'portal/api/workspace.html')
            self.assertIn('no-store', response['Cache-Control'])
            soup = BeautifulSoup(response.content, 'html.parser')
            if soup.select_one('.api-advanced'):
                self.assertFalse(soup.select_one('.api-advanced').has_attr('open'))
            self.assertIsNotNone(soup.select_one('a[href$="?tab=notifications"]'))
            self.assertNotContains(response, 'synthetic-key')

    def test_rendering_overview_and_notification_badge_never_contacts_sources(self):
        connection = self.connection()
        self.log(connection, errors=1)
        with patch('requests.Session', side_effect=AssertionError('Rendering must not use the network')):
            for name in ('api_workspace', 'api_notifications', 'teacher_dashboard'):
                response = self.client.get(reverse('portal:' + name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'api-notice-count')

    def test_latest_success_resolves_error_without_removing_history(self):
        connection = self.connection()
        self.log(connection, errors=1)
        self.assertEqual(len(self.overview()['notices']), 1)
        self.log(connection)
        self.assertEqual(self.overview()['notices'], [])
        self.assertEqual(connection.pull_logs.count(), 2)

    def test_roster_and_connection_success_cannot_hide_failed_grades(self):
        connection = self.connection()
        self.log(connection, errors=1)
        self.log(connection, 'roster')
        self.log(connection, 'connection')
        notice, = self.overview()['notices']
        self.assertIn('оцінки', notice['title'])

    def test_grade_success_cannot_hide_failed_roster(self):
        connection = self.connection()
        self.log(connection, 'roster', errors=1)
        self.log(connection)
        notice, = self.overview()['notices']
        self.assertIn('Список', notice['title'])
        self.assertIn('&notice_log=', notice['url'])
        self.assertIn('#sync-log-', notice['url'])

    def test_successful_sibling_resolves_group_error_but_other_instances_stay_separate(self):
        first, backup = self.connection(), self.connection()
        self.log(first, errors=1)
        self.log(backup)
        self.assertEqual(len(self.overview()['groups']), 1)
        self.assertEqual(self.overview()['notices'], [])
        other = self.connection()
        other.roster_cache['instance_id'] = '22222222-2222-4222-8222-222222222222'
        other.save()
        self.log(other, errors=1)
        self.assertEqual(len(self.overview()['groups']), 2)
        self.assertEqual(len(self.overview()['notices']), 1)

    def test_failed_check_links_to_exact_address_in_group(self):
        first, backup = self.connection(), self.connection()
        log = self.log(backup, 'connection', errors=1)
        notice, = self.overview()['notices']
        self.assertIn(f'?connection={backup.pk}&notice_log={log.pk}#sync-log-{log.pk}', notice['url'])
        self.assertNotIn(f'?connection={first.pk}#address', notice['url'])

    def test_old_failed_grade_log_remains_visible_from_its_notification(self):
        connection = self.connection(mappings=True)
        error = self.log(connection, errors=1)
        error.errors = [{'row': 4, 'message': 'Оцінка має бути цілим числом. synthetic-key'}]
        error.save()
        for _ in range(30):
            self.log(connection, 'connection')
        notice = next(n for n in self.overview()['notices'] if n['id'].endswith('-grades'))
        self.assertIn(f'log={error.pk}#sync-log-{error.pk}', notice['url'])
        response = self.client.get(notice['url'])
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        highlighted = soup.select_one(f'#sync-log-{error.pk}[aria-current=true]')
        self.assertIsNotNone(highlighted)
        self.assertIn('Рядок 4:', highlighted.text)
        self.assertIn('цілим числом', highlighted.text)
        self.assertNotContains(response, 'synthetic-key')

        self.assertContains(response, 'У черзі немає збережених результатів для ручного виправлення.')
        self.assertNotContains(response, 'Перевірити збережені результати та їхні причини')

    def test_roster_failure_page_displays_the_persisted_reason(self):
        connection = self.connection(mappings=True)
        log = self.log(connection, 'roster', errors=1)
        log.errors = [{'message': 'Потрібне підтвердження ID. synthetic-key'}]
        log.save()
        notice, = self.overview()['notices']
        page = self.client.get(notice['url'])
        self.assertContains(page, 'Причина сповіщення')
        self.assertContains(page, 'Потрібне підтвердження ID.')
        self.assertNotContains(page, 'synthetic-key')

    def test_review_candidates_missing_from_cache_have_real_form_fields(self):
        connection = self.connection(mappings=True)
        connection.roster_report = {'schema_version': 2, 'source': connection.source,
            'instance_id': connection.roster_cache['instance_id'], 'class_id': connection.source_class_id,
            'counts': {'needs_review': 1, 'conflicts': 0},
            'needs_review': [{'journal_id': 'synthetic-stable-id', 'name': 'Учень журналу',
                              'candidates': [{'id': 99, 'name': 'Кандидат джерела'}]}]}
        connection.save()
        notice, = self.overview()['notices']
        page = self.client.get(notice['url'])
        self.assertContains(page, 'Учень журналу')
        self.assertContains(page, 'Кандидат джерела')
        self.assertContains(page, 'id="id_pupil_99"')
        self.assertContains(page, 'id="students" open')

    def test_selected_log_cannot_open_another_journal_or_connection(self):
        first, second = self.connection(), self.connection()
        log = self.log(second, 'roster', errors=1)
        url = reverse('portal:connect_source', args=[self.journal.pk])
        self.assertEqual(self.client.get(url, {'connection': first.pk, 'notice_log': log.pk}).status_code, 404)
        url = reverse('portal:journal_integrations', args=[self.journal.pk])
        self.assertEqual(self.client.get(url, {'advanced': 1, 'tab': 'diagnostics', 'log': 'bad'}).status_code, 400)

    def test_notification_is_a_clickable_card_with_context_and_persisted_reason(self):
        connection = self.connection()
        self.log(connection, 'roster', errors=1)
        response = self.client.get(reverse('portal:api_notifications'))
        soup = BeautifulSoup(response.content, 'html.parser')
        card = soup.select_one('a.api-notice')
        self.assertIn('Ключ не прийнято.', card.text)
        self.assertIn(self.group.name, card.text)
        self.assertIn(self.subject.name, card.text)
        self.assertIn(f'?connection={connection.pk}&notice_log=', card['href'])
        self.assertIn('#sync-log-', card['href'])
        self.assertEqual(self.client.get(reverse('portal:api_notifications')).context['api_notices'], response.context['api_notices'])

    def test_confirmed_roster_conflicts_link_to_roster_and_disappear_after_new_report(self):
        connection = self.connection()
        connection.roster_report = {'counts': {'conflicts': 1, 'needs_review': 2}}
        connection.save()
        notice, = self.overview()['notices']
        self.assertIn('3', notice['detail'])
        self.assertTrue(notice['url'].endswith('#roster'))
        connection.roster_report = {'counts': {'conflicts': 0, 'needs_review': 0}}
        connection.save()
        self.assertEqual(self.overview()['notices'], [])

    def test_pending_and_unmatched_grades_lead_to_different_review_pages(self):
        connection = self.connection(mappings=True)
        old_api = GradeAPIConnection.objects.create(journal=self.journal, name='Інші роботи', url='http://10.44.1.21/api/')
        pending = {'id': 'pending', 'student_id': self.student.pk, 'date': self.l1.date.isoformat(),
                   'value': '8', 'is_ai': True, 'approved_by_teacher': False}
        import_grade_records(old_api, [pending], self.teacher, self.year.start_date, self.year.end_date)
        import_grade_records(connection, [self.row(student_id=41, student_key='41', value='8.5')],
            self.teacher, self.year.start_date, self.year.end_date)
        urls = [n['url'] for n in self.overview()['notices']]
        self.assertIn(reverse('portal:external_grade_review', args=[self.journal.pk]), urls)
        self.assertIn(reverse('portal:api_inbox', args=[self.journal.pk]) + '?source=schooltest', urls)

    def test_disabled_completed_connection_does_not_raise_old_failures(self):
        connection = self.connection(mappings=True)
        self.log(connection, errors=1)
        connection.enabled = False
        connection.save()
        self.assertEqual(self.overview()['notices'], [])
        self.assertEqual(self.overview()['groups'][0]['status_label'], 'Вимкнено')

    def test_unfinished_connection_and_failure_are_visible(self):
        connection = self.connection()
        connection.enabled = False
        connection.save()
        self.log(connection, 'roster', errors=1)
        notices = self.overview()['notices']
        self.assertTrue(any('Список' in n['title'] for n in notices))
        self.assertTrue(any('Завершіть' in n['title'] for n in notices))

    def test_other_teachers_notifications_and_keys_are_not_exposed(self):
        connection = self.connection()
        self.log(connection, errors=1)
        outsider = User.objects.create_user(username='api-outsider', school=None)
        self.client.force_login(outsider)
        response = self.client.get(reverse('portal:api_notifications'))
        self.assertEqual(response.context['api_notices'], [])
        self.assertNotContains(response, self.group.name)
        self.assertEqual(self.client.get(reverse('portal:api_workspace') + f'?journal={self.journal.pk}').status_code, 403)

    def test_own_personal_nz_journal_without_local_school_is_in_workspace(self):
        from apps.journals.models import NZAccount
        self.teacher.school = None
        self.teacher.save()
        self.journal.nz_owner = NZAccount.objects.create(user=self.teacher, is_active=True)
        self.journal.save()
        self.connection()
        response = self.client.get(reverse('portal:api_workspace'))
        self.assertEqual(len(response.context['api_groups']), 1)

    def test_archived_journal_does_not_leave_global_notification_badge(self):
        connection = self.connection()
        self.log(connection, errors=1)
        self.journal.is_archived = True
        self.journal.save()
        response = self.client.get(reverse('portal:api_notifications'))
        self.assertEqual(response.context['api_notices'], [])

    def test_malformed_journal_selection_is_rejected_without_server_error(self):
        self.assertEqual(self.client.get(reverse('portal:api_workspace') + '?journal=oops').status_code, 400)

    def test_add_api_selector_opens_setup_for_selected_journal(self):
        response = self.client.get(reverse('portal:api_workspace'), {'journal': self.journal.pk, 'action': 'connect'})
        self.assertRedirects(response, reverse('portal:connections_matching') + f'?journal={self.journal.pk}')

    def test_anonymous_cannot_read_workspace_or_notifications(self):
        self.client.logout()
        for name in ('api_workspace', 'api_notifications'):
            self.assertEqual(self.client.get(reverse('portal:' + name)).status_code, 302)

    def test_connection_check_failure_is_persisted_and_success_resolves_it(self):
        connection = self.connection()
        url = reverse('portal:test_source_connection', args=[self.journal.pk, connection.pk])
        with patch('apps.portal.views_connection_setup.fetch_source_roster', side_effect=ExternalAPIError('Ключ не прийнято.')):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.overview()['notices']), 1)
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=connection.roster_cache):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.overview()['notices'], [])

    def test_connection_check_cannot_replace_verified_instance(self):
        connection = self.connection()
        cache = dict(connection.roster_cache)
        wrong = {**cache, 'instance_id': '22222222-2222-4222-8222-222222222222'}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=wrong):
            response = self.client.post(reverse('portal:test_source_connection', args=[self.journal.pk, connection.pk]),
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        connection.refresh_from_db()
        self.assertEqual(connection.roster_cache, cache)

    def test_backup_form_preserves_original_address_and_copies_confirmed_mappings(self):
        from apps.journals.services.roster_transfer import roster_request
        first = self.connection(mappings=True)
        _, refs = roster_request(first)
        ref = next(ref for ref, student in refs.items() if student.pk == self.student.pk)
        first.roster_report = {'schema_version': 2, 'source': first.source,
            'instance_id': first.roster_cache['instance_id'], 'class_id': first.source_class_id,
            'mappings': [{'journal_id': ref, 'local_id': 41}]}
        first.save()
        GradeStudentMapping.objects.create(connection=first, external_id='99', student=self.student)
        url = reverse('portal:connect_source', args=[self.journal.pk])
        page = self.client.get(url, {'backup_for': first.pk})
        self.assertContains(page, 'Додати резервну адресу')
        self.assertNotContains(page, 'name="apply_to_all_classes"')
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=first.roster_cache), \
                patch('apps.portal.views_connection_setup.send_journal_roster', return_value={'counts': {}}):
            response = self.client.post(url, {'action': 'connect', 'backup_for': first.pk, 'source': first.source,
                'site_url': 'http://10.44.1.20:8000', 'api_key': 'backup-synthetic-key'})
        self.assertEqual(response.status_code, 302)
        backup = GradeAPIConnection.objects.exclude(pk=first.pk).get()
        first.refresh_from_db()
        self.assertEqual(first.url, 'http://schooltest-api:8000/api/v2/journal/grades/')
        self.assertEqual(backup.source_class_id, first.source_class_id)
        self.assertEqual(backup.source_subject_id, first.source_subject_id)
        self.assertEqual(backup.student_mappings.get(external_id='41').student, self.student)
        roster, _ = roster_request(backup)
        self.assertEqual(next(row['local_id'] for row in roster['students'] if row['id'] == ref), '41')
        self.assertEqual(len(group_connections_by_server([first, backup])), 1)

    def test_backup_from_another_instance_is_rejected_before_sending_students(self):
        first = self.connection()
        wrong = {**first.roster_cache, 'instance_id': '22222222-2222-4222-8222-222222222222'}
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=wrong), \
                patch('apps.portal.views_connection_setup.send_journal_roster') as send:
            response = self.client.post(reverse('portal:connect_source', args=[self.journal.pk]),
                {'action': 'connect', 'backup_for': first.pk, 'source': first.source,
                 'site_url': 'http://10.44.1.20:8000', 'api_key': 'backup-synthetic-key'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'інший сервіс')
        self.assertEqual(GradeAPIConnection.objects.count(), 1)
        send.assert_not_called()

    def test_actual_grade_api_failure_retries_backup_and_imports_once(self):
        first, backup = self.connection(mappings=True), self.connection(mappings=True)
        row = self.row(student_id=41, student_key='41', value='8')
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')), \
                patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {}}), \
                patch('apps.portal.views_integrations.fetch_grade_payload', side_effect=[ExternalAPIError('HTTP 503'), [row]]) as fetch:
            response = self.client.post(reverse('portal:pull_source_grades', args=[self.journal.pk, first.source]),
                {'date_from': self.year.start_date, 'date_to': self.year.end_date}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Grade.objects.count(), 1)
        self.assertEqual(fetch.call_args_list[1].args[0].pk, backup.pk)
        self.assertEqual(self.overview()['notices'], [])

    def test_successful_fallback_does_not_replay_warnings_from_failed_address(self):
        first, backup = self.connection(), self.connection()
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')), \
                patch('apps.portal.views_integrations.send_journal_roster', side_effect=[ExternalAPIError('Стара адреса недоступна'), {'counts': {}}]), \
                patch('apps.portal.views_integrations.fetch_grade_payload', side_effect=[ExternalAPIError('HTTP 503'), []]):
            response = self.client.post(reverse('portal:pull_source_grades', args=[self.journal.pk, first.source]),
                {'date_from': self.year.start_date, 'date_to': self.year.end_date}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['level'], 'success')
        self.assertNotIn('Стара адреса', response.json()['message'])

    def test_changing_address_keeps_saved_key_and_resolves_connection_notice(self):
        connection = self.connection(mappings=True)
        self.log(connection, 'connection', errors=1)
        with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=connection.roster_cache) as fetch, \
                patch('apps.portal.views_connection_setup.send_journal_roster', return_value={'counts': {}}):
            response = self.client.post(reverse('portal:connect_source', args=[self.journal.pk]),
                {'action': 'connect', 'connection_id': connection.pk, 'source': connection.source,
                 'site_url': 'http://10.44.1.25:8000', 'api_key': '', 'key_name': connection.name})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fetch.call_args.args[1], connection.api_key)
        connection.refresh_from_db()
        self.assertEqual(connection.api_key, 'synthetic-key')
        self.assertEqual(self.overview()['notices'], [])

    def test_custom_api_notice_opens_edit_form_without_exposing_saved_key(self):
        connection = GradeAPIConnection.objects.create(journal=self.journal, name='Інша система',
            url='http://10.44.1.21/api/', api_key='synthetic-custom-secret')
        self.log(connection, 'connection', errors=1)
        notice, = self.overview()['notices']
        self.assertTrue(notice['url'].endswith('#custom-api'))
        response = self.client.get(notice['url'].split('#')[0])
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        form = soup.select_one('#custom-api[open] form')
        self.assertIsNotNone(form)
        self.assertEqual(form.select_one('[name="url"]')['value'], connection.url)
        self.assertNotContains(response, 'synthetic-custom-secret')

    def test_journal_uses_auto_selection_before_manual_endpoint_choice(self):
        self.connection()
        self.connection()
        response = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('#selectTestEndpoint option[selected]')['value'], '')
        self.assertEqual(soup.select_one('#testConnectionId')['value'], '')

    def test_actual_roster_api_failure_retries_backup(self):
        first, backup = self.connection(), self.connection()
        report = {'counts': {'created': 0, 'updated': 0, 'needs_review': 0, 'conflicts': 0}}
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')), \
                patch('apps.portal.views_connection_setup.send_journal_roster', side_effect=[ExternalAPIError('HTTP 503'), report]) as send:
            response = self.client.post(reverse('portal:quick_send_roster', args=[self.journal.pk, first.pk]),
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send.call_args_list[1].args[0].pk, backup.pk)
        self.assertIn('?tab=notifications', response.json()['attention_url'])

    def test_all_api_addresses_failing_produces_persistent_notice_and_action_url(self):
        first, backup = self.connection(), self.connection()
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')), \
                patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {}}), \
                patch('apps.portal.views_integrations.fetch_grade_payload', side_effect=ExternalAPIError('HTTP 503')):
            response = self.client.post(reverse('portal:pull_source_grades', args=[self.journal.pk, first.source]),
                {'date_from': self.year.start_date, 'date_to': self.year.end_date}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        self.assertIn('?tab=notifications', response.json()['attention_url'])
        self.assertEqual(len(self.overview()['notices']), 1)
        self.assertFalse(Grade.objects.exists())

    def test_disabled_address_is_never_used_by_exchange(self):
        connection = self.connection()
        connection.enabled = False
        with self.assertRaises(ExternalAPIError), patch('requests.Session') as network:
            exchange_with_fallback([connection], lambda c: None)
        network.assert_not_called()

    def test_beginner_guide_is_public_and_explains_actions_and_recovery(self):
        self.client.logout()
        response = self.client.get(reverse('portal:guide'))
        for text in ('id="api-start"', 'інструкція простими словами', 'Якщо є помилка', 'резервні адреси', 'Сповіщення'):
            self.assertContains(response, text)
