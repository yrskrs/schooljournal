import datetime as dt
from unittest.mock import patch, MagicMock

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, Semester, StudentClass, Subject, Student, ClassEnrollment, LessonTime
from apps.journals.models import Journal, Lesson, LessonPlanTopic, Grade, Attendance, AuditLog, SummaryGrade, LessonType
from apps.journals.services.assessment import calculate
from apps.journals.services.nz_service import NZService, NZServiceError


class JournalWorkflowsTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Вигадана школа для перевірки')
        self.teacher = User.objects.create_user(username='synthetic_teacher', password='synthetic-test', role=User.Role.TEACHER, school=self.school)
        self.year = AcademicYear.objects.create(school=self.school, name='2026/2027', start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 6, 30))
        self.sem1 = Semester.objects.create(academic_year=self.year, number=1, start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 12, 31))
        self.sem2 = Semester.objects.create(academic_year=self.year, number=2, start_date=dt.date(2027, 1, 1), end_date=dt.date(2027, 6, 30))
        self.group = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Тестовий клас')
        self.subject = Subject.objects.create(school=self.school, name='Тестовий предмет')
        self.journal = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group, subject=self.subject, teacher=self.teacher, nz_journal_id='synthetic-journal')
        self.student = Student.objects.create(school=self.school, first_name='Учень', last_name='Вигаданий', nz_student_id='synthetic-student')
        self.empty_student = Student.objects.create(school=self.school, first_name='Учениця', last_name='Безоцінок')
        for student in (self.student, self.empty_student):
            ClassEnrollment.objects.create(student=student, student_class=self.group, is_active=True)
        self.l1 = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 5), topic='Тема A', lesson_number=1, nz_schedule_id='synthetic-a', is_synced_to_nz=True, nz_last_synced_topic='Тема A')
        self.l2 = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 9), topic='Тема B', lesson_number=1, nz_schedule_id='synthetic-b', is_synced_to_nz=True, nz_last_synced_topic='Тема B')
        self.l3 = Lesson.objects.create(journal=self.journal, date=dt.date(2027, 2, 1), topic='Тема C', lesson_number=1)
        self.client.force_login(self.teacher)

    def url(self, name, **kwargs):
        return reverse('portal:' + name, kwargs={'journal_id': self.journal.pk, **kwargs})

    def lesson_post(self, **kwargs):
        return {'date': '2026-10-09', 'topic': 'Нова тема', 'lesson_type': 'ORDINARY', **kwargs}

    def assessment_post(self, **kwargs):
        return {'grade_type': 'THEMATIC', 'date': '2026-10-09', 'start_date': '2026-09-01', 'end_date': '2026-10-09',
                'source': 'current', 'rounding': 'nearest', 'lesson_ids': [str(self.l1.pk), str(self.l2.pk)], 'action': 'preview', **kwargs}

    def test_all_year_default_with_month_groups_and_no_permanent_summaries(self):
        response = self.client.get(self.url('journal_detail'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([l.pk for l in response.context['lessons']], [self.l1.pk, self.l2.pk, self.l3.pk])
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual([th.text.strip() for th in soup.select('.month-group-th')], ['Вересень 2026', 'Жовтень 2026', 'Лютий 2027'])
        self.assertEqual(len(soup.select('#journal-lesson-data')), 1)
        self.assertEqual(response.context['legacy_summary_columns'], [])
        self.assertNotIn('Річна', [th.text.strip() for th in soup.select('.journal-table thead th')])

    def test_month_and_semester_filters(self):
        response = self.client.get(self.url('journal_detail'), {'semester_id': 'all', 'month': '2027-02'})
        self.assertEqual([l.pk for l in response.context['lessons']], [self.l3.pk])
        response = self.client.get(self.url('journal_detail'), {'semester_id': self.sem1.pk})
        self.assertEqual([l.pk for l in response.context['lessons']], [self.l1.pk, self.l2.pk])
        self.assertEqual(len(response.context['month_tabs']), 2)

    def test_invalid_month_falls_back_to_all(self):
        response = self.client.get(self.url('journal_detail'), {'month': '2026-99'})
        self.assertEqual(len(response.context['lessons']), 3)

    def test_existing_summary_is_preserved_and_only_existing_column_visible(self):
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='SEMESTER_1', value='9')
        response = self.client.get(self.url('journal_detail'))
        self.assertEqual(response.context['legacy_summary_columns'], [('SEMESTER_1', 'I Семестр')])
        self.assertContains(response, 'value="9"')

    def test_online_local_and_cleared_marks(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        response = self.client.get(self.url('journal_detail'))
        self.assertContains(response, 'sync-online')
        response = self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': self.student.pk, 'value': '9'})
        self.assertContains(response, 'sync-local')
        response = self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': self.student.pk, 'value': ''})
        self.assertContains(response, 'sync-local')
        grade.refresh_from_db()
        self.assertEqual(grade.nz_last_synced_value, '8')
        self.assertEqual(grade.value, '')

    def test_noop_grade_save_keeps_online_confirmation(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': self.student.pk, 'value': '8'})
        grade.refresh_from_db()
        self.assertTrue(grade.is_synced_to_nz)

    def test_new_records_are_local_by_default(self):
        self.assertFalse(self.l3.is_synced_to_nz)
        self.assertFalse(Grade.objects.create(lesson=self.l3, student=self.student, value='7').is_synced_to_nz)
        self.assertFalse(Attendance.objects.create(lesson=self.l3, student=self.student).is_synced_to_nz)

    def test_automatic_number_is_next_on_selected_date(self):
        self.client.post(self.url('lesson_create'), self.lesson_post())
        new = self.journal.lessons.get(topic='Нова тема')
        self.assertEqual(new.lesson_number, 2)
        self.assertEqual(new.semester, self.sem1)

    def test_invalid_number_has_no_partial_writes(self):
        before = self.journal.lessons.count()
        response = self.client.post(self.url('lesson_create'), self.lesson_post(lesson_number='incorrect'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.journal.lessons.count(), before)

    def test_weekly_series_copies_fields_locally_without_marks_or_network(self):
        Grade.objects.create(lesson=self.l2, student=self.student, value='10')
        Attendance.objects.create(lesson=self.l2, student=self.empty_student, status='ABSENT_EXCUSED')
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network request')) as network:
            response = self.client.post(self.url('lesson_create'), self.lesson_post(
                repeat_frequency='weekly', repeat_end='count', repeat_count='4',
                lesson_number='7', lesson_time='10:30', lesson_type='PRACTICAL',
                homework='Вигадане завдання', notes='Вигадана примітка'))
        network.assert_not_called()
        self.assertEqual(response.status_code, 302)
        lessons = list(self.journal.lessons.filter(topic='Нова тема'))
        self.assertEqual([l.date for l in lessons], [dt.date(2026, 10, day) for day in (9, 16, 23, 30)])
        self.assertEqual([l.lesson_number for l in lessons], [7, 1, 1, 1])
        for lesson in lessons:
            self.assertEqual((lesson.lesson_time, lesson.lesson_type, lesson.homework, lesson.notes),
                (dt.time(10, 30), 'PRACTICAL', 'Вигадане завдання', 'Вигадана примітка'))
            self.assertEqual(lesson.created_by_id, self.teacher.pk)
            self.assertFalse(lesson.is_synced_to_nz)
            self.assertEqual(lesson.nz_schedule_id, '')
        self.assertEqual(Grade.objects.count(), 1)
        self.assertEqual(Attendance.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(target_model='Lesson').count(), 4)

    def test_biweekly_series_stops_at_inclusive_end_date(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='biweekly', repeat_end='date', repeat_until='2026-11-07'))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('date', flat=True)),
            [dt.date(2026, 10, 9), dt.date(2026, 10, 23), dt.date(2026, 11, 6)])

    def test_custom_series_interval(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='custom', repeat_interval_weeks='3', repeat_end='count', repeat_count='3'))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('date', flat=True)),
            [dt.date(2026, 10, 9), dt.date(2026, 10, 30), dt.date(2026, 11, 20)])

    def test_series_end_on_start_date_creates_only_first_lesson(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='date', repeat_until='2026-10-09'))
        self.assertEqual(self.journal.lessons.filter(topic='Нова тема').count(), 1)

    def test_series_assigns_semester_to_each_occurrence(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            date='2026-12-25', repeat_frequency='weekly', repeat_end='count', repeat_count='2'))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('date', 'semester_id')),
            [(dt.date(2026, 12, 25), self.sem1.pk), (dt.date(2027, 1, 1), self.sem2.pk)])

    def test_series_insertion_and_occupied_days_preserve_existing_records(self):
        occupied = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 16),
            topic='Зайнятий день', lesson_number=2, nz_schedule_id='synthetic-occupied', is_synced_to_nz=True)
        grade = Grade.objects.create(lesson=occupied, student=self.student, value='9', is_synced_to_nz=True)
        att = Attendance.objects.create(lesson=self.l2, student=self.empty_student, status='ABSENT_EXCUSED')
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='2', insert_before_id=self.l2.pk))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('lesson_number', flat=True)), [1, 3])
        self.l2.refresh_from_db(); occupied.refresh_from_db(); grade.refresh_from_db(); att.refresh_from_db()
        self.assertEqual((self.l2.lesson_number, self.l2.nz_schedule_id), (2, 'synthetic-b'))
        self.assertEqual((occupied.lesson_number, occupied.nz_schedule_id, occupied.is_synced_to_nz), (2, 'synthetic-occupied', True))
        self.assertEqual((grade.lesson_id, grade.value, grade.is_synced_to_nz), (occupied.pk, '9', True))
        self.assertEqual(att.lesson_id, self.l2.pk)

    def test_series_homework_date_shifts_and_can_target_new_series_day(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='4',
            homework='Завдання серії', homework_target='date:2026-10-16'))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('homework_due_date', flat=True)),
            [dt.date(2026, 10, 16), dt.date(2026, 10, 23), dt.date(2026, 10, 30), dt.date(2026, 11, 6)])
        for lesson in self.journal.lessons.filter(topic='Нова тема'):
            self.assertIsNone(lesson.homework_due_lesson_id)
            self.assertFalse(lesson.homework_for_next_lesson)

    def test_series_automatic_homework_resolves_separately(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='3', homework_target='next'))
        first, second, third = self.journal.lessons.filter(topic='Нова тема')
        self.assertTrue(all(l.homework_for_next_lesson for l in (first, second, third)))
        self.assertEqual(first.get_homework_due_lesson(), second)
        self.assertEqual(second.get_homework_due_lesson(), third)
        self.assertEqual(third.get_homework_due_lesson(), self.l3)

    def test_series_preserves_undated_homework_plan_choice(self):
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Майбутня тема')
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='2', homework_target=f'plan:{plan.pk}'))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('homework_due_plan_topic_id', flat=True)), [plan.pk, plan.pk])
        plan.refresh_from_db()
        self.assertFalse(plan.is_conducted)

    def test_series_uses_calendar_source_only_for_first_occurrence(self):
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Джерело теми')
        other = LessonPlanTopic.objects.create(journal=self.journal, topic_number=2, title='Наступна тема')
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='3', plan_topic_id=plan.pk))
        self.assertEqual(list(self.journal.lessons.filter(topic='Нова тема').values_list('linked_plan_topic_id', flat=True)), [plan.pk, None, None])
        plan.refresh_from_db(); other.refresh_from_db()
        self.assertTrue(plan.is_conducted)
        self.assertEqual(plan.planned_date, dt.date(2026, 10, 9))
        self.assertFalse(other.is_conducted)

    def test_invalid_series_inputs_leave_no_writes(self):
        invalid = [
            {'repeat_count': '1'}, {'repeat_count': '101'}, {'repeat_count': ''},
            {'repeat_frequency': 'unknown'}, {'repeat_frequency': 'custom'},
            {'repeat_frequency': 'custom', 'repeat_interval_weeks': '0'},
            {'repeat_frequency': 'custom', 'repeat_interval_weeks': '53'},
            {'repeat_end': 'date', 'repeat_until': ''},
            {'repeat_end': 'date', 'repeat_until': '2026-10-08'},
            {'repeat_end': 'date', 'repeat_until': '2027-07-01'},
            {'date': '2026-08-28'}, {'date': '2027-06-25', 'repeat_count': '2'},
        ]
        before = self.journal.lessons.count()
        for values in invalid:
            with self.subTest(values=values):
                self.client.post(self.url('lesson_create'), self.lesson_post(**{
                    'repeat_frequency': 'weekly', 'repeat_end': 'count', 'repeat_count': '3', **values}))
                self.assertEqual(self.journal.lessons.count(), before)
                self.assertFalse(AuditLog.objects.exists())

    def test_series_failure_rolls_back_all_occurrences_and_insertion(self):
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 16), topic='Повна нумерація', lesson_number=32767)
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Не проведено')
        before = self.journal.lessons.count()
        self.client.post(self.url('lesson_create'), self.lesson_post(
            repeat_frequency='weekly', repeat_end='count', repeat_count='3',
            insert_before_id=self.l2.pk, plan_topic_id=plan.pk))
        self.l2.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(self.journal.lessons.count(), before)
        self.assertEqual(self.l2.lesson_number, 1)
        self.assertFalse(plan.is_conducted)
        self.assertFalse(AuditLog.objects.exists())

    def test_series_controls_have_year_bounds_and_single_lesson_default(self):
        soup = BeautifulSoup(self.client.get(self.url('journal_detail')).content, 'html.parser')
        form = soup.select_one('#createLessonForm')
        self.assertEqual(form['data-year-start'], '2026-09-01')
        self.assertEqual(form['data-year-end'], '2027-06-30')
        self.assertEqual([o['value'] for o in form.select('#lesson_repeat_frequency option')], ['none', 'weekly', 'biweekly', 'custom'])
        self.assertTrue(form.select_one('#lessonRepeatOptions').has_attr('hidden'))
        self.assertTrue(form.select_one('#lesson_repeat_count').has_attr('disabled'))
        self.assertEqual(form.select_one('#lessonCreateSubmit').text.strip(), 'Зберегти урок')

    def test_insert_before_preserves_marks_attendance_and_online_ids(self):
        grade = Grade.objects.create(lesson=self.l2, student=self.student, value='10')
        att = Attendance.objects.create(lesson=self.l2, student=self.empty_student, status='ABSENT_EXCUSED')
        self.client.post(self.url('lesson_create'), self.lesson_post(insert_before_id=self.l2.pk))
        self.l2.refresh_from_db(); grade.refresh_from_db(); att.refresh_from_db()
        self.assertEqual(self.l2.lesson_number, 2)
        self.assertEqual(self.l2.nz_schedule_id, 'synthetic-b')
        self.assertEqual(grade.lesson_id, self.l2.pk)
        self.assertEqual(att.lesson_id, self.l2.pk)
        self.assertEqual(self.journal.lessons.get(topic='Нова тема').lesson_number, 1)

    def test_failed_insert_rolls_back_shift(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(insert_before_id=self.l2.pk, homework_target='lesson:999999'))
        self.l2.refresh_from_db()
        self.assertEqual(self.l2.lesson_number, 1)
        self.assertFalse(self.journal.lessons.filter(topic='Нова тема').exists())

    def test_swap_topics_keeps_marks_and_dates(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        self.client.post(self.url('lesson_swap'), {'first_id': self.l1.pk, 'second_id': self.l2.pk})
        self.l1.refresh_from_db(); self.l2.refresh_from_db(); grade.refresh_from_db()
        self.assertEqual(self.l1.topic, 'Тема B')
        self.assertEqual(self.l2.topic, 'Тема A')
        self.assertEqual(grade.lesson_id, self.l1.pk)
        self.assertEqual(self.l1.nz_schedule_id, 'synthetic-a')
        self.assertEqual(self.l1.date, dt.date(2026, 9, 5))
        self.assertFalse(self.l1.is_synced_to_nz)

    def test_local_lesson_can_move_with_marks(self):
        grade = Grade.objects.create(lesson=self.l3, student=self.student, value='12')
        self.client.post(self.url('lesson_edit', lesson_id=self.l3.pk), self.lesson_post(date='2026-11-10'))
        self.l3.refresh_from_db(); grade.refresh_from_db()
        self.assertEqual(self.l3.date, dt.date(2026, 11, 10))
        self.assertEqual(self.l3.semester_id, self.sem1.pk)
        self.assertEqual(grade.lesson_id, self.l3.pk)

    def test_online_lesson_date_cannot_silently_move(self):
        self.client.post(self.url('lesson_edit', lesson_id=self.l1.pk), self.lesson_post(date='2026-11-10'))
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.date, dt.date(2026, 9, 5))

    def test_plan_reorder_and_reschedule_preserves_topic_identity(self):
        a = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='План A')
        b = LessonPlanTopic.objects.create(journal=self.journal, topic_number=2, title='План B')
        self.client.post(self.url('plan_topic_move', topic_id=b.pk), {'direction': 'up'})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.topic_number, b.topic_number), (2, 1))
        self.client.post(self.url('plan_topic_move', topic_id=b.pk), {'planned_date': '2026-11-01'})
        b.refresh_from_db()
        self.assertEqual(b.planned_date, dt.date(2026, 11, 1))

    def test_homework_points_to_future_lesson(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(date='2026-09-07', homework='Вигадане ДЗ', homework_target=f'lesson:{self.l2.pk}'))
        new = self.journal.lessons.get(topic='Нова тема')
        self.assertEqual(new.homework_due_lesson_id, self.l2.pk)
        self.assertIn('09.10.2026', new.get_homework_target_label())

    def test_homework_date_includes_day_of_new_unsaved_lesson(self):
        self.client.post(self.url('lesson_create'), self.lesson_post(homework='Вигадане ДЗ', homework_target='date:2026-10-09'))
        new = self.journal.lessons.get(topic='Нова тема')
        self.assertEqual(new.homework_due_date, dt.date(2026, 10, 9))
        self.assertIsNone(new.homework_due_lesson_id)
        self.assertEqual(new.get_homework_target_label(), 'На 09.10.2026')

    def test_homework_date_uses_future_lesson_or_dated_plan(self):
        LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Вигадана тема', planned_date=dt.date(2026, 11, 2))
        for i, day in enumerate(['2027-02-01', '2026-11-02']):
            with self.subTest(day=day):
                topic = f'Вигадане ДЗ за датою {i}'
                self.client.post(self.url('lesson_create'), self.lesson_post(topic=topic, homework_target=f'date:{day}'))
                self.assertEqual(self.journal.lessons.get(topic=topic).homework_due_date.isoformat(), day)

    def test_homework_date_rejects_past_invalid_unknown_and_foreign_day(self):
        other = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group,
            subject=Subject.objects.create(school=self.school, name='Вигаданий інший предмет'), teacher=self.teacher)
        Lesson.objects.create(journal=other, date=dt.date(2026, 11, 4), topic='Вигаданий інший урок')
        for day in ['2026-09-05', '2026-02-30', '2026-11-03', '2026-11-04']:
            with self.subTest(day=day):
                self.client.post(self.url('lesson_create'), self.lesson_post(homework_target=f'date:{day}'))
                self.assertFalse(self.journal.lessons.filter(topic='Нова тема').exists())

    def test_homework_date_excludes_days_with_only_summary_columns(self):
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 10), topic='Тематична', lesson_type='THEMATIC')
        LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Планова тематична',
            planned_date=dt.date(2026, 11, 3), lesson_type='THEMATIC')
        for day in ['2026-10-10', '2026-11-03']:
            self.client.post(self.url('lesson_create'), self.lesson_post(homework_target=f'date:{day}'))
            self.assertFalse(self.journal.lessons.filter(topic='Нова тема').exists())

    def test_swapping_topics_preserves_homework_due_on_same_day(self):
        self.l1.homework_due_date = self.l2.date; self.l1.save()
        self.client.post(self.url('lesson_swap'), {'first_id': self.l1.pk, 'second_id': self.l2.pk})
        self.l2.refresh_from_db()
        self.assertEqual(self.l2.homework_due_date, self.l2.date)
        self.assertFalse(self.l2.homework_for_next_lesson)

    def test_next_homework_skips_summary_columns(self):
        self.l1.homework_for_next_lesson = True; self.l1.save()
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 6), topic='Тематична', lesson_type='THEMATIC')
        self.assertEqual(self.l1.get_homework_due_lesson().pk, self.l2.pk)

    def test_homework_plan_resolves_after_conducting_topic(self):
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Майбутня тема')
        self.client.post(self.url('lesson_create'), self.lesson_post(date='2026-09-07', homework='ДЗ', homework_target=f'plan:{plan.pk}'))
        new = self.journal.lessons.get(topic='Нова тема')
        self.assertIsNone(new.get_homework_due_lesson())
        self.client.post(self.url('lesson_create'), self.lesson_post(date='2026-11-02', topic='Майбутня тема', plan_topic_id=plan.pk))
        self.assertEqual(new.get_homework_due_lesson().date, dt.date(2026, 11, 2))

    def test_invalid_foreign_homework_is_rejected_atomically(self):
        other = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group, subject=Subject.objects.create(school=self.school, name='Інший предмет'), teacher=self.teacher)
        lesson = Lesson.objects.create(journal=other, date=dt.date(2026, 12, 1), topic='Інший журнал')
        self.client.post(self.url('lesson_create'), self.lesson_post(homework_target=f'lesson:{lesson.pk}'))
        self.assertFalse(self.journal.lessons.filter(topic='Нова тема').exists())

    def test_preview_rounding_empty_and_special_marks(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='9')
        Grade.objects.create(lesson=self.l1, student=self.empty_student, value='ЗАР')
        response = self.client.post(self.url('assessment_calculate'), self.assessment_post())
        self.assertEqual(response.status_code, 200)
        self.assertIn('ORDINARY', response.context['form']['source_types'].value())
        rows = {r['student'].pk: r for r in response.context['rows']}
        self.assertEqual(rows[self.student.pk]['value'], '9')
        self.assertEqual(rows[self.student.pk]['count'], 2)
        self.assertEqual(rows[self.empty_student.pk]['value'], '')
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_summary_creation_needs_only_its_own_fields_and_does_not_copy_lesson_fields(self):
        for kind in ('THEMATIC', 'SEMESTER_1', 'SEMESTER_2', 'ANNUAL'):
            with self.subTest(kind=kind):
                response = self.client.post(self.url('lesson_create'), {
                    'lesson_type': kind, 'date': '2026-10-09', 'lesson_time': '14:30',
                    'lesson_number': '999', 'homework': 'Вигадане зайве ДЗ', 'homework_target': 'next',
                })
                self.assertEqual(response.status_code, 302)
                column = self.journal.lessons.get(lesson_type=kind)
                self.assertEqual(column.topic, dict(LessonType.choices)[kind])
                self.assertEqual(column.lesson_time, dt.time(14, 30))
                self.assertNotEqual(column.lesson_number, 999)
                self.assertEqual(column.homework, '')
                self.assertFalse(column.homework_for_next_lesson)
                self.assertFalse(column.is_synced_to_nz)
                self.assertFalse(column.grades.exists())

    def test_summary_creation_custom_title_and_matching_plan(self):
        plan = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1,
            title='Вигаданий тематичний підсумок', lesson_type='THEMATIC')
        self.client.post(self.url('lesson_create'), {'lesson_type': 'THEMATIC', 'date': '2026-10-09', 'plan_topic_id': plan.pk})
        column = self.journal.lessons.get(lesson_type='THEMATIC')
        self.assertEqual(column.topic, plan.title)
        self.assertEqual(column.linked_plan_topic_id, plan.pk)
        plan.refresh_from_db()
        self.assertTrue(plan.is_conducted)
        self.client.post(self.url('lesson_create'), {'lesson_type': 'ANNUAL', 'date': '2027-06-30', 'title': 'Власна назва підсумку'})
        self.assertEqual(self.journal.lessons.get(lesson_type='ANNUAL').topic, 'Власна назва підсумку')

    def test_summary_does_not_repeat_or_shift_existing_lesson_numbers(self):
        invalid = [
            {'repeat_frequency': 'weekly', 'repeat_end': 'count', 'repeat_count': '3'},
            {'insert_before_id': self.l2.pk}, {'date': '2027-07-01'},
            {'date': ''}, {'lesson_time': 'incorrect'},
        ]
        before = self.journal.lessons.count()
        for values in invalid:
            with self.subTest(values=values):
                self.client.post(self.url('lesson_create'), {'lesson_type': 'THEMATIC', 'date': '2026-10-09', **values})
                self.assertEqual(self.journal.lessons.count(), before)
                self.l2.refresh_from_db()
                self.assertEqual(self.l2.lesson_number, 1)
        self.assertFalse(AuditLog.objects.exists())

    def test_summary_cannot_conduct_an_ordinary_or_foreign_plan_topic(self):
        ordinary = LessonPlanTopic.objects.create(journal=self.journal, topic_number=1, title='Звичайна планова тема')
        response = self.client.post(self.url('lesson_create'), {'lesson_type': 'THEMATIC', 'date': '2026-10-09', 'plan_topic_id': ordinary.pk})
        self.assertEqual(response.status_code, 404)
        ordinary.refresh_from_db()
        self.assertFalse(ordinary.is_conducted)
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())
        other_subject = Subject.objects.create(school=self.school, name='Інший вигаданий предмет')
        other = Journal.objects.create(school=self.school, academic_year=self.year,
            student_class=self.group, subject=other_subject, teacher=self.teacher)
        foreign = LessonPlanTopic.objects.create(journal=other, topic_number=1,
            title='Тематична іншого журналу', lesson_type='THEMATIC')
        response = self.client.post(self.url('lesson_create'), {'lesson_type': 'THEMATIC', 'date': '2026-10-09', 'plan_topic_id': foreign.pk})
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertFalse(foreign.is_conducted)

    def test_assessment_opens_with_selected_summary_parameters_and_default_sources(self):
        thematic = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 10),
            topic='Вигадана тематична', lesson_type='THEMATIC')
        for kind, expected_source, expected_types in [
            ('THEMATIC', 'current', {'ORDINARY', 'QUIZ', 'TEST', 'PRACTICAL', 'LAB', 'EXAM'}),
            ('SEMESTER_1', 'thematic', {'THEMATIC'}),
            ('ANNUAL', 'semester', {'SEMESTER_1', 'SEMESTER_2'}),
        ]:
            with self.subTest(kind=kind):
                response = self.client.get(self.url('assessment_calculate'), {
                    'grade_type': kind, 'date': '2026-10-09', 'lesson_time': '13:45', 'title': 'Вигаданий підсумок'})
                form = response.context['form']
                self.assertEqual(form['grade_type'].value(), kind)
                self.assertEqual(form['source'].value(), expected_source)
                self.assertEqual(form['date'].value(), dt.date(2026, 10, 9))
                self.assertEqual(form['lesson_time'].value(), dt.time(13, 45))
                self.assertEqual(form['title'].value(), 'Вигаданий підсумок')
                self.assertEqual(set(form['source_types'].value()), expected_types)
                if kind == 'SEMESTER_1':
                    self.assertEqual(form['lesson_ids'].value(), [str(thematic.pk)])
        self.assertEqual(self.journal.lessons.filter(lesson_type='ANNUAL').count(), 0)

    def test_thematic_calculation_filters_selected_lesson_types(self):
        self.l2.lesson_type = 'QUIZ'
        self.l2.save()
        test = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 8),
            topic='Вигадана контрольна', lesson_type='TEST')
        for lesson, value in ((self.l1, '8'), (self.l2, '10'), (test, '4')):
            Grade.objects.create(lesson=lesson, student=self.student, value=value)
        data = self.assessment_post(lesson_ids=[str(self.l1.pk), str(self.l2.pk), str(test.pk)],
            source_types_present='1', source_types=['QUIZ', 'TEST'])
        preview = self.client.post(self.url('assessment_calculate'), data)
        row = next(row for row in preview.context['rows'] if row['student'].pk == self.student.pk)
        self.assertEqual((row['count'], row['value']), (2, '7'))
        response = self.client.post(self.url('assessment_calculate'), {
            **data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertEqual(response.status_code, 302)
        column = self.journal.lessons.get(lesson_type='THEMATIC')
        self.assertEqual(column.grades.get(student=self.student).value, '7')

    def test_journal_actions_open_the_selected_semester_calculation(self):
        soup = BeautifulSoup(self.client.get(self.url('journal_detail'), {'semester_id': self.sem2.pk}).content, 'html.parser')
        links = soup.select('#journalActions [data-assessment-link]')
        self.assertEqual(len(links), 6)
        self.assertIn('grade_type=SEMESTER_2', links[2]['href'])
        self.assertIn(f'semester_id={self.sem2.pk}', links[0]['href'])
        self.assertEqual(soup.select_one('#assessmentModal')['role'], 'dialog')

    def test_journal_menu_can_include_withdrawn_students_without_losing_marks(self):
        enrollment = ClassEnrollment.objects.get(student=self.student, student_class=self.group)
        enrollment.is_active = False
        enrollment.save()
        Grade.objects.create(lesson=self.l1, student=self.student, value='9')
        name = self.student.get_full_name_ukr()
        ordinary = BeautifulSoup(self.client.get(self.url('journal_detail')).content, 'html.parser')
        self.assertTrue(ordinary.select_one(f'.student-row[data-student-name="{name}"]').has_attr('hidden'))
        history = BeautifulSoup(self.client.get(self.url('journal_detail'), {'withdrawn': '1'}).content, 'html.parser')
        self.assertFalse(history.select_one(f'.student-row[data-student-name="{name}"]').has_attr('hidden'))
        self.assertEqual(self.l1.grades.get(student=self.student).value, '9')

    def test_calculation_defaults_to_current_date_and_semester_start(self):
        with patch('apps.journals.services.assessment.timezone.localdate', return_value=dt.date(2026, 10, 10)):
            response = self.client.get(self.url('assessment_calculate'), {'modal': '1'})
        form = response.context['form']
        self.assertEqual(form['date'].value(), dt.date(2026, 10, 10))
        self.assertEqual(form['start_date'].value(), self.sem1.start_date)
        self.assertNotIn(str(self.l3.pk), form['lesson_ids'].value())
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('#id_date')['value'], '2026-10-10')
        self.assertIsNone(soup.select_one('html'))
        self.assertIsNotNone(soup.select_one('.assessment-date-range'))

    def test_thematic_period_uses_latest_graded_summary_only(self):
        previous = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 20), lesson_type='THEMATIC', topic='Попередній підсумок')
        Grade.objects.create(lesson=previous, student=self.student, value='9')
        blank = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 1), lesson_type='THEMATIC', topic='Порожня колонка')
        Grade.objects.create(lesson=blank, student=self.student, value='')
        future = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 20), lesson_type='THEMATIC', topic='Майбутня колонка')
        Grade.objects.create(lesson=future, student=self.student, value='10')
        response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'date': '2026-10-10'})
        self.assertEqual(response.context['form']['start_date'].value(), previous.date)
        self.assertEqual(response.context['form']['lesson_ids'].value(), [str(blank.pk), str(self.l2.pk)])

    def test_thematic_period_resets_at_second_semester(self):
        previous = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 12, 20), lesson_type='THEMATIC', topic='Перший семестр')
        Grade.objects.create(lesson=previous, student=self.student, value='9')
        response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'date': '2027-02-10'})
        self.assertEqual(response.context['form']['start_date'].value(), self.sem2.start_date)
        self.assertEqual(response.context['form']['lesson_ids'].value(), [str(self.l3.pk)])

    def test_semester_modal_hides_dates_and_requires_type_selection(self):
        response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'grade_type': 'SEMESTER_1', 'date': '2026-10-10'})
        self.assertEqual(response.context['form']['source_types'].value(), [])
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertIsNone(soup.select_one('.assessment-date-range'))
        self.assertIsNotNone(soup.select_one('[hidden] #id_start_date'))
        invalid = self.client.post(self.url('assessment_calculate'), self.assessment_post(modal='1', source_types=[]))
        self.assertIn('source_types', invalid.context['form'].errors)
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_modal_uses_school_times_without_losing_selected_value(self):
        LessonTime.objects.create(school=self.school, number=8, start_time=dt.time(15, 20), end_time=dt.time(16, 5))
        self.l2.lesson_time = dt.time(15, 20)
        self.l2.save()
        response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'date': '2026-10-09'})
        soup = BeautifulSoup(response.content, 'html.parser')
        self.assertEqual(soup.select_one('#id_lesson_time option[selected]')['value'], '15:20')

    def test_modal_selects_lesson_ids_on_server_and_saves_after_review(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='9')
        data = self.assessment_post(modal='1', source_types=['ORDINARY'], lesson_ids=[], lesson_time='15:20')
        preview = self.client.post(self.url('assessment_calculate'), data)
        self.assertFalse(preview.context['form'].errors)
        row = next(row for row in preview.context['rows'] if row['student'].pk == self.student.pk)
        self.assertEqual((row['count'], row['value']), (2, '9'))
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())
        saved = self.client.post(self.url('assessment_calculate'), {
            **data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertEqual(saved.json()['redirect'], self.url('journal_detail'))
        column = self.journal.lessons.get(lesson_type='THEMATIC')
        self.assertEqual(column.lesson_time, dt.time(15, 20))
        self.assertEqual(column.grades.get(student=self.student).value, '9')

    def test_modal_boundary_preserves_later_lessons_on_same_day(self):
        earlier = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 9), lesson_number=2, lesson_type='THEMATIC', topic='Попередній підсумок')
        later = Lesson.objects.create(journal=self.journal, date=earlier.date, lesson_number=3, topic='Після підсумку')
        for lesson, value in ((self.l2, '2'), (earlier, '6'), (later, '11')):
            Grade.objects.create(lesson=lesson, student=self.student, value=value)
        data = self.assessment_post(modal='1', start_date='2026-10-09', source_types=['ORDINARY', 'THEMATIC'])
        preview = self.client.post(self.url('assessment_calculate'), data)
        row = next(row for row in preview.context['rows'] if row['student'].pk == self.student.pk)
        self.assertEqual((row['count'], row['value']), (1, '11'))

    def test_semester_period_uses_semester_summary_instead_of_thematic(self):
        previous = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 20), lesson_type='SEMESTER_1', topic='Попередня семестрова')
        Grade.objects.create(lesson=previous, student=self.student, value='2')
        thematic = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 1), lesson_type='THEMATIC', topic='Тематична')
        Grade.objects.create(lesson=thematic, student=self.student, value='10')
        Grade.objects.create(lesson=self.l2, student=self.student, value='8')
        data = self.assessment_post(modal='1', grade_type='SEMESTER_1', source_types=['ORDINARY', 'THEMATIC'], start_date='2026-09-01', end_date='2027-06-30')
        preview = self.client.post(self.url('assessment_calculate'), data)
        self.assertEqual(preview.context['form'].cleaned_data['start_date'], previous.date)
        self.assertEqual(preview.context['form'].cleaned_data['end_date'], dt.date(2026, 10, 9))
        row = next(row for row in preview.context['rows'] if row['student'].pk == self.student.pk)
        self.assertEqual((row['count'], row['value']), (2, '9'))

    def test_new_boundary_after_modal_preview_requires_recalculation(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='10')
        data = self.assessment_post(modal='1', source_types=['ORDINARY'])
        preview = self.client.post(self.url('assessment_calculate'), data)
        previous = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 20), lesson_type='THEMATIC', topic='Новий підсумок')
        Grade.objects.create(lesson=previous, student=self.student, value='9')
        saved = self.client.post(self.url('assessment_calculate'), {
            **data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertContains(saved, 'Оцінки або параметри змінилися')
        self.assertEqual(self.journal.lessons.filter(lesson_type='THEMATIC').count(), 1)

    def test_modal_rejects_dates_outside_academic_year(self):
        invalid = self.client.post(self.url('assessment_calculate'), self.assessment_post(
            modal='1', date='2028-01-01', source_types=['ORDINARY']))
        self.assertIn('date', invalid.context['form'].errors)
        self.assertIsNone(invalid.context['rows'])

    def test_selected_semester_limits_default_placement_date(self):
        with patch('apps.journals.services.assessment.timezone.localdate', return_value=dt.date(2027, 2, 10)):
            response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'semester_id': self.sem1.pk})
        self.assertEqual(response.context['form']['date'].value(), self.sem1.end_date)
        self.assertEqual(response.context['form']['start_date'].value(), self.sem1.start_date)

    def test_modular_and_nush_calculations_create_distinct_summary_types(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='9')
        for kind in ('MODULAR', 'THEMATIC_NUSH', 'SEMESTER_1_NUSH'):
            with self.subTest(kind=kind):
                data = self.assessment_post(modal='1', grade_type=kind, source_types=['ORDINARY'])
                preview = self.client.post(self.url('assessment_calculate'), data)
                self.assertFalse(preview.context['form'].errors)
                row = next(row for row in preview.context['rows'] if row['student'].pk == self.student.pk)
                self.assertEqual((row['count'], row['value']), (2, '9'))
                saved = self.client.post(self.url('assessment_calculate'), {
                    **data, 'action': 'save', 'preview_token': preview.context['preview_token']})
                self.assertEqual(saved.json()['redirect'], self.url('journal_detail'))
                column = self.journal.lessons.get(lesson_type=kind)
                self.assertEqual(column.grades.get(student=self.student).value, '9')
                self.assertFalse(column.is_synced_to_nz)

    def test_modular_and_nush_periods_have_independent_boundaries(self):
        for number, kind in enumerate(('THEMATIC', 'MODULAR', 'THEMATIC_NUSH', 'SEMESTER_1_NUSH'), start=1):
            column = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 10 + number), lesson_type=kind, topic=kind)
            Grade.objects.create(lesson=column, student=self.student, value='9')
        for kind, expected in (('THEMATIC', 11), ('MODULAR', 12), ('THEMATIC_NUSH', 13), ('SEMESTER_1_NUSH', 14)):
            response = self.client.get(self.url('assessment_calculate'), {'modal': '1', 'grade_type': kind, 'date': '2026-10-10'})
            self.assertEqual(response.context['form']['start_date'].value(), dt.date(2026, 9, expected))

    def test_current_source_excludes_modular_and_nush_summary_marks(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        summaries = []
        for number, kind in enumerate(('MODULAR', 'THEMATIC_NUSH', 'SEMESTER_1_NUSH', 'SEMESTER_2_NUSH'), start=2):
            lesson = Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=number, topic=kind, lesson_type=kind)
            Grade.objects.create(lesson=lesson, student=self.student, value='1')
            summaries.append(lesson)
        rows, _, _ = calculate(self.journal, [self.l1, *summaries], 'current', 'nearest')
        row = next(row for row in rows if row['student'].pk == self.student.pk)
        self.assertEqual((row['count'], row['value']), (1, '8'))

    def test_nush_semester_defaults_to_nush_thematic_sources_in_advanced_form(self):
        thematic = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 9, 10), topic='НУШ', lesson_type='THEMATIC_NUSH')
        response = self.client.get(self.url('assessment_calculate'), {'grade_type': 'SEMESTER_1_NUSH', 'date': '2026-10-10'})
        self.assertEqual(response.context['form']['source_types'].value(), ['THEMATIC_NUSH'])
        self.assertEqual(response.context['form']['lesson_ids'].value(), [str(thematic.pk)])

    def test_assessment_type_choices_require_valid_nonempty_selection(self):
        for types in ([], ['THEMATIC'], ['UNKNOWN']):
            with self.subTest(types=types):
                response = self.client.post(self.url('assessment_calculate'), self.assessment_post(
                    source_types_present='1', source_types=types))
                self.assertIsNone(response.context['rows'])
                self.assertIn('source_types', response.context['form'].errors)
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_changing_assessment_types_after_preview_cannot_save(self):
        self.l2.lesson_type = 'QUIZ'
        self.l2.save()
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='10')
        data = self.assessment_post(source_types_present='1', source_types=['ORDINARY', 'QUIZ'])
        preview = self.client.post(self.url('assessment_calculate'), data)
        response = self.client.post(self.url('assessment_calculate'), {
            **data, 'action': 'save', 'source_types': ['ORDINARY'], 'preview_token': preview.context['preview_token']})
        self.assertContains(response, 'Оцінки або параметри змінилися')
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_create_form_has_separate_disabled_summary_fields_and_calculation_link(self):
        soup = BeautifulSoup(self.client.get(self.url('journal_detail')).content, 'html.parser')
        form = soup.select_one('#createLessonForm')
        self.assertTrue(form.select_one('#createLessonSummaryFields').has_attr('disabled'))
        self.assertTrue(form.select_one('#createLessonSummaryFields').has_attr('hidden'))
        self.assertIsNotNone(form.select_one('#createLessonOrdinaryFields #lesson_topic_input'))
        self.assertIsNotNone(form.select_one('#createLessonSummaryFields #create_summary_title'))
        self.assertEqual(form.select_one('#create_lesson_type')['name'], 'lesson_type')
        self.assertEqual(form['data-assessment-url'], self.url('assessment_calculate'))

    def test_calculation_excludes_absence_and_summaries(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='12')
        Attendance.objects.create(lesson=self.l2, student=self.student, status='ABSENT_EXCUSED')
        thematic = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 9), lesson_number=2, topic='Тематична', lesson_type='THEMATIC')
        Grade.objects.create(lesson=thematic, student=self.student, value='3')
        rows, _, _ = calculate(self.journal, [self.l1, self.l2, thematic], 'current', 'nearest')
        row = next(r for r in rows if r['student'].pk == self.student.pk)
        self.assertEqual(row['value'], '8')
        self.assertEqual(row['count'], 1)

    def test_save_creates_summary_after_selected_date_and_local_marks(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        Grade.objects.create(lesson=self.l2, student=self.student, value='9')
        data = self.assessment_post(lesson_time='14:30')
        preview = self.client.post(self.url('assessment_calculate'), data)
        response = self.client.post(self.url('assessment_calculate'), {**data, 'action': 'save', 'preview_token': preview.context['preview_token'], f'grade_{self.student.pk}': '10'})
        self.assertEqual(response.status_code, 302)
        result = self.journal.lessons.get(lesson_type='THEMATIC')
        self.assertEqual(result.lesson_number, 2)
        self.assertEqual(result.lesson_time, dt.time(14, 30))
        self.assertEqual(result.grades.get(student=self.student).value, '10')
        self.assertFalse(result.grades.get(student=self.student).is_synced_to_nz)
        self.assertFalse(result.grades.filter(student=self.empty_student).exists())

    def test_stale_preview_cannot_overwrite_after_grade_change(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        data = self.assessment_post()
        preview = self.client.post(self.url('assessment_calculate'), data)
        grade.value = '12'; grade.save()
        response = self.client.post(self.url('assessment_calculate'), {**data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertContains(response, 'Оцінки або параметри змінилися')
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_save_without_preview_is_rejected(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8')
        response = self.client.post(self.url('assessment_calculate'), self.assessment_post(action='save'))
        self.assertContains(response, 'Перегляньте новий розрахунок')
        self.assertFalse(self.journal.lessons.filter(lesson_type='THEMATIC').exists())

    def test_semester_uses_selected_thematic_marks_only(self):
        thematic = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 9), lesson_number=2, topic='Тема підсумок', lesson_type='THEMATIC')
        Grade.objects.create(lesson=thematic, student=self.student, value='10')
        Grade.objects.create(lesson=self.l1, student=self.student, value='1')
        data = self.assessment_post(grade_type='SEMESTER_1', source='thematic', lesson_ids=[str(self.l1.pk), str(thematic.pk)])
        response = self.client.post(self.url('assessment_calculate'), data)
        row = next(r for r in response.context['rows'] if r['student'].pk == self.student.pk)
        self.assertEqual(row['value'], '10')

    def test_student_from_other_school_is_rejected(self):
        outsider = Student.objects.create(school=School.objects.create(name='Інша вигадана школа'), first_name='Інший', last_name='Вигаданий')
        response = self.client.post(self.url('update_grade'), {'lesson_id': self.l1.pk, 'student_id': outsider.pk, 'value': '9'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Grade.objects.filter(student=outsider).exists())

    def test_write_operations_reject_get_and_archived_journal(self):
        self.assertEqual(self.client.get(self.url('lesson_swap')).status_code, 405)
        self.journal.is_archived = True; self.journal.save()
        self.assertEqual(self.client.post(self.url('lesson_create'), self.lesson_post()).status_code, 403)

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_preserves_local_grade_clear_and_topics(self, mock_fetch):
        self.l1.topic = 'Локальна тема'; self.l1.is_synced_to_nz = False; self.l1.save()
        Grade.objects.create(lesson=self.l1, student=self.student, value='', is_synced_to_nz=False, nz_last_synced_value='8')
        mock_fetch.return_value = self.nz_data()
        stats = NZService().import_journal_to_local('synthetic-journal', self.journal, user=self.teacher)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.topic, 'Локальна тема')
        self.assertFalse(self.l1.is_synced_to_nz)
        self.assertEqual(self.l1.nz_last_synced_topic, 'Онлайн тема')
        self.assertEqual(Grade.objects.get(lesson=self.l1, student=self.student).value, '')
        self.assertEqual(stats['local_changes_preserved'], 2)

    def nz_data(self):
        return {'students': [{'nz_student_id': self.student.nz_student_id, 'name': self.student.get_full_name_ukr()}],
                'lessons': [{'nz_schedule_id': self.l1.nz_schedule_id, 'date': self.l1.date, 'lesson_number': 1, 'topic': 'Онлайн тема', 'homework': ''}],
                'marks': [{'nz_student_id': self.student.nz_student_id, 'student_name': self.student.get_full_name_ukr(), 'nz_schedule_id': self.l1.nz_schedule_id,
                           'date': self.l1.date, 'lesson_number': 1, 'value': '9', 'mark_id': 14}]}

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_updates_online_grade_and_topic(self, mock_fetch):
        Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        mock_fetch.return_value = self.nz_data()
        NZService().import_journal_to_local('synthetic-journal', self.journal)
        self.l1.refresh_from_db()
        self.assertEqual(self.l1.topic, 'Онлайн тема')
        grade = Grade.objects.get(lesson=self.l1, student=self.student)
        self.assertEqual(grade.value, '9')
        self.assertTrue(grade.is_synced_to_nz)

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_distinguishes_multiple_same_day_online_columns(self, mock_fetch):
        data = self.nz_data()
        data['lessons'].append({'nz_schedule_id': 'synthetic-second', 'date': self.l1.date, 'lesson_number': 1, 'topic': 'Другий на дату', 'homework': ''})
        mock_fetch.return_value = data
        service = NZService()
        service.import_journal_to_local('synthetic-journal', self.journal)
        service.import_journal_to_local('synthetic-journal', self.journal)
        self.assertEqual(self.journal.lessons.filter(date=self.l1.date).count(), 2)
        self.assertEqual(self.journal.lessons.get(nz_schedule_id='synthetic-second').lesson_number, 2)

    def test_homework_push_uses_only_confirmed_select_field(self):
        self.l1.homework = 'ДЗ'; self.l1.homework_due_lesson = self.l2; self.l1.save()
        service = NZService()
        response = MagicMock(status_code=200, url='https://nz.ua/journal/add-edit-home-task')
        response.text = '<form><select name="Observed[hometask_schedule]"><option value="synthetic-b">Майбутній урок</option></select></form>'
        with patch.object(service.session, 'get', return_value=response):
            self.assertEqual(service._homework_target_fields(self.l1, 'https://nz.ua/journal/add-edit-home-task'), {'Observed[hometask_schedule]': 'synthetic-b'})
        response.text = '<form><select name="unknown"><option value="other">Інший урок</option></select></form>'
        with patch.object(service.session, 'get', return_value=response):
            with self.assertRaises(NZServiceError):
                service._homework_target_fields(self.l1, 'https://nz.ua/journal/add-edit-home-task')

    def test_summary_column_type_detection(self):
        for label, expected in [('Тематична', 'THEMATIC'), ('II сем', 'SEMESTER_2'), ('Річна', 'ANNUAL')]:
            self.assertEqual(NZService._column_type(BeautifulSoup('<td>' + label + '</td>', 'html.parser').td), expected)
        self.assertEqual(NZService._column_type(BeautifulSoup('<td>Семестрова</td>', 'html.parser').td, dt.date(2027, 5, 31)), 'SEMESTER_2')

    def mock_push(self, response, side_effect=None):
        service = NZService()
        service.csrf_token = 'synthetic-csrf'
        with patch.object(service.session, 'post', return_value=response, side_effect=side_effect) as post, patch('apps.journals.services.nz_service.time.sleep'):
            log = service.push_journal_changes(self.journal)
        return log, post

    def test_replacing_absence_with_grade_sends_one_effective_mark(self):
        Grade.objects.create(lesson=self.l1, student=self.student, value='10')
        attendance = Attendance.objects.create(lesson=self.l1, student=self.student, status='PRESENT', nz_last_synced_status='ABSENT_UNEXCUSED')
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"success":true}')
        log, post = self.mock_push(response)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['data']['mark'], 15)
        self.assertEqual(log.items_count, 2)  # one effective cell plus one unlinked local lesson
        attendance.refresh_from_db()
        self.assertTrue(attendance.is_synced_to_nz)
        self.assertEqual(NZService().get_unsynced_changes(self.journal)['total_count'], 1)  # only the unlinked local lesson

    def test_replacing_grade_with_absence_sends_no_separate_clear(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='', nz_last_synced_value='8')
        Attendance.objects.create(lesson=self.l1, student=self.student, status='ABSENT_UNEXCUSED')
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"success":true}')
        _, post = self.mock_push(response)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['data']['mark'], 1)
        grade.refresh_from_db()
        self.assertTrue(grade.is_synced_to_nz)
        self.assertEqual(grade.nz_last_synced_value, '')

    def test_http_success_with_application_failure_keeps_mark_local(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='10')
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"success":false,"error":"synthetic failure"}')
        self.mock_push(response)
        grade.refresh_from_db()
        self.assertFalse(grade.is_synced_to_nz)

    def test_edit_during_push_is_not_overwritten_or_marked_synced(self):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='10')
        response = MagicMock(status_code=200, url='https://nz.ua/journal/set-mark', text='{"success":true}')
        def change_during_push(*args, **kwargs):
            grade.value = '12'; grade.save()
            return response
        self.mock_push(response, change_during_push)
        grade.refresh_from_db()
        self.assertEqual(grade.value, '12')
        self.assertEqual(grade.nz_last_synced_value, '10')
        self.assertFalse(grade.is_synced_to_nz)

    def test_annual_calculation_can_use_existing_legacy_semester_grades(self):
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='SEMESTER_1', value='8')
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='SEMESTER_2', value='9')
        data = self.assessment_post(grade_type='ANNUAL', source='semester', lesson_ids=['legacy:SEMESTER_1', 'legacy:SEMESTER_2'])
        response = self.client.post(self.url('assessment_calculate'), data)
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.context['rows'] if r['student'].pk == self.student.pk)
        self.assertEqual(row['value'], '9')
        self.assertEqual(row['count'], 2)
        saved = self.client.post(self.url('assessment_calculate'), {**data, 'action': 'save', 'preview_token': response.context['preview_token']})
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(self.journal.lessons.get(lesson_type='ANNUAL').grades.get(student=self.student).value, '9')

    def test_different_legacy_thematic_grades_keep_separate_columns(self):
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='THEMATIC', thematic_name='Тема A', value='7')
        SummaryGrade.objects.create(journal=self.journal, student=self.student, grade_type='THEMATIC', thematic_name='Тема B', value='10')
        response = self.client.get(self.url('journal_detail'))
        self.assertEqual(len(response.context['legacy_summary_columns']), 2)
        self.assertContains(response, 'value="7"')
        self.assertContains(response, 'value="10"')

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_moves_existing_online_schedule_without_losing_grade(self, mock_fetch):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        data = self.nz_data()
        data['lessons'][0]['date'] = dt.date(2026, 9, 6)
        data['marks'][0]['date'] = dt.date(2026, 9, 6)
        mock_fetch.return_value = data
        NZService().import_journal_to_local('synthetic-journal', self.journal)
        self.l1.refresh_from_db(); grade.refresh_from_db()
        self.assertEqual(self.l1.date, dt.date(2026, 9, 6))
        self.assertEqual(grade.lesson_id, self.l1.pk)
        self.assertEqual(grade.value, '9')

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_online_clear_does_not_keep_stale_grade(self, mock_fetch):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        data = self.nz_data()
        data['marks'][0].update(value='', mark_id=0)
        mock_fetch.return_value = data
        NZService().import_journal_to_local('synthetic-journal', self.journal)
        grade.refresh_from_db()
        self.assertEqual(grade.value, '')
        self.assertTrue(grade.is_synced_to_nz)

    @patch.object(NZService, 'fetch_journal_data')
    def test_pull_confirms_exact_matching_local_value_without_upload(self, mock_fetch):
        grade = Grade.objects.create(lesson=self.l1, student=self.student, value='9', is_synced_to_nz=False)
        mock_fetch.return_value = self.nz_data()
        service = NZService()
        with patch.object(service.session, 'post') as post:
            service.import_journal_to_local('synthetic-journal', self.journal)
        grade.refresh_from_db()
        self.assertTrue(grade.is_synced_to_nz)
        self.assertEqual(grade.nz_last_synced_value, '9')
        post.assert_not_called()

    def test_toggle_student_ignore_and_block_grades(self):
        toggle_url = reverse('portal:toggle_student_sync_ignore', kwargs={'journal_id': self.journal.pk, 'student_id': self.empty_student.pk})
        resp = self.client.post(toggle_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['is_ignored'])
        self.assertTrue(self.journal.ignored_students.filter(pk=self.empty_student.pk).exists())

        # Attempt to enter grade should fail with 400
        grade_url = reverse('portal:update_grade', kwargs={'journal_id': self.journal.pk})
        resp_grade = self.client.post(grade_url, {'lesson_id': self.l1.pk, 'student_id': self.empty_student.pk, 'value': '10'})
        self.assertEqual(resp_grade.status_code, 400)
        self.assertFalse(Grade.objects.filter(lesson=self.l1, student=self.empty_student).exists())

        # Attempt to enter attendance should fail with 400
        att_url = reverse('portal:update_attendance', kwargs={'journal_id': self.journal.pk})
        resp_att = self.client.post(att_url, {'lesson_id': self.l1.pk, 'student_id': self.empty_student.pk, 'status': 'ABSENT_UNEXCUSED'})
        self.assertEqual(resp_att.status_code, 400)

        # Attempt to enter summary grade should fail with 400
        sum_url = reverse('portal:update_summary_grade', kwargs={'journal_id': self.journal.pk})
        resp_sum = self.client.post(sum_url, {'student_id': self.empty_student.pk, 'grade_type': 'THEMATIC', 'value': '11'})
        self.assertEqual(resp_sum.status_code, 400)

        # Toggle back
        resp = self.client.post(toggle_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['is_ignored'])
        self.assertFalse(self.journal.ignored_students.filter(pk=self.empty_student.pk).exists())

        # Now entering grade succeeds
        resp_grade = self.client.post(grade_url, {'lesson_id': self.l1.pk, 'student_id': self.empty_student.pk, 'value': '10'})
        self.assertEqual(resp_grade.status_code, 200)
        self.assertTrue(Grade.objects.filter(lesson=self.l1, student=self.empty_student, value='10').exists())

    def test_ignored_student_excluded_from_nz_pending_cells_and_roster(self):
        from apps.journals.models import GradeAPIConnection
        from apps.journals.services.roster_transfer import roster_request
        # Create an unsynced grade for student
        Grade.objects.create(lesson=self.l1, student=self.student, value='10', is_synced_to_nz=False)
        pending_before = NZService._pending_cells(self.journal)
        self.assertEqual(len(pending_before), 1)

        # Ignore student
        self.journal.ignored_students.add(self.student)

        # NZ pending cells must exclude ignored student
        pending_after = NZService._pending_cells(self.journal)
        self.assertEqual(len(pending_after), 0)

        # Roster request must exclude ignored student
        conn = GradeAPIConnection.objects.create(journal=self.journal, contract_version=2, url='http://127.0.0.1:8000/api/v2/journal/grades/', api_key='test')
        payload, refs = roster_request(conn)
        student_ids_in_payload = [s['id'] for s in payload['students']]
        self.assertNotIn(str(self.student.pk), [s.pk for s in refs.values()])

