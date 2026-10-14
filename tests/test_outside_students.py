"""Standalone identities never create a teaching class or rewrite pupil history."""
import uuid
from django.test import TestCase
from django.urls import reverse
from apps.curriculum.models import StudentClass, Student
from apps.journals.models import ExtraStudent, APIInboxResult, Journal, Grade
from apps.journals.services.extra_students import keep_separate
from apps.journals.services.extra_students import merge_extra
from tests import test_simple_exchange as exchange


class OutsideStudentsTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row
    pull = exchange.SimpleExchangeTests.pull

    def test_separate_identity_creates_extracurricular_journal_and_records_grade(self):
        connection = self.connection()
        self.pull(connection, self.row())
        extra = ExtraStudent.objects.get()
        keep_separate(extra, self.teacher)
        extra.refresh_from_db()
        self.assertTrue(extra.is_separate)
        self.assertIsNotNone(extra.separate_student_id)
        self.assertIsNotNone(extra.separate_journal_id)
        self.assertEqual(extra.separate_journal.student_class.letter, 'ПК')
        self.assertEqual(extra.separate_journal.nz_journal_id, '')
        self.assertTrue(Grade.objects.filter(student=extra.separate_student).exists())
        self.assertTrue(APIInboxResult.objects.get().resolved)
        page = self.client.get(reverse('portal:api_inbox', args=[self.journal.pk]))
        self.assertContains(page, extra.name)
        self.assertContains(page, 'Залишено позакласним')

    def test_stable_outside_identity_groups_works_but_never_same_names(self):
        connection = self.connection()
        identity = str(uuid.uuid4())
        for attempt in (12, 13):
            row = self.row(id=f'work-{attempt}', attempt_id=attempt,
                outside_student_id=identity, student_key='outside:' + identity)
            self.pull(connection, row)
            self.pull(connection, row)
        self.assertEqual(ExtraStudent.objects.count(), 1)
        self.assertEqual(APIInboxResult.objects.count(), 2)
        other = str(uuid.uuid4())
        self.pull(connection, self.row(id='twin-work', outside_student_id=other, student_key='outside:' + other))
        self.assertEqual(ExtraStudent.objects.count(), 2)

    def test_conflicting_or_invalid_outside_identity_is_rejected(self):
        connection = self.connection()
        for values in ({'outside_student_id': 'bad'},
                       {'outside_student_id': str(uuid.uuid4()), 'student_id': 41}):
            self.assertEqual(self.pull(connection, self.row(**values)).counts['errors'], 1)
        self.assertFalse(ExtraStudent.objects.exists())

    def test_explicit_merge_preserves_old_work_ids_and_survives_repeated_pull(self):
        connection = self.connection()
        first, second = self.row(), self.row(id='second-work', attempt_id=13, student_key='unmapped:13')
        self.pull(connection, first)
        self.pull(connection, second)
        source, target = ExtraStudent.objects.order_by('pk')
        merge_extra(source, target, self.teacher)
        self.pull(connection, first)
        self.pull(connection, second)
        self.assertEqual(APIInboxResult.objects.filter(extra_student=target).count(), 2)
        self.assertEqual(ExtraStudent.objects.filter(alias_of__isnull=True).count(), 1)
        page = self.client.get(reverse('portal:api_inbox', args=[self.journal.pk]))
        self.assertEqual(list(page.context['extra_page']), [target])
        self.assertFalse(Grade.objects.exists())

    def test_separate_identity_can_be_corrected_to_a_classmate_explicitly(self):
        from apps.journals.services.extra_students import match_student
        connection = self.connection()
        self.pull(connection, self.row())
        extra = ExtraStudent.objects.get()
        keep_separate(extra, self.teacher)
        match_student(extra, self.student, self.teacher)
        self.assertFalse(extra.is_separate)
        self.assertEqual(self.pull(connection, self.row()).counts['added'], 1)
        self.assertEqual(Grade.objects.get().student_id, self.student.pk)

    def test_global_directory_uses_source_ids_and_hides_other_teachers(self):
        from apps.accounts.models import User
        from apps.journals.models import GradeAPIConnection
        connection = self.connection()
        identity = str(uuid.uuid4())
        row = self.row(outside_student_id=identity, student_key='outside:' + identity)
        self.pull(connection, row)
        # Same service and pupil in another connection is one directory identity.
        other = GradeAPIConnection.objects.create(journal=self.journal, source=connection.source,
            name='Друге підключення', contract_version=2, url=connection.url, source_class_id='7',
            source_subject_id='3', roster_cache=connection.roster_cache)
        self.pull(other, row)
        page = self.client.get(reverse('portal:outside_students'))
        self.assertEqual(len(page.context['page_obj']), 1)
        self.assertEqual(len(page.context['page_obj'][0]['contexts']), 2)
        stranger = User.objects.create_user(username='outside-stranger', school=self.school, role=User.Role.TEACHER)
        self.client.force_login(stranger)
        page = self.client.get(reverse('portal:outside_students'))
        self.assertEqual(len(page.context['page_obj']), 0)

    def test_merge_form_requires_confirmation_and_current_target_revision(self):
        connection = self.connection()
        self.pull(connection, self.row())
        self.pull(connection, self.row(id='second-work', attempt_id=13, student_key='unmapped:13'))
        source, target = ExtraStudent.objects.order_by('pk')
        url = reverse('portal:api_inbox', args=[self.journal.pk])
        data = {'action': 'merge_extra', 'extra_id': source.pk, 'version': source.updated_at.isoformat(),
                'target_extra': f'{target.pk}|{target.updated_at.isoformat()}'}
        self.client.post(url, data)
        source.refresh_from_db(); self.assertIsNone(source.alias_of_id)
        data['confirm_same_student'] = 'on'
        target.name = 'Уточнене ім’я'; target.save()
        self.client.post(url, data)
        source.refresh_from_db(); self.assertIsNone(source.alias_of_id)
        data['target_extra'] = f'{target.pk}|{target.updated_at.isoformat()}'
        self.client.post(url, data)
        source.refresh_from_db(); self.assertEqual(source.alias_of_id, target.pk)

    def test_legacy_service_journal_keeps_history_but_leaves_teaching_class_list(self):
        from tests.test_extra_decisions import ExtraDecisionTests
        connection = self.connection()
        extra = ExtraStudent.objects.create(connection=connection, external_key='99', name='Окремий Учень')
        ExtraDecisionTests.legacy_separate(self, extra)
        page = self.client.get(reverse('portal:teacher_dashboard'))
        self.assertEqual([journal.pk for journal in page.context['my_journals']], [self.journal.pk])
        self.assertTrue(Journal.objects.filter(pk=extra.separate_journal_id).exists())
        self.assertContains(self.client.get(reverse('portal:outside_students')), 'Історія в попередньому окремому журналі')

    def test_merge_extra_across_different_connections_of_same_journal(self):
        from apps.journals.models import GradeAPIConnection
        conn1 = self.connection()
        conn2 = GradeAPIConnection.objects.create(
            journal=self.journal, source='schooltest', name='Тести',
            contract_version=2, url=conn1.url, source_class_id='7',
            source_subject_id='3', roster_cache=conn1.roster_cache
        )
        self.pull(conn1, self.row(id='work-1', attempt_id=1, student_key='unmapped:1'))
        self.pull(conn2, self.row(id='test-1', attempt_id=2, student_key='unmapped:2'))
        source = ExtraStudent.objects.get(connection=conn1)
        target = ExtraStudent.objects.get(connection=conn2)
        self.assertNotEqual(source.connection_id, target.connection_id)

        url = reverse('portal:api_inbox', args=[self.journal.pk])
        data = {
            'action': 'merge_extra',
            'extra_id': source.pk,
            'version': source.updated_at.isoformat(),
            'target_extra': f'{target.pk}|{target.updated_at.isoformat()}',
            'confirm_same_student': 'on',
        }
        resp = self.client.post(url, data, follow=True)
        self.assertEqual(resp.status_code, 200)
        source.refresh_from_db()
        self.assertEqual(source.alias_of_id, target.pk)

    def test_keep_separate_with_nz_owner_or_linked_profile_synchronizes_access(self):
        from apps.accounts.models import User
        from apps.journals.models import NZAccount
        nz_teacher = User.objects.create_user(username='nz-teacher-remote', school=self.school, role=User.Role.TEACHER)
        self.journal.teacher = nz_teacher
        nz_acc = NZAccount.objects.create(user=self.teacher, login='nz_user', phpsessid='sess_123', is_active=True)
        self.journal.nz_owner = nz_acc
        self.journal.save()

        connection = self.connection()
        self.pull(connection, self.row())
        extra = ExtraStudent.objects.get(connection=connection)

        # Calling keep_separate must NOT fail with 'Окремий журнал належить іншому вчителю'
        extra_journal, created = keep_separate(extra, self.teacher)
        self.assertIsNotNone(extra_journal)
        self.assertTrue(extra.is_separate)
        from apps.portal.permissions import has_journal_access
        self.assertTrue(has_journal_access(self.teacher, extra_journal, write=True))
