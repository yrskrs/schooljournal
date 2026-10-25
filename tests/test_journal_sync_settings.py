import copy
import datetime as dt
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from bs4 import BeautifulSoup
from apps.journals.models import Grade, GradeStudentMapping, GradeLessonAssignment, Lesson, ExternalGrade
from apps.journals.services.external_grades import import_grade_records
from apps.journals.services.grade_placement import work_key
from tests import test_school_api_v2 as fixtures


class JournalSyncSettingsTests(TestCase):
    setUp = fixtures.SchoolAPIV2Tests.setUp
    row = fixtures.SchoolAPIV2Tests.row
    pull = fixtures.SchoolAPIV2Tests.pull

    def settings(self, **changes):
        prefix = f'connection-{self.connection.pk}-'
        return self.client.post(reverse('portal:journal_sync_settings', args=[self.journal.pk, self.connection.pk]),
            {prefix+k: v for k,v in {'enabled': 'on', 'default_lesson': self.l2.pk,
                'default_period_days': 14, **changes}.items()})

    def test_defaults_are_saved_without_reading_sources_or_changing_existing_grade(self):
        self.pull(self.row())
        with patch('requests.Session', side_effect=AssertionError('Settings never contact sources')):
            response = self.settings()
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.default_lesson, self.l2)
        self.assertEqual(self.connection.default_period_days, 14)
        self.assertEqual(Grade.objects.get().lesson, self.l1)

    def test_foreign_or_summary_lesson_and_invalid_period_are_rejected(self):
        from apps.journals.models import Journal, LessonType
        from apps.curriculum.models import Subject
        subject = Subject.objects.create(school=self.school, name='Другий предмет')
        journal = Journal.objects.create(school=self.school, academic_year=self.year, teacher=self.teacher,
            student_class=self.group, subject=subject)
        foreign = Lesson.objects.create(journal=journal, date=self.l1.date, lesson_number=1)
        for changes in ({'default_lesson': foreign.pk}, {'default_period_days': 0}, {'default_period_days': 367}):
            self.settings(**changes)
            self.connection.refresh_from_db()
            self.assertIsNone(self.connection.default_lesson_id)
            self.assertEqual(self.connection.default_period_days, 30)
        self.l3.lesson_type = LessonType.SEMESTER_1; self.l3.save()
        self.settings(default_lesson=self.l3.pk)
        self.connection.refresh_from_db(); self.assertIsNone(self.connection.default_lesson_id)

    def test_default_places_a_new_work_but_never_moves_imported_work_or_late_pupil(self):
        self.connection.default_lesson = self.l2; self.connection.save()
        self.pull(self.row())
        self.assertEqual(Grade.objects.get().lesson, self.l2)
        self.connection.default_lesson = self.l3; self.connection.save()
        self.pull(self.row(value='10', updated_at='2026-10-11T12:00:00+03:00'))
        self.assertEqual(Grade.objects.get().lesson, self.l2)
        GradeStudentMapping.objects.create(connection=self.connection, external_id='42', student=self.empty_student)
        self.pull(self.row(id='second-pupil', student_id=42, attempt_id=2))
        self.assertEqual(Grade.objects.get(student=self.empty_student).lesson, self.l2)

    def test_work_specific_placement_wins_over_default(self):
        self.connection.default_lesson = self.l2; self.connection.save()
        row = self.row()
        GradeLessonAssignment.objects.create(connection=self.connection, work_key=work_key(self.connection, row),
            lesson=self.l1, selected_by=self.teacher)
        self.pull(row)
        self.assertEqual(Grade.objects.get().lesson, self.l1)

    def test_default_cannot_overwrite_manual_or_teacher_reviewed_grade(self):
        self.connection.default_lesson = self.l2; self.connection.save()
        Grade.objects.create(lesson=self.l2, student=self.student, value='12', created_by=self.teacher)
        self.assertEqual(self.pull(self.row()).counts['conflicts'], 1)
        self.assertEqual(Grade.objects.get().value, '12')

    def test_journal_has_one_sync_block_separate_forms_and_no_credential_fields(self):
        with patch('apps.journals.services.journal_sync.timezone.localdate', return_value=dt.date(2026, 10, 24)):
            response = self.client.get(reverse('portal:journal_detail', args=[self.journal.pk]))
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(len(soup.select('#journal-sync')), 1)
        panel = soup.select_one('[data-sync-source=schooltest]')
        self.assertEqual(panel.select_one('[name=date_from]')['value'], '2026-09-25')
        self.assertEqual(panel.select_one('[name=date_to]')['value'], '2026-10-24')
        self.assertEqual(len(panel.select('form[data-sync-operation=grades]')), 1)
        self.assertFalse(soup.select('input[name=api_key],input[name=url],input[name=site_url]'))
        self.assertContains(response, '📥 Забрати оцінки')
        self.assertContains(response, 'SchoolNet · Завдання')
        details = soup.select_one('#journalSyncDetails')
        self.assertNotIn('open', details.attrs)
        quick = soup.select_one('.journal-sync-quick')
        form = quick.select_one('form[data-sync-operation=grades]')
        self.assertIsNotNone(form)
        self.assertEqual(form.select_one('[name=connection_id]')['value'], str(self.connection.pk))
        self.assertFalse(quick.select('input[type=date]'))
        self.assertEqual(len({n['id'] for n in soup.select('[id]')}), len(soup.select('[id]')))

