"""Ignore decisions on synthetic records, including repeated API pulls."""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.journals.models import APIInboxResult, ExtraStudent, Grade
from tests import test_simple_exchange as exchange


class IgnoreRegressionTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row
    pull = exchange.SimpleExchangeTests.pull

    def inbox_url(self):
        return reverse('portal:api_inbox', args=[self.journal.pk])

    def ignore(self, result, action='ignore_result', **extra):
        follow = extra.pop('follow', False)
        return self.client.post(self.inbox_url(), {
            'action': action, 'result_id': result.pk, **extra,
        }, follow=follow)

    def test_only_ignored_results_hide_card_in_both_views_and_restore_returns_it(self):
        connection = self.connection()
        self.pull(connection, self.row())
        result = APIInboxResult.objects.get()
        self.ignore(result)
        for query in ('', '?all=1'):
            response = self.client.get(self.inbox_url() + query)
            self.assertEqual(list(response.context['extra_page']), [])
            self.assertEqual(list(response.context['result_page']), [])
            self.assertNotContains(response, 'Підтвердити відповідність')
            self.assertEqual(list(response.context['ignored_results']), [result])
        self.assertEqual(ExtraStudent.objects.count(), 1)
        self.ignore(result, action='unignore_result')
        response = self.client.get(self.inbox_url())
        self.assertEqual(len(response.context['extra_page']), 1)
        self.assertEqual(len(response.context['result_page']), 1)
        self.assertFalse(Grade.objects.exists())

    def test_ignoring_one_date_keeps_other_pending_dates(self):
        connection = self.connection()
        self.pull(connection, self.row())
        self.pull(connection, self.row(id='other-date', date=self.l2.date.isoformat()))
        first = APIInboxResult.objects.get(external_id=self.row()['id'])
        other = APIInboxResult.objects.get(external_id='other-date')
        self.ignore(first)
        response = self.client.get(self.inbox_url())
        self.assertEqual(list(response.context['result_page']), [other])
        self.assertEqual(response.context['extra_page'][0].recent_results, [other])
        self.ignore(other)
        self.assertEqual(list(self.client.get(self.inbox_url()).context['extra_page']), [])

    def test_roster_only_students_remain_available_without_results(self):
        connection = self.connection()
        extra = ExtraStudent.objects.create(connection=connection, external_key='99', name='Вигаданий учень')
        self.assertEqual(list(self.client.get(self.inbox_url()).context['extra_page']), [extra])

    def test_ignored_unmatched_result_survives_correction_and_later_mapping(self):
        connection = self.connection()
        self.pull(connection, self.row())
        result = APIInboxResult.objects.get()
        self.ignore(result)
        extra = ExtraStudent.objects.get()
        extra.matched_student = self.student
        extra.save()
        revised = self.row(value='11', updated_at='2026-10-11T11:00:00+03:00')
        counts = self.pull(connection, revised).counts
        self.assertEqual(counts['ignored'], 1)
        self.assertEqual(counts['errors'], 0)
        self.assertFalse(Grade.objects.exists())
        result.refresh_from_db()
        self.assertTrue(result.is_ignored)
        self.assertTrue(result.resolved)
        self.assertEqual(result.payload['value'], '11')
        self.assertEqual(APIInboxResult.objects.count(), 1)
        self.ignore(result, action='unignore_result')
        self.assertFalse(Grade.objects.exists())
        self.assertEqual(self.pull(connection, revised).counts['added'], 1)
        self.assertEqual(Grade.objects.get().value, '11')

    def test_ignoring_correction_preserves_previously_imported_grade(self):
        connection = self.connection(mappings=True)
        row = self.row(student_id=41, student_key='41')
        self.pull(connection, row)
        result = APIInboxResult.objects.get()
        self.ignore(result)
        revised = dict(row, value='11', updated_at='2026-10-11T11:00:00+03:00')
        self.assertEqual(self.pull(connection, revised).counts['ignored'], 1)
        self.assertEqual(Grade.objects.get().value, '8')
        self.ignore(result, action='unignore_result')
        self.assertEqual(self.pull(connection, revised).counts['updated'], 1)
        self.assertEqual(Grade.objects.get().value, '11')

    def test_direct_apply_cannot_bypass_ignore(self):
        connection = self.connection(mappings=True)
        self.pull(connection, self.row(student_id=41, student_key='41', value='8.5'))
        result = APIInboxResult.objects.get()
        self.ignore(result)
        result.refresh_from_db()
        response = self.ignore(result, action='apply_saved', version=result.updated_at.isoformat(), follow=True)
        self.assertContains(response, 'Спочатку відновіть')
        self.assertFalse(Grade.objects.exists())

    def test_stale_ignore_form_does_not_hide_a_newer_result(self):
        connection = self.connection()
        self.pull(connection, self.row())
        result = APIInboxResult.objects.get()
        old_version = result.updated_at.isoformat()
        self.pull(connection, self.row(value='9', updated_at='2026-10-11T11:00:00+03:00'))
        response = self.ignore(result, version=old_version, follow=True)
        self.assertContains(response, 'Результат змінився')
        result.refresh_from_db()
        self.assertFalse(result.is_ignored)

    def test_localized_form_versions_match_the_same_instant(self):
        connection = self.connection()
        self.pull(connection, self.row())
        result = APIInboxResult.objects.get()
        extra = result.extra_student
        response = self.client.post(self.inbox_url(), {'action': 'match', 'extra_id': extra.pk,
            'version': timezone.localtime(extra.updated_at).isoformat(), 'student': self.student.pk})
        self.assertEqual(response.status_code, 302)
        extra.refresh_from_db()
        self.assertEqual(extra.matched_student_id, self.student.pk)
        self.ignore(result, version=timezone.localtime(result.updated_at).isoformat())
        result.refresh_from_db()
        self.assertTrue(result.is_ignored)
        self.ignore(result, action='unignore_result', version=timezone.localtime(result.updated_at).isoformat())
        result.refresh_from_db()
        self.assertFalse(result.is_ignored)
        response = self.ignore(result, action='apply_saved', version=timezone.localtime(result.updated_at).isoformat())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Grade.objects.get().value, '8')

    def test_mapped_pupil_invalid_mark_page_does_not_require_extra_student(self):
        connection = self.connection(mappings=True)
        self.pull(connection, self.row(student_id=41, student_key='41', value='8.5'))
        result = APIInboxResult.objects.get()
        self.assertIsNone(result.extra_student_id)
        self.assertContains(self.client.get(self.inbox_url()), 'Вигаданий У.')
        self.ignore(result)
        self.assertContains(self.client.get(self.inbox_url()), 'Ігноровані записи та оцінки (1)')

    def test_ignored_list_is_paginated_without_hiding_the_total(self):
        connection = self.connection()
        APIInboxResult.objects.bulk_create([
            APIInboxResult(connection=connection, external_id=f'ignored-{i}', is_ignored=True, resolved=True,
                payload={'student_name': 'Вигаданий учень', 'date': '2026-09-05', 'value': '8'})
            for i in range(51)
        ])
        response = self.client.get(self.inbox_url())
        self.assertContains(response, 'Ігноровані записи та оцінки (51)')
        self.assertEqual(len(response.context['ignored_results']), 50)
        response = self.client.get(self.inbox_url() + '?ignored_page=2')
        self.assertEqual(len(response.context['ignored_results']), 1)

    def test_another_teacher_cannot_ignore_restore_or_toggle_a_pupil(self):
        connection = self.connection()
        self.pull(connection, self.row())
        result = APIInboxResult.objects.get()
        other = User.objects.create_user(username='other-ignore-teacher', password='synthetic',
            role=User.Role.TEACHER, school=self.school)
        self.client.force_login(other)
        for action in ('ignore_result', 'unignore_result', 'apply_saved'):
            self.assertEqual(self.ignore(result, action=action).status_code, 403)
        toggle = reverse('portal:toggle_student_sync_ignore', args=[self.journal.pk, self.student.pk])
        self.assertEqual(self.client.post(toggle).status_code, 403)
        result.refresh_from_db()
        self.assertFalse(result.is_ignored)
        self.assertFalse(self.journal.ignored_students.exists())

    def test_family_status_menu_and_pull_preserve_manual_grade_and_restore(self):
        connection = self.connection(mappings=True)
        Grade.objects.create(lesson=self.l1, student=self.student, value='10', created_by=self.teacher)
        toggle = reverse('portal:toggle_student_sync_ignore', args=[self.journal.pk, self.student.pk])
        self.client.post(toggle)
        response = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        self.assertContains(response, 'Ігноровано · сімейна форма')
        self.assertContains(response, 'role="menuitemcheckbox" aria-checked="true"')
        self.assertContains(response, 'Ігноровано (сімейна форма) — відновити')
        counts = self.pull(connection, self.row(student_id=41, student_key='41')).counts
        self.assertEqual(counts['ignored'], 1)
        self.assertEqual(counts['errors'], 0)
        self.assertEqual(Grade.objects.get().value, '10')
        self.assertEqual(len(self.client.get(self.inbox_url()).context['result_page']), 0)
        self.client.post(toggle)
        self.assertFalse(self.journal.ignored_students.exists())
        self.assertEqual(self.pull(connection, self.row(student_id=41, student_key='41')).counts['conflicts'], 1)
        self.assertEqual(Grade.objects.get().value, '10')

    def test_family_pupil_unusable_mark_stays_out_of_decision_queue(self):
        connection = self.connection(mappings=True)
        self.journal.ignored_students.add(self.student)
        counts = self.pull(connection, self.row(student_id=41, student_key='41', value='8.5')).counts
        self.assertEqual(counts['ignored'], 1)
        self.assertEqual(counts['errors'], 0)
        self.assertFalse(Grade.objects.exists())
        self.assertEqual(len(self.client.get(self.inbox_url()).context['result_page']), 0)
