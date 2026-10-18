from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from apps.journals.models import GradeAPIConnection, Grade
from apps.journals.services.connection_setup import group_connections_by_server, find_online_connection_in_group
from tests import test_simple_exchange as exchange


class GradeSyncFeedbackTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row

    def post(self, connection, **data):
        return self.client.post(reverse('portal:pull_source_grades', args=[self.journal.pk, connection.source]),
            {'date_from': self.year.start_date, 'date_to': self.year.end_date,
             'next': reverse('portal:journal_detail', args=[self.journal.pk]), **data},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_grade_action_reports_grade_counts_before_roster_review_and_does_not_replay_flash(self):
        connection = self.connection(mappings=True)
        row = self.row(student_id=41, student_key='41', value='8')
        with patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {'conflicts': 2, 'needs_review': 1}}), \
                patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[row]):
            response = self.post(connection)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['operation'], 'grades')
        self.assertEqual(body['level'], 'warning')
        self.assertTrue(body['message'].startswith('📥 Оцінки'))
        self.assertIn('додано 1', body['message'])
        self.assertIn('список потребує перевірки', body['message'])
        self.assertNotIn('Список учнів успішно передано', body['message'])
        self.assertEqual(Grade.objects.get().value, '8')
        self.assertEqual(body['counts']['added'], 1)
        page = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        self.assertEqual(list(page.context['messages']), [])

    def test_known_instance_addresses_without_manual_group_are_imported_once(self):
        first = self.connection(mappings=True)
        other = self.connection()
        other.url = 'http://offline.invalid:8000/api/v2/journal/grades/'
        other.save()
        with patch('apps.journals.services.connection_setup.verify_source_identity'), patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')) as probe, \
                patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {}}) as roster, \
                patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[]) as grades:
            response = self.post(first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['operation'], 'grades')
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(roster.call_count, 1)
        self.assertEqual(grades.call_count, 1)
        self.assertEqual(grades.call_args.args[0].pk, first.pk)

    def test_grouping_never_substitutes_other_instance_class_subject_or_journal(self):
        first = self.connection()
        rows = [first]
        for field, value in [('source_class_id', '8'), ('source_subject_id', '4'), ('instance_id', '22222222-2222-4222-8222-222222222222')]:
            other = self.connection()
            other.server_group = 'Same label'
            if field == 'instance_id':
                other.roster_cache = {**other.roster_cache, 'instance_id': value}
            else:
                setattr(other, field, value)
            other.save()
            rows.append(other)
        first.server_group = 'Same label'; first.save()
        self.assertEqual(len(group_connections_by_server(rows)), 4)

    def test_offline_endpoint_falls_back_without_probing_after_first_success(self):
        first, second, third = self.connection(), self.connection(), self.connection()
        with patch('apps.journals.services.connection_setup.verify_source_identity'), patch('apps.journals.services.connection_setup.probe_connection_reachability', side_effect=[(False, 'offline'), (True, 'online')]) as probe:
            active, results = find_online_connection_in_group([first, second, third])
        self.assertEqual(active.pk, second.pk)
        self.assertEqual(probe.call_count, 2)
        self.assertNotIn(third.pk, results)

    def test_explicit_grade_endpoint_keeps_verified_sibling_for_fallback(self):
        first, second = self.connection(), self.connection()
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', side_effect=[(False, 'offline'), (True, 'online')]), \
                patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {}}), \
                patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[]) as grades:
            response = self.post(first, connection_id=str(first.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(grades.call_args.args[0].pk, second.pk)

    def test_roster_endpoint_uses_same_fallback_and_identifies_its_operation(self):
        first, second = self.connection(), self.connection()
        counts = dict(created=0, updated=0, ignored=0, needs_review=1, conflicts=2)
        with patch('apps.journals.services.connection_setup.verify_source_identity'), \
                patch('apps.journals.services.connection_setup.probe_connection_reachability', side_effect=[(False, 'offline'), (True, 'online')]), \
                patch('apps.portal.views_connection_setup.send_journal_roster', return_value={'counts': counts}) as roster:
            response = self.client.post(reverse('portal:quick_send_roster', args=[self.journal.pk, first.pk]),
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['operation'], 'roster')
        self.assertEqual(response.json()['level'], 'warning')
        self.assertEqual(roster.call_args.args[0].pk, second.pk)

    def test_identity_preflight_is_reused_only_for_unchanged_connection(self):
        from apps.journals.services.connection_setup import verify_source_identity
        connection = self.connection()
        with patch('apps.journals.services.connection_setup.fetch_source_roster', return_value=connection.roster_cache) as fetch:
            verify_source_identity(connection)
            verify_source_identity(connection)
            self.assertEqual(fetch.call_count, 1)
            connection.url = 'http://another.invalid:8000/api/v2/journal/grades/'
            verify_source_identity(connection)
            self.assertEqual(fetch.call_count, 2)
            connection.api_key = 'another-synthetic-key'
            verify_source_identity(connection)
            self.assertEqual(fetch.call_count, 3)

    def test_grade_failure_is_explicit_json_without_roster_success_text(self):
        from apps.journals.services.external_grades import ExternalAPIError
        connection = self.connection()
        with patch('apps.portal.views_integrations.send_journal_roster', return_value={'counts': {}}), \
                patch('apps.portal.views_integrations.fetch_grade_payload', side_effect=ExternalAPIError('Джерело недоступне.')):
            response = self.post(connection)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['operation'], 'grades')
        self.assertIn('Джерело недоступне', response.json()['message'])
        self.assertFalse(Grade.objects.exists())

    def test_rendered_buttons_have_explicit_distinct_form_owners_and_disabled_endpoints_are_not_choices(self):
        first = self.connection()
        disabled = self.connection()
        disabled.enabled = False; disabled.save()
        response = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        soup = BeautifulSoup(response.content, 'html.parser')
        grades = soup.select_one('#formPullTestGrades')
        roster = soup.select_one('#formSendTestRoster')
        self.assertEqual(grades['data-sync-operation'], 'grades')
        self.assertEqual(roster['data-sync-operation'], 'roster')
        self.assertEqual(grades.select_one('button')['form'], grades['id'])
        self.assertEqual(roster.select_one('button')['form'], roster['id'])
        self.assertNotEqual(grades['action'], roster['action'])
        self.assertEqual(response.context['test_connection']['has_multiple_endpoints'], False)

    def test_wrong_live_instance_cannot_receive_roster_or_supply_grades(self):
        from apps.journals.services.external_grades import ExternalAPIError, fetch_grade_payload
        from apps.journals.services.roster_transfer import send_journal_roster
        connection = self.connection()
        other = {**connection.roster_cache, 'instance_id': '22222222-2222-4222-8222-222222222222'}
        with patch('apps.journals.services.connection_setup.fetch_source_roster', return_value=other), patch('apps.journals.services.roster_transfer.requests.Session') as network:
            with self.assertRaises(ExternalAPIError):
                send_journal_roster(connection, self.teacher)
            network.assert_not_called()
        with patch('apps.journals.services.connection_setup.fetch_source_roster', return_value=other), patch('apps.journals.services.school_api.requests.Session') as network:
            with self.assertRaises(ExternalAPIError):
                fetch_grade_payload(connection, self.year.start_date, self.year.end_date)
            network.assert_not_called()

    def test_reachable_wrong_instance_falls_back_to_verified_sibling(self):
        from apps.journals.services.external_grades import ExternalAPIError
        first, second = self.connection(), self.connection()
        with patch('apps.journals.services.connection_setup.probe_connection_reachability', return_value=(True, 'online')), patch('apps.journals.services.connection_setup.verify_source_identity', side_effect=[ExternalAPIError('Інший екземпляр.'), None]):
            active, _ = find_online_connection_in_group([first, second])
        self.assertEqual(active.pk, second.pk)
