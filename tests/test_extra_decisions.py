"""Actual inbox decisions, source IDs and destructive-action boundaries."""
from django.test import TestCase, Client
from django.urls import reverse
from apps.accounts.models import User
from apps.curriculum.models import Student, StudentClass, ClassEnrollment
from apps.journals.models import (
    Journal, ExtraStudent, APIInboxResult, Grade, Lesson, AuditLog, DeletedAPIIdentity,
    GradeStudentMapping, Attendance, SummaryGrade, SummaryGradeType, ExternalGrade
)
from apps.journals.services.api_inbox import keep_roster_extras, resolve_student
from tests import test_simple_exchange as exchange


class ExtraDecisionTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row
    pull = exchange.SimpleExchangeTests.pull

    def url(self, query=''):
        return reverse('portal:api_inbox', args=[self.journal.pk]) + query

    def decision(self, extra, action='match', **values):
        return self.client.post(self.url(values.pop('query', '')), {
            'action': action, 'extra_id': extra.pk, 'version': extra.updated_at.isoformat(), **values}, follow=True)

    def extra(self, **values):
        values.setdefault('external_key', '99')
        values.setdefault('name', 'Вигаданий Профіль')
        return ExtraStudent.objects.create(connection=self.connection(), **values)

    def legacy_separate(self, extra):
        # Reproduce the previous schema behavior to check existing grade history.
        group = StudentClass.objects.create(school=self.school, academic_year=self.year,
            name=f'{self.group.name} (Позакласні)', grade_level=self.group.grade_level, letter='ПК')
        journal = Journal.objects.create(school=self.school, academic_year=self.year,
            student_class=group, subject=self.subject, teacher=self.teacher)
        student = Student.objects.create(school=self.school, last_name='Вигаданий', first_name='Учень')
        ClassEnrollment.objects.create(student=student, student_class=group)
        extra.separate_student, extra.separate_journal = student, journal
        extra.save()

    def test_placeholder_requires_choice_without_creating_another_journal(self):
        extra = self.extra()
        response = self.decision(extra, student='__choose__')
        self.assertContains(response, 'Оберіть учня цього журналу')
        extra.refresh_from_db()
        self.assertIsNone(extra.matched_student_id)
        self.assertIsNone(extra.separate_student_id)

    def test_confirmation_is_visible_and_changes_effective_mapping_without_grades(self):
        connection = self.connection(mappings=True)
        twin = Student.objects.create(school=self.school, last_name='Інший', first_name='Учень')
        ClassEnrollment.objects.create(student=twin, student_class=self.group)
        extra = ExtraStudent.objects.create(connection=connection, external_key='41', name='Введено з помилкою', matched_student=self.student)
        response = self.decision(extra, student=twin.pk, query='?all=1')
        self.assertEqual(response.redirect_chain[-1][0], self.url('?all=1'))
        self.assertContains(response, 'Підтверджено: Інший Учень')
        self.assertEqual(resolve_student(connection, self.row(student_id=41, student_key='41')), twin.pk)
        self.assertFalse(Grade.objects.exists())
        self.assertTrue(AuditLog.objects.filter(target_model='ExtraStudent').exists())
        self.assertEqual(list(self.client.get(self.url()).context['extra_page']), [])

    def test_imported_history_cannot_be_reassigned(self):
        connection = self.connection(mappings=True)
        self.pull(connection, self.row(student_id=41, student_key='41'))
        extra = ExtraStudent.objects.create(connection=connection, external_key='41', name='Вигаданий профіль', matched_student=self.student)
        other = Student.objects.create(school=self.school, last_name='Інший', first_name='Учень')
        ClassEnrollment.objects.create(student=other, student_class=self.group)
        self.assertContains(self.decision(extra, student=other.pk), 'Переприв’язування заборонено')
        self.assertEqual(Grade.objects.get().student_id, self.student.pk)

    def test_separate_roster_only_identity_leaves_queue_and_stays_in_all_view(self):
        extra = self.extra()
        response = self.decision(extra, action='keep_separate')
        extra.refresh_from_db()
        self.assertTrue(extra.is_separate)
        self.assertIsNotNone(extra.separate_student_id)
        self.assertIsNotNone(extra.separate_journal_id)
        self.assertEqual(list(response.context['extra_page']), [extra])
        self.assertContains(self.client.get(self.url('?all=1')), 'Залишено позакласним')
        original_id = extra.separate_student_id
        self.decision(extra, action='keep_separate')
        extra.refresh_from_db()
        self.assertEqual(extra.separate_student_id, original_id)
        self.assertFalse(Grade.objects.exists())

    def test_separate_same_names_are_distinct_ids_and_grades_require_explicit_action(self):
        connection = self.connection()
        self.pull(connection, self.row())
        extra = ExtraStudent.objects.get()
        ordinary = Student.objects.create(school=self.school, last_name='Вигаданий', first_name='У.')
        self.legacy_separate(extra)
        extra.refresh_from_db()
        self.assertNotEqual(extra.separate_student_id, ordinary.pk)
        self.assertFalse(Grade.objects.exists())
        lesson = Lesson.objects.create(journal=extra.separate_journal, date=self.l1.date, lesson_number=1)
        result = APIInboxResult.objects.get()
        self.client.post(self.url(), {'action': 'apply_saved', 'result_id': result.pk, 'version': result.updated_at.isoformat()})
        self.assertEqual((Grade.objects.get().student_id, Grade.objects.get().lesson_id), (extra.separate_student_id, lesson.pk))

    def test_delete_removes_all_saved_payloads_and_prevents_roster_and_grade_recreation(self):
        connection = self.connection()
        row = self.row(student_id=41, student_key='41')
        self.pull(connection, row)
        extra = ExtraStudent.objects.get()
        self.assertContains(self.decision(extra, action='delete_extra', confirm_delete='on'), 'не завантажиться повторно')
        self.assertFalse(ExtraStudent.objects.exists())
        self.assertFalse(APIInboxResult.objects.exists())
        self.assertTrue(DeletedAPIIdentity.objects.filter(connection=connection, external_key='41').exists())
        keep_roster_extras(connection)
        counts = self.pull(connection, dict(row, value='11', updated_at='2026-10-11T11:00:00+03:00')).counts
        self.assertEqual((counts['ignored'], counts['errors']), (1, 0))
        self.assertFalse(ExtraStudent.objects.exists())
        self.assertFalse(APIInboxResult.objects.exists())
        for query in ('', '?all=1'):
            self.assertEqual(list(self.client.get(self.url(query)).context['extra_page']), [])
        audit = AuditLog.objects.get(target_model='ExtraStudent', action='DELETE')
        self.assertEqual(audit.user, self.teacher)
        self.assertEqual(audit.changes['results_deleted'], 1)

    def test_delete_ordinary_mapped_identity_preserves_class_pupil_and_grade(self):
        connection = self.connection(mappings=True)
        row = self.row(student_id=41, student_key='41')
        self.pull(connection, row)
        extra = ExtraStudent.objects.create(connection=connection, external_key='41', name='Підтверджений псевдонім', matched_student=self.student)
        original = Grade.objects.get()
        self.decision(extra, action='delete_extra', confirm_delete='on')
        self.assertTrue(Student.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(Grade.objects.get(pk=original.pk).value, original.value)
        self.assertEqual(self.pull(connection, row).counts['ignored'], 1)

    def test_delete_separate_identity_removes_its_own_pupil_and_marks(self):
        extra = self.extra()
        self.legacy_separate(extra)
        extra.refresh_from_db()
        pk = extra.separate_student_id
        lesson = Lesson.objects.create(journal=extra.separate_journal, date=self.l1.date, lesson_number=1)
        Grade.objects.create(student=extra.separate_student, lesson=lesson, value='8')
        self.decision(extra, action='delete_extra', confirm_delete='on')
        self.assertFalse(Student.objects.filter(pk=pk).exists())
        self.assertFalse(Grade.objects.exists())
        self.assertTrue(Student.objects.filter(pk=self.student.pk).exists())

    def test_delete_requires_confirmation_current_revision_permission_and_csrf(self):
        extra = self.extra()
        self.assertContains(self.decision(extra, action='delete_extra'), 'Підтвердьте видалення')
        extra.refresh_from_db()
        old_version = extra.updated_at.isoformat()
        extra.name = 'Оновлена назва'; extra.save()
        response = self.client.post(self.url(), {'action': 'delete_extra', 'extra_id': extra.pk, 'version': old_version, 'confirm_delete': 'on'}, follow=True)
        self.assertContains(response, 'Запис оновився після перегляду')
        stranger = User.objects.create_user(username='stranger-delete', school=self.school, role=User.Role.TEACHER)
        self.client.force_login(stranger)
        self.assertEqual(self.decision(extra, action='delete_extra', confirm_delete='on').status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True); csrf_client.force_login(self.teacher)
        self.assertEqual(csrf_client.post(self.url(), {'action': 'delete_extra'}).status_code, 403)
        self.assertTrue(ExtraStudent.objects.filter(pk=extra.pk).exists())

    def test_deletion_is_scoped_to_connection_not_same_name_or_other_source(self):
        extra = self.extra()
        other = self.extra()
        self.decision(extra, action='delete_extra', confirm_delete='on')
        self.assertTrue(ExtraStudent.objects.filter(pk=other.pk).exists())
        self.assertFalse(other.connection.deleted_identities.exists())

    def test_number_menu_deletes_new_separate_profile_and_source_identity(self):
        extra = self.extra()
        self.legacy_separate(extra)
        extra.refresh_from_db()
        student = extra.separate_student
        detail = reverse('portal:journal_detail', args=[extra.separate_journal_id])
        self.assertRedirects(self.client.get(detail), reverse('portal:outside_students') + '?journal=' + str(self.journal.pk))
        url = reverse('portal:delete_extracurricular_student', args=[extra.separate_journal_id, student.pk])
        response = self.client.post(url, {'confirm_delete':'on', 'version':student.updated_at.isoformat()}, follow=True)
        self.assertContains(response, 'локальні записи видалено')
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())
        self.assertTrue(extra.connection.deleted_identities.filter(external_key='99').exists())

    def test_old_separate_table_profile_can_be_deleted_by_id_without_name_linking(self):
        extra = self.extra()
        self.legacy_separate(extra)
        extra.refresh_from_db()
        student, separate = extra.separate_student, extra.separate_journal
        # Old releases created these rows without any stable link from the inbox.
        extra.separate_student = None; extra.separate_journal = None; extra.save()
        lesson = Lesson.objects.create(journal=separate, date=self.l1.date, lesson_number=1)
        Grade.objects.create(student=student, lesson=lesson, value='9')
        self.assertContains(self.client.get(reverse('portal:journal_detail', args=[separate.pk])), 'Видалити позакласного учня та оцінки')
        url = reverse('portal:delete_extracurricular_student', args=[separate.pk, student.pk])
        self.client.post(url, {'confirm_delete':'on','version':student.updated_at.isoformat()})
        self.assertFalse(Student.objects.filter(pk=student.pk).exists())
        self.assertFalse(Grade.objects.exists())
        self.assertTrue(ExtraStudent.objects.filter(pk=extra.pk).exists())
        self.assertFalse(extra.connection.deleted_identities.exists())

    def test_separate_deletion_endpoint_cannot_remove_an_ordinary_classmate(self):
        url = reverse('portal:delete_extracurricular_student', args=[self.journal.pk, self.student.pk])
        response = self.client.post(url, {'confirm_delete':'on', 'version':self.student.updated_at.isoformat()}, follow=True)
        self.assertContains(response, 'Це не окремий позакласний журнал')
        self.assertTrue(Student.objects.filter(pk=self.student.pk).exists())
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_other_class_students_selectable_and_matchable(self):
        other_class = StudentClass.objects.create(school=self.school, academic_year=self.year, name='8-Б', grade_level=8, letter='Б')
        other_student = Student.objects.create(school=self.school, last_name='Шевченко', first_name='Тарас')
        ClassEnrollment.objects.create(student=other_student, student_class=other_class, is_active=True)
        extra = self.extra(name='Шевченко Тарас')

        # Verify other-class student appears in dropdown with their class name
        resp = self.client.get(self.url('?all=1'))
        self.assertContains(resp, 'Шевченко Тарас (8-Б)')

        # Match to the other-class student
        match_resp = self.decision(extra, student=other_student.pk)
        self.assertContains(match_resp, 'Шевченко Тарас (8-Б)')
        extra.refresh_from_db()
        self.assertEqual(extra.matched_student_id, other_student.pk)

    def test_mismatched_connection_warning_displayed(self):
        conn = self.connection()
        conn.source_class_id = '999'
        conn.roster_cache = {
            'classes': [{'id': '999', 'name': '8-Б'}],
            'subjects': [{'id': '10', 'name': 'Алгебра'}],
            'students': [],
        }
        conn.save()

        # Check in api_inbox
        inbox_resp = self.client.get(self.url())
        self.assertContains(inbox_resp, 'підключено інший клас')
        self.assertContains(inbox_resp, '8-Б')

        # Check in connect_source
        connect_url = reverse('portal:connect_source', args=[self.journal.pk]) + f'?connection={conn.pk}'
        connect_resp = self.client.get(connect_url)
        self.assertContains(connect_resp, 'Увага: невідповідність класу!')

        # Check in journal_detail
        detail_url = reverse('portal:journal_detail', args=[self.journal.pk])
        detail_resp = self.client.get(detail_url)
        self.assertContains(detail_resp, 'підключено інший клас із джерела')

    def test_auto_match_suggestion_and_batch_match(self):
        extra = self.extra(external_key='101', name=self.student.last_name)
        resp = self.client.get(self.url())
        self.assertContains(resp, 'Знайдено збіг')
        self.assertContains(resp, '★ (підібрано)')
        self.assertContains(resp, 'Підтвердити всі підібрані')

        batch_resp = self.client.post(self.url(), {'action': 'batch_match'}, follow=True)
        self.assertContains(batch_resp, 'Успішно підтверджено 1 учнів за прізвищами')
        extra.refresh_from_db()
        self.assertEqual(extra.matched_student_id, self.student.pk)
        self.assertTrue(GradeStudentMapping.objects.filter(connection=extra.connection, external_id='101', student=self.student).exists())

    def test_match_creates_grade_student_mapping_and_unmatch_clears_it(self):
        extra = self.extra(external_key='102', name=f'{self.student.last_name} {self.student.first_name}')
        self.decision(extra, student=self.student.pk)
        extra.refresh_from_db()
        self.assertEqual(extra.matched_student_id, self.student.pk)
        self.assertTrue(GradeStudentMapping.objects.filter(connection=extra.connection, external_id='102', student=self.student).exists())

        unmatch_resp = self.decision(extra, action='unmatch')
        self.assertContains(unmatch_resp, 'скасовано')
        extra.refresh_from_db()
        self.assertIsNone(extra.matched_student_id)
        self.assertTrue(extra.is_separate)
        self.assertFalse(GradeStudentMapping.objects.filter(connection=extra.connection, external_id='102').exists())

    def test_merge_journal_students(self):
        dup_student = Student.objects.create(school=self.school, last_name=self.student.last_name, first_name='Дублікат')
        ClassEnrollment.objects.create(student=dup_student, student_class=self.group)
        Grade.objects.create(student=dup_student, lesson=self.l1, value='11')
        Attendance.objects.create(student=dup_student, lesson=self.l1, status=Attendance.Status.ABSENT_EXCUSED)
        SummaryGrade.objects.create(student=dup_student, journal=self.journal, grade_type=SummaryGradeType.THEMATIC, thematic_name='Тема 1', value='10')

        url = reverse('portal:merge_journal_student', args=[self.journal.pk, dup_student.pk])
        resp = self.client.post(url, {'target_student_id': str(self.student.pk)}, follow=True)
        self.assertContains(resp, 'успішно об’єднано')

        self.assertEqual(Grade.objects.filter(student=self.student, lesson=self.l1).first().value, '11')
        self.assertEqual(Attendance.objects.filter(student=self.student, lesson=self.l1).first().status, Attendance.Status.ABSENT_EXCUSED)
        self.assertEqual(SummaryGrade.objects.filter(student=self.student, journal=self.journal).first().value, '10')
        self.assertFalse(Student.objects.filter(pk=dup_student.pk).exists())

    def test_delete_journal_student(self):
        st = Student.objects.create(school=self.school, last_name='Тестовий', first_name='Учень')
        ClassEnrollment.objects.create(student=st, student_class=self.group)
        Grade.objects.create(student=st, lesson=self.l1, value='7')

        url = reverse('portal:delete_journal_student', args=[self.journal.pk, st.pk])
        resp = self.client.post(url, follow=True)
        self.assertContains(resp, 'вилучено з цього журналу')
        self.assertFalse(Grade.objects.filter(student=st).exists())
        self.assertFalse(ClassEnrollment.objects.filter(student=st).exists())
        self.assertFalse(Student.objects.filter(pk=st.pk).exists())
