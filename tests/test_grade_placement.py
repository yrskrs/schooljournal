"""Teacher-selected lesson placement without moving existing grade history."""
import datetime as dt
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.curriculum.models import Student, ClassEnrollment, Subject
from apps.journals.models import (
    Journal, Lesson, Grade, Attendance, GradeAPIConnection, GradeLessonAssignment,
    GradeStudentMapping, GradePullLog, ExternalGrade, APIInboxResult, ExtraStudent, AuditLog,
)
from apps.journals.services.external_grades import import_grade_records
from apps.journals.services.grade_placement import work_key
from tests import test_school_api_v2 as api
from apps.portal.views_grade_placement import PREVIEW_SESSION_KEY


class GradePlacementTests(TestCase):
    setUp = api.SchoolAPIV2Tests.setUp
    row = api.SchoolAPIV2Tests.row
    pull = api.SchoolAPIV2Tests.pull

    def url(self, source=None, journal=None):
        return reverse('portal:choose_grade_lesson', args=[(journal or self.journal).pk, source or self.connection.source])

    def preview(self, rows, **values):
        def exchange(connections, operation, **kwargs):
            return connections[0], operation(connections[0])
        with patch('apps.portal.views_grade_placement.exchange_with_fallback', side_effect=exchange), \
                patch('apps.portal.views_grade_placement.fetch_grade_payload', return_value=rows):
            return self.client.post(self.url(), {'action': 'preview',
                'date_from': self.year.start_date, 'date_to': self.year.end_date, **values}, follow=True)

    def apply(self, lesson=None, index=0, **values):
        preview = self.client.session[PREVIEW_SESSION_KEY]
        work = preview['batches'][0]['works'][index]
        return self.client.post(self.url(), {'action': 'apply', 'preview_token': preview['token'],
            'work_id': work['id'], 'lesson': lesson.pk if lesson else '', **values}, follow=True)

    def session_row(self, student_id=41, session=70, **values):
        return self.row(id=f'schooltest:session:{session}:student:{student_id}', student_id=student_id,
            **{'date': self.l2.date.isoformat(), **values})

    def second_mapping(self, connection=None):
        GradeStudentMapping.objects.create(connection=connection or self.connection, external_id='42', student=self.empty_student)

    def pin(self, row, lesson=None, connection=None):
        connection = connection or self.connection
        return GradeLessonAssignment.objects.create(connection=connection, work_key=work_key(connection, row),
            lesson=lesson or self.l1, selected_by=self.teacher)

    def other_journal(self):
        subject = Subject.objects.create(school=self.school, name='Інший предмет')
        return Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group,
            subject=subject, teacher=self.teacher)

    def test_get_reads_no_api_and_lists_existing_ordinary_lessons(self):
        with patch('apps.portal.views_grade_placement.fetch_grade_payload') as fetch:
            response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Обрати урок для оцінок')
        fetch.assert_not_called()
        self.assertFalse(GradeLessonAssignment.objects.exists())
        self.assertFalse(Grade.objects.exists())

    def test_preview_is_read_only_and_groups_one_session_across_completion_days(self):
        self.second_mapping()
        before = GradeStudentMapping.objects.count(), AuditLog.objects.count()
        response = self.preview([self.session_row(date=self.l1.date.isoformat()), self.session_row(student_id=42)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cards']), 1)
        self.assertEqual(response.context['cards'][0]['count'], 2)
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(APIInboxResult.objects.exists())
        self.assertFalse(GradePullLog.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())
        self.assertEqual(before, (GradeStudentMapping.objects.count(), AuditLog.objects.count()))
        self.assertContains(response, '05.09.2026')
        self.assertContains(response, '09.10.2026')

    def test_teacher_example_later_pupils_go_to_the_original_lesson(self):
        self.second_mapping()
        self.pull(self.session_row(date=self.l1.date.isoformat()))
        original = Grade.objects.get(student=self.student)
        later = self.session_row(student_id=42, value='11')
        self.preview([later])
        with patch('apps.portal.views_grade_placement.fetch_grade_payload') as fetch:
            response = self.apply(self.l1)
        fetch.assert_not_called()
        self.assertContains(response, 'Додано 1')
        self.assertEqual(Grade.objects.get(student=self.empty_student).lesson_id, self.l1.pk)
        original.refresh_from_db()
        self.assertEqual(original.lesson_id, self.l1.pk)
        self.assertEqual(Grade.objects.count(), 2)
        record = ExternalGrade.objects.get(student=self.empty_student)
        self.assertEqual(record.source_payload['date'], self.l2.date.isoformat())
        self.assertEqual(APIInboxResult.objects.get(external_id=later['id']).payload['date'], self.l2.date.isoformat())

    def test_manual_choice_overrides_automatic_session_lesson(self):
        self.second_mapping()
        self.pull(self.session_row(date=self.l1.date.isoformat()))
        self.preview([self.session_row(student_id=42)])
        self.apply(self.l2)
        self.assertEqual(Grade.objects.get(student=self.empty_student).lesson_id, self.l2.pk)
        self.assertEqual(Grade.objects.get(student=self.student).lesson_id, self.l1.pk)

    def test_lesson_may_be_outside_fetch_period_in_same_academic_year(self):
        self.preview([self.session_row()], date_from='2026-10-01', date_to='2026-10-31')
        self.apply(self.l1)
        self.assertEqual(Grade.objects.get().lesson_id, self.l1.pk)

    def test_persisted_choice_wins_for_future_automatic_sync(self):
        self.second_mapping()
        self.pull(self.session_row())  # Earlier automatic import went to October.
        self.preview([self.session_row(student_id=42)])
        self.apply(self.l1)
        third = Student.objects.create(school=self.school, first_name='Третій', last_name='Вигаданий')
        ClassEnrollment.objects.create(student=third, student_class=self.group)
        GradeStudentMapping.objects.create(connection=self.connection, external_id='43', student=third)
        log = self.pull(self.session_row(student_id=43, date='2026-10-16'))
        self.assertEqual(log.counts['errors'], 0)
        self.assertEqual(Grade.objects.get(student=third).lesson_id, self.l1.pk)
        self.assertEqual(Grade.objects.get(student=self.student).lesson_id, self.l2.pk)

    def test_only_selected_work_is_imported(self):
        self.preview([self.session_row(session=70), self.session_row(session=71)])
        self.apply(self.l1)
        self.assertEqual(ExternalGrade.objects.count(), 1)
        self.assertEqual(ExternalGrade.objects.get().external_id, self.session_row(session=70)['id'])
        self.assertEqual(len(self.client.session[PREVIEW_SESSION_KEY]['batches'][0]['works']), 1)

    def test_repeated_test_sessions_keep_independent_choices(self):
        self.second_mapping()
        first, second = self.session_row(session=70), self.session_row(student_id=42, session=71)
        self.preview([first, second])
        self.assertEqual(len(self.client.session[PREVIEW_SESSION_KEY]['batches'][0]['works']), 2)
        self.apply(self.l1)
        self.apply(self.l2)
        self.assertEqual(Grade.objects.get(student=self.student).lesson_id, self.l1.pk)
        self.assertEqual(Grade.objects.get(student=self.empty_student).lesson_id, self.l2.pk)

    def test_schoolwork_assignments_support_the_same_flow(self):
        self.connection.source = 'schoolwork'
        self.connection.save(update_fields=['source'])
        self.preview([self.row(source='schoolwork', id='schoolwork:assignment:80:student:41', date=self.l2.date.isoformat())])
        self.apply(self.l1)
        self.assertEqual(Grade.objects.get().lesson_id, self.l1.pk)

    def test_existing_manual_mark_is_a_conflict_and_is_not_overwritten(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='12', created_by=self.teacher)
        self.preview([self.session_row(value='8')])
        response = self.apply(self.l1)
        grade.refresh_from_db()
        self.assertEqual(grade.value, '12')
        self.assertEqual(ExternalGrade.objects.get().status, ExternalGrade.Status.CONFLICT)
        self.assertContains(response, 'конфлікти 1')

    def test_existing_absence_requires_teacher_review(self):
        absence = Attendance.objects.create(lesson=self.l1, student=self.student, status=Attendance.Status.ABSENT_UNEXCUSED)
        self.preview([self.session_row()])
        self.apply(self.l1)
        absence.refresh_from_db()
        self.assertEqual(absence.status, Attendance.Status.ABSENT_UNEXCUSED)
        self.assertFalse(Grade.objects.exists())
        self.assertEqual(ExternalGrade.objects.get().status, ExternalGrade.Status.CONFLICT)

    def test_repeated_import_does_not_move_previously_imported_marks(self):
        row = self.session_row()
        self.pull(row)
        original = Grade.objects.get()
        self.preview([row])
        self.apply(self.l1)
        self.assertEqual(Grade.objects.count(), 1)
        original.refresh_from_db()
        self.assertEqual(original.lesson_id, self.l2.pk)
        self.assertEqual(ExternalGrade.objects.get().lesson_id, self.l2.pk)

    def test_foreign_journal_lesson_is_rejected_without_changing_data(self):
        foreign = Lesson.objects.create(journal=self.other_journal(), date=self.l1.date, lesson_number=1)
        self.preview([self.session_row()])
        response = self.apply(foreign)
        self.assertContains(response, 'Оберіть наявний звичайний урок саме цього журналу')
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_summary_lesson_is_not_a_destination(self):
        from apps.journals.models import LessonType
        summary = Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=2, lesson_type=LessonType.THEMATIC)
        self.preview([self.session_row()])
        self.apply(summary)
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_deleted_lesson_is_rejected(self):
        self.preview([self.session_row()])
        pk = self.l1.pk
        self.l1.delete()
        preview = self.client.session[PREVIEW_SESSION_KEY]
        self.client.post(self.url(), {'action': 'apply', 'preview_token': preview['token'],
            'work_id': preview['batches'][0]['works'][0]['id'], 'lesson': str(pk)})
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_expired_preview_cannot_import_or_save_choice(self):
        self.preview([self.session_row()])
        session = self.client.session
        preview = session[PREVIEW_SESSION_KEY]
        preview['created'] = timezone.now().timestamp() - 1201
        session[PREVIEW_SESSION_KEY] = preview
        session.save()
        self.apply(self.l1)
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_changed_connection_invalidates_staged_rows(self):
        self.preview([self.session_row()])
        self.connection.api_key = 'changed-synthetic-key'
        self.connection.save(update_fields=['api_key'])
        response = self.apply(self.l1)
        self.assertContains(response, 'Налаштування підключення змінилися')
        self.assertFalse(Grade.objects.exists())
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_disabled_connection_invalidates_staged_rows(self):
        self.preview([self.session_row()])
        self.connection.enabled = False
        self.connection.save(update_fields=['enabled'])
        self.apply(self.l1)
        self.assertFalse(Grade.objects.exists())

    def test_tampered_token_and_work_id_are_rejected(self):
        self.preview([self.session_row()])
        self.apply(self.l1, preview_token='wrong')
        self.assertFalse(Grade.objects.exists())
        self.apply(self.l1, work_id='other-work')
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_no_cross_user_preview_and_no_foreign_journal_write(self):
        self.preview([self.session_row()])
        stranger = User.objects.create_user(username='placement-stranger', role=User.Role.TEACHER, school=self.school)
        preview = self.client.session[PREVIEW_SESSION_KEY]
        other = Client()
        other.force_login(stranger)
        self.assertEqual(other.get(self.url()).status_code, 403)
        self.assertEqual(other.post(self.url(), {'action': 'apply', 'preview_token': preview['token'],
            'work_id': preview['batches'][0]['works'][0]['id'], 'lesson': self.l1.pk}).status_code, 403)
        self.assertFalse(GradeLessonAssignment.objects.exists())

    def test_csrf_is_required_and_anonymous_requests_do_not_fetch(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.teacher)
        self.assertEqual(client.post(self.url(), {'action': 'preview'}).status_code, 403)
        self.client.logout()
        with patch('apps.portal.views_grade_placement.fetch_grade_payload') as fetch:
            self.assertEqual(self.client.get(self.url()).status_code, 302)
            self.assertEqual(self.client.post(self.url(), {'action': 'preview'}).status_code, 302)
            fetch.assert_not_called()

    def test_preview_replay_cannot_import_twice_or_change_destination(self):
        self.preview([self.session_row()])
        preview = self.client.session[PREVIEW_SESSION_KEY]
        work_id = preview['batches'][0]['works'][0]['id']
        self.apply(self.l1)
        response = self.client.post(self.url(), {'action': 'apply', 'preview_token': preview['token'],
            'work_id': work_id, 'lesson': self.l2.pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Grade.objects.get().lesson_id, self.l1.pk)
        self.assertEqual(GradeLessonAssignment.objects.get().lesson_id, self.l1.pk)

    def test_reset_to_automatic_removes_saved_choice(self):
        row = self.session_row()
        self.pin(row)
        self.preview([row])
        self.apply()
        self.assertFalse(GradeLessonAssignment.objects.exists())
        self.assertEqual(Grade.objects.get().lesson_id, self.l2.pk)

    def test_late_new_backup_address_uses_existing_confirmed_site_choice(self):
        self.connection.roster_cache = {'instance_id': '11111111-1111-4111-8111-111111111111'}
        self.connection.save(update_fields=['roster_cache'])
        row = self.session_row()
        self.pin(row)
        backup = GradeAPIConnection.objects.create(journal=self.journal, name='Резервна адреса', source='schooltest',
            contract_version=2, source_class_id='7', source_subject_id='3', url='http://backup.invalid/api/v2/grades/',
            roster_cache=self.connection.roster_cache)
        GradeStudentMapping.objects.create(connection=backup, external_id='41', student=self.student)
        log = import_grade_records(backup, [row], self.teacher, self.year.start_date, self.year.end_date)
        self.assertEqual(log.counts['errors'], 0)
        self.assertEqual(Grade.objects.get().lesson_id, self.l1.pk)

    def test_choice_applies_in_separate_pupil_journal(self):
        from apps.journals.services.extra_students import keep_separate
        extra = ExtraStudent.objects.create(connection=self.connection, external_key='42', name='Окремий Учень', is_separate=True)
        keep_separate(extra, self.teacher)
        extra.refresh_from_db()
        row = self.session_row(student_id=42)
        self.pin(row)
        log = self.pull(row)
        self.assertEqual(log.counts['errors'], 0)
        grade = Grade.objects.get(student=extra.separate_student)
        self.assertEqual(grade.lesson.journal_id, extra.separate_journal_id)
        self.assertEqual(grade.lesson.date, self.l1.date)

    def test_bad_period_and_oversized_preview_do_not_import(self):
        with patch('apps.portal.views_grade_placement.fetch_grade_payload') as fetch:
            response = self.client.post(self.url(), {'action': 'preview', 'date_from': '2026-08-01', 'date_to': '2026-10-01'})
            fetch.assert_not_called()
            self.assertContains(response, 'у межах навчального року')
        self.preview([self.session_row()] * 2001)
        self.assertFalse(Grade.objects.exists())
        self.assertNotIn(PREVIEW_SESSION_KEY, self.client.session)

    def test_saved_choice_is_audited_with_teacher(self):
        self.preview([self.session_row()])
        self.apply(self.l1)
        assignment = GradeLessonAssignment.objects.get()
        audit = AuditLog.objects.get(target_model='GradeLessonAssignment')
        self.assertEqual(assignment.selected_by_id, self.teacher.pk)
        self.assertEqual(audit.user_id, self.teacher.pk)
        self.assertEqual(audit.changes['lesson_id'], self.l1.pk)

    def test_backup_does_not_duplicate_an_existing_result_on_a_different_lesson(self):
        self.connection.roster_cache = {'instance_id': '11111111-1111-4111-8111-111111111111'}
        self.connection.save(update_fields=['roster_cache'])
        row = self.session_row()
        self.pull(row)
        self.pin(row)
        backup = GradeAPIConnection.objects.create(journal=self.journal, name='Резервна адреса', source='schooltest',
            contract_version=2, source_class_id='7', source_subject_id='3', url='http://backup.invalid/api/v2/grades/',
            roster_cache=self.connection.roster_cache)
        GradeStudentMapping.objects.create(connection=backup, external_id='41', student=self.student)
        log = import_grade_records(backup, [row], self.teacher, self.year.start_date, self.year.end_date)
        self.assertEqual(log.counts['errors'], 0)
        self.assertEqual(Grade.objects.count(), 1)
        self.assertEqual(Grade.objects.get().lesson_id, self.l2.pk)

    def test_another_site_or_class_does_not_inherit_the_selection(self):
        row = self.session_row()
        self.connection.roster_cache = {'instance_id': '11111111-1111-4111-8111-111111111111'}
        self.connection.save(update_fields=['roster_cache'])
        self.pin(row)
        other = GradeAPIConnection.objects.create(journal=self.journal, name='Інший сайт', source='schooltest',
            contract_version=2, source_class_id='7', source_subject_id='3', url='http://different.invalid/api/v2/grades/',
            roster_cache={'instance_id': '22222222-2222-4222-8222-222222222222'})
        GradeStudentMapping.objects.create(connection=other, external_id='41', student=self.student)
        log = import_grade_records(other, [row], self.teacher, self.year.start_date, self.year.end_date)
        self.assertEqual(log.counts['errors'], 0)
        self.assertEqual(Grade.objects.get().lesson_id, self.l2.pk)

    def test_inconsistent_saved_choices_do_not_guess(self):
        row = self.session_row()
        self.connection.roster_cache = {'instance_id': '11111111-1111-4111-8111-111111111111'}
        self.connection.save(update_fields=['roster_cache'])
        self.pin(row)
        backup = GradeAPIConnection.objects.create(journal=self.journal, name='Резервна адреса', source='schooltest',
            contract_version=2, source_class_id='7', source_subject_id='3', url='http://backup.invalid/api/v2/grades/',
            roster_cache=self.connection.roster_cache)
        self.pin(row, lesson=self.l2, connection=backup)
        log = self.pull(row)
        self.assertEqual(log.counts['errors'], 1)
        self.assertFalse(Grade.objects.exists())

    def test_deleting_a_connection_cleans_only_its_assignment(self):
        from apps.journals.services.connection_setup import safely_delete_grade_api_connection
        row = self.session_row()
        self.pin(row)
        grade = Grade.objects.create(student=self.student, lesson=self.l1, value='11')
        safely_delete_grade_api_connection(self.connection)
        self.assertFalse(GradeLessonAssignment.objects.exists())
        self.assertTrue(Grade.objects.filter(pk=grade.pk).exists())

    def test_generic_api_keeps_its_original_date_and_uses_selected_lesson(self):
        self.connection.contract_version = 1
        self.connection.source = 'generic'
        self.connection.save(update_fields=['contract_version', 'source'])
        row = {'id': 'generic:result:1', 'work_id': 'generic-work', 'student_id': self.student.pk,
            'date': self.l2.date.isoformat(), 'value': 8, 'is_ai': False, 'approved_by_teacher': True}
        self.preview([row], date_from='2026-10-01', date_to='2026-10-31')
        self.apply(self.l1)
        self.assertEqual(Grade.objects.get().lesson_id, self.l1.pk)

    def test_title_html_is_escaped_in_preview_and_messages(self):
        row = self.session_row(title='<script>throw new Error("unsafe")</script>')
        response = self.preview([row])
        self.assertNotContains(response, row['title'])
        response = self.apply(self.l1)
        self.assertNotContains(response, row['title'])
