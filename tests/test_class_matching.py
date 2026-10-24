import copy
import datetime as dt
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from apps.curriculum.models import Student, StudentClass, ClassEnrollment, AcademicYear
from apps.journals.models import GradeAPIConnection, GradeStudentMapping, Journal, ExtraStudent, Grade
from apps.journals.services.connection_setup import roster_version
from apps.portal.forms_connection_setup import ConfirmStudentsForm
from apps.portal.views_api_inbox import exact_preview
from tests import test_network_profiles as fixtures


@override_settings(LOCAL_SERVER_PROFILE_ENABLED=False)
class ClassMatchingTests(TestCase):
    def setUp(self):
        fixtures.NetworkProfileTests.setUp(self)
        self.school = self.journal.school
        self.payload = {'schema_version': 2, 'source': 'schooltest', 'instance_id': str(self.service.instance_id),
            'classes': [{'id': '7', 'name': 'Інша назва класу'}, {'id': '8', 'name': 'Тестовий клас'}],
            'subjects': [{'id': '2', 'name': self.subject.name}], 'students': [
                {'id': '41', 'name': self.student.get_full_name_ukr(), 'class_id': '7'},
                {'id': '42', 'name': 'Вигадан Уч.', 'class_id': '7'}], 'capabilities': []}
        self.binding.roster_cache = self.payload; self.binding.save()
        self.service.roster_cache = self.payload; self.service.save()

    def confirm(self, **changes):
        data = {'connection_id': self.binding.pk, 'action': 'confirm_exact', 'confirm': 'on',
            'version': roster_version(self.binding), **changes}
        with patch('apps.portal.views_connection_setup.fetch_connection_roster', return_value=self.payload):
            return self.client.post(reverse('portal:connect_source', args=[self.journal.pk]), data)

    def test_matrix_has_two_sources_stable_ids_and_no_network_on_get(self):
        with patch('requests.Session', side_effect=AssertionError('GET renders cached names')):
            response = self.client.get(reverse('portal:connections_matching'))
        self.assertContains(response, 'SchoolTest · Тести')
        self.assertContains(response, 'SchoolNet · Завдання')
        self.assertContains(response, 'Інша назва класу · ID 7')
        self.assertNotContains(response, self.service.token)
        self.assertEqual(len(response.context['matrix']), 1)

    def test_same_name_in_different_year_is_a_separate_matrix_class(self):
        year = AcademicYear.objects.create(school=self.school, name='2027/2028',
            start_date=dt.date(2027, 9, 1), end_date=dt.date(2028, 6, 30))
        group = StudentClass.objects.create(school=self.school, academic_year=year, name=self.group.name)
        Journal.objects.create(school=self.school, academic_year=year, student_class=group,
            teacher=self.teacher, subject=self.subject)
        response = self.client.get(reverse('portal:connections_matching'))
        groups = response.context['matrix']
        self.assertEqual(len(groups), 2)
        self.assertNotEqual(groups[0]['uuid'], groups[1]['uuid'])

    def test_exact_confirmation_binds_only_unique_exact_ids(self):
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(list(self.binding.student_mappings.values_list('external_id', 'student_id')), [('41', self.student.pk)])
        self.assertFalse(Grade.objects.exists())

    def test_duplicate_names_and_fuzzy_are_never_mass_confirmed(self):
        twin = Student.objects.create(school=self.school, last_name=self.student.last_name, first_name=self.student.first_name)
        ClassEnrollment.objects.create(student=twin, student_class=self.group)
        form = ConfirmStudentsForm(connection=self.binding)
        self.assertEqual(form.exact_matches(self.binding), {})
        self.confirm()
        self.assertFalse(self.binding.student_mappings.exists())

    def test_stale_journal_name_or_membership_rejects_confirmation(self):
        version = roster_version(self.binding)
        self.student.first_name = 'Змінене ім’я'; self.student.save()
        response = self.confirm(version=version)
        self.assertContains(response, 'Списки оновилися')
        self.assertFalse(self.binding.student_mappings.exists())

    def test_stale_remote_roster_rejects_all_mapping_writes(self):
        old_version = roster_version(self.binding)
        self.payload = copy.deepcopy(self.payload)
        self.payload['students'][0]['name'] = 'Інша Людина'
        response = self.confirm(version=old_version)
        self.assertContains(response, 'Список на джерелі змінився')
        self.assertFalse(self.binding.student_mappings.exists())

    def test_binding_by_selected_id_survives_renamed_class(self):
        GradeStudentMapping.objects.create(connection=self.binding, external_id='41', student=self.student)
        with patch('apps.portal.views_connections.resolve_endpoint', return_value=(self.home_endpoint, {})), \
                patch('apps.portal.views_connections.fetch_source_roster', return_value=self.payload):
            response = self.client.post(reverse('portal:connection_bind', args=[self.journal.pk]), {
                'service': self.service.pk, 'source_class_id': '7', 'source_subject_id': '2'})
        self.assertEqual(response.status_code, 302)
        self.binding.refresh_from_db(); self.assertEqual(self.binding.source_class_id, '7')
        page = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        self.assertEqual(page.context['mismatched_connections'], [])

    def test_cannot_change_class_behind_existing_pupil_mapping(self):
        GradeStudentMapping.objects.create(connection=self.binding, external_id='41', student=self.student)
        with patch('apps.portal.views_connections.resolve_endpoint', return_value=(self.home_endpoint, {})), \
                patch('apps.portal.views_connections.fetch_source_roster', return_value=self.payload):
            self.client.post(reverse('portal:connection_bind', args=[self.journal.pk]), {
                'service': self.service.pk, 'source_class_id': '8', 'source_subject_id': '2'})
        self.binding.refresh_from_db(); self.assertEqual(self.binding.source_class_id, '7')
        self.assertEqual(self.binding.student_mappings.get().student, self.student)

    def test_outside_batch_requires_preview_and_ignores_ambiguous_source_names(self):
        extra = ExtraStudent.objects.create(connection=self.binding, external_key='91', name=self.student.get_full_name_ukr())
        pairs, version = exact_preview(self.journal)
        self.assertEqual(len(pairs), 1)
        url = reverse('portal:api_inbox', args=[self.journal.pk])
        self.client.post(url, {'action': 'batch_match', 'confirm': 'on'})
        extra.refresh_from_db(); self.assertIsNone(extra.matched_student_id)
        ExtraStudent.objects.create(connection=self.binding, external_key='92', name=extra.name)
        self.client.post(url, {'action': 'batch_match', 'confirm': 'on', 'batch_version': version})
        extra.refresh_from_db(); self.assertIsNone(extra.matched_student_id)
        self.assertEqual(exact_preview(self.journal)[0], [])
