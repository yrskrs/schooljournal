"""Roster page regressions: read-only GET and bounded rendering on synthetic data."""
import uuid
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from apps.accounts.models import User
from apps.curriculum.models import StudentClass
from apps.journals.models import GradeAPIConnection, Journal, NZAccount, RosterState, RosterReplica
from school_sync.django_backend import operate
from tests import test_journal_workflows as workflows


class RosterUIRegressionTests(TestCase):
    setUp = workflows.JournalWorkflowsTests.setUp

    def test_page_without_peer_configuration_is_read_only(self):
        with override_settings(ROSTER_SCHOOL_ID=0, ROSTER_YEAR_ID=0, ROSTER_PEERS=[]):
            RosterState.objects.all().delete()
            with patch('school_sync.django_backend.Engine.capture', side_effect=AssertionError('GET must not capture or mutate roster')):
                response = self.client.get(reverse('portal:roster_sync'))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(RosterState.objects.exists())
            self.assertContains(response, self.group.name)
            self.assertContains(response, f'value="{self.group.pk}"')
            self.assertContains(response, reverse('portal:connect_source', args=[self.journal.pk]))
            self.assertContains(response, 'Фоновий обмін ще не налаштовано')

    def test_class_selection_uses_accessible_journals_and_never_foreign_classes(self):
        second = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Другий доступний клас')
        Journal.objects.create(school=self.school, academic_year=self.year, student_class=second, subject=self.subject, teacher=self.teacher)
        other = User.objects.create_user(username='synthetic_roster_other', school=self.school, role=User.Role.TEACHER)
        foreign = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Чужий клас для перевірки')
        Journal.objects.create(school=self.school, academic_year=self.year, student_class=foreign, subject=self.subject, teacher=other)
        with override_settings(ROSTER_SCHOOL_ID=0, ROSTER_YEAR_ID=0, ROSTER_PEERS=[]):
            response = self.client.get(reverse('portal:roster_sync'), {'class_id': second.pk})
            self.assertEqual(response.context['teacher_roster_selected_class']['id'], second.pk)
            self.assertEqual([item.student_class_id for item in response.context['teacher_roster_journals']], [second.pk])
            self.assertNotContains(response, foreign.name)
            response = self.client.get(reverse('portal:roster_sync'), {'class_id': foreign.pk})
            self.assertNotEqual(response.context['teacher_roster_selected_class']['id'], foreign.pk)
            self.assertNotContains(response, foreign.name)

    def test_personal_nz_teacher_sees_owned_class_without_local_school(self):
        personal = User.objects.create_user(username='synthetic_roster_personal', role=User.Role.TEACHER, is_nz_personal=True)
        account = NZAccount.objects.create(user=personal, login='synthetic', phpsessid='synthetic', is_active=True)
        self.journal.nz_owner = account
        self.journal.save(update_fields=['nz_owner'])
        self.client.force_login(personal)
        with override_settings(ROSTER_SCHOOL_ID=0, ROSTER_YEAR_ID=0, ROSTER_PEERS=[]):
            response = self.client.get(reverse('portal:roster_sync'))
        self.assertContains(response, self.group.name)
        self.assertContains(response, self.subject.name)

    def test_no_journals_shows_actions_instead_of_empty_class_selector(self):
        Journal.objects.all().delete()
        response = self.client.get(reverse('portal:roster_sync'))
        self.assertContains(response, 'Створити журнал')
        self.assertContains(response, 'Завантажити з nz.ua')
        self.assertNotContains(response, 'id="rosterClass"')

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('portal:roster_sync')).status_code, 302)

    def test_existing_connection_has_explicit_transfer_without_sending_on_open(self):
        item = GradeAPIConnection.objects.create(journal=self.journal, source='schooltest', contract_version=2,
                                                name='Синтетичне підключення', url='http://schooltest-api:8000/api/v2/journal/grades/',
                                                api_key='synthetic-key', roster_cache={'capabilities': ['journal_roster_import']})
        with override_settings(ROSTER_SCHOOL_ID=0, ROSTER_YEAR_ID=0, ROSTER_PEERS=[]), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Opening must not transmit pupil data')):
            response = self.client.get(reverse('portal:roster_sync'))
        self.assertContains(response, 'Передати список')
        self.assertContains(response, f'name="connection_id" value="{item.pk}"')
        self.assertContains(response, 'Список ще не передавався')

    def test_large_ready_roster_does_not_build_a_pupil_picker_for_every_record(self):
        with override_settings(ROSTER_SCHOOL_ID=self.school.pk, ROSTER_YEAR_ID=self.year.pk, ROSTER_PEERS=[]):
            operate(lambda engine, store: engine.share('class', self.group.pk))
            parent = RosterReplica.objects.get(data__kind='class').ref
            rows = []
            for i in range(120):
                ref = str(uuid.uuid4())
                rows.append(RosterReplica(ref=ref, data={
                    'ref': ref, 'kind': 'student', 'native_id': self.student.pk,
                    'payload': {'name': 'Синтетичний '+str(i), 'class_ref': str(parent), 'active': True},
                    'clock': {}, 'status': 'ready', 'pending': [],
                }))
            RosterReplica.objects.bulk_create(rows)
            with patch('school_sync.django_backend.Engine.capture', side_effect=AssertionError('GET must stay read-only')):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse('portal:roster_sync'))
            self.assertEqual(response.status_code, 200)
            self.assertLess(len(queries), 25)
            self.assertLessEqual(len(response.context['roster_rows']), 50)
            second_page = self.client.get(reverse('portal:roster_sync'), {'page': 2, 'class_id': self.group.pk})
            self.assertEqual(second_page.context['roster_rows'].number, 2)
            self.assertEqual(second_page.context['teacher_roster_selected_class']['id'], self.group.pk)

    def test_peer_share_still_works_and_preserves_class_selection(self):
        with override_settings(ROSTER_SCHOOL_ID=self.school.pk, ROSTER_YEAR_ID=self.year.pk, ROSTER_PEERS=[]):
            url = reverse('portal:roster_sync') + f'?class_id={self.group.pk}'
            response = self.client.post(url, {'action': 'share', 'native_class_id': self.group.pk})
            self.assertEqual(response.url, url)
            response = self.client.get(url)
            self.assertContains(response, 'Узгоджено')
            self.assertTrue(RosterReplica.objects.filter(data__kind='class', data__native_id=self.group.pk).exists())
            row = next(row for row in response.context['roster_rows'] if row['kind'] == 'class')
            response = self.client.post(url, {'action': 'edit', 'ref': row['ref'], 'version': row['version'],
                                             'name': 'Перевірений клас', 'grade_level': 5, 'letter': 'А', 'active': '1'})
            self.assertEqual(response.url, url)
            self.group.refresh_from_db()
            self.assertEqual(self.group.name, 'Перевірений клас')
