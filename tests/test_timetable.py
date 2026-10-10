import datetime as dt
from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, StudentClass, Subject, LessonTime, WeeklyScheduleEntry
from apps.journals.models import Journal, Lesson, AuditLog


class TimetableTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Вигадана школа розкладу')
        self.other_school = School.objects.create(name='Інша вигадана школа')
        self.teacher = User.objects.create_user(username='schedule_teacher', school=self.school, role=User.Role.TEACHER)
        self.colleague = User.objects.create_user(username='schedule_colleague', school=self.school, role=User.Role.TEACHER)
        self.outsider = User.objects.create_user(username='schedule_outsider', school=self.other_school, role=User.Role.TEACHER)
        self.admin = User.objects.create_user(username='schedule_admin', role=User.Role.SUPERADMIN)
        self.year = AcademicYear.objects.create(school=self.school, name='2026/2027',
            start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 6, 30), is_current=True)
        self.other_year = AcademicYear.objects.create(school=self.other_school, name='2026/2027',
            start_date=self.year.start_date, end_date=self.year.end_date)
        self.group = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Тестовий 5-А')
        self.group2 = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Тестовий 6-А')
        self.other_group = StudentClass.objects.create(school=self.other_school, academic_year=self.other_year, name='Інший 7-А')
        self.subject = Subject.objects.create(school=self.school, name='Вигадана математика')
        self.subject2 = Subject.objects.create(school=self.school, name='Вигадана інформатика')
        self.other_subject = Subject.objects.create(school=self.other_school, name='Інший предмет')
        self.journal = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group, subject=self.subject, teacher=self.teacher)
        self.second_journal = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group2, subject=self.subject, teacher=self.teacher)
        self.colleague_journal = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.group, subject=self.subject2, teacher=self.colleague)
        self.foreign_journal = Journal.objects.create(school=self.other_school, academic_year=self.other_year, student_class=self.other_group, subject=self.other_subject, teacher=self.outsider)
        self.slot1 = LessonTime.objects.create(school=self.school, number=1, start_time=dt.time(8, 30), end_time=dt.time(9, 15))
        self.slot2 = LessonTime.objects.create(school=self.school, number=2, start_time=dt.time(9, 25), end_time=dt.time(10, 10))
        self.foreign_slot = LessonTime.objects.create(school=self.other_school, number=3, start_time=dt.time(10, 20), end_time=dt.time(11, 5))
        self.client.force_login(self.teacher)

    def url(self, name):
        return reverse('portal:' + name)

    def schedule_post(self, **changes):
        return {'school_id': self.school.pk, 'teacher_id': self.teacher.pk, 'year_id': self.year.pk,
                'week': '2026-10-05', 'journal': self.journal.pk, 'weekday': '0', 'lesson_time': self.slot1.pk,
                'start_date': '', 'end_date': '', 'room': '101', 'action': 'save', **changes}

    def entry(self, **changes):
        return WeeklyScheduleEntry.objects.create(**{
            'journal': self.journal, 'weekday': 0, 'lesson_time': self.slot1,
            'start_date': self.year.start_date, 'end_date': self.year.end_date, **changes})

    def items(self, response):
        return [item for row in response.context['schedule_rows'] for cell in row['cells'] for item in cell['items']]

    def test_new_pages_require_login(self):
        self.client.logout()
        for name in ('weekly_schedule', 'lesson_times'):
            response = self.client.get(self.url(name))
            self.assertEqual(response.status_code, 302)
            self.assertIn('/accounts/login/', response.url)

    def test_school_times_are_shared_with_colleagues_and_hidden_from_other_schools(self):
        for user in (self.teacher, self.colleague):
            self.client.force_login(user)
            response = self.client.get(self.url('lesson_times'))
            self.assertEqual([slot.pk for slot in response.context['slots']], [self.slot1.pk, self.slot2.pk])
        self.client.force_login(self.outsider)
        response = self.client.get(self.url('lesson_times'))
        self.assertEqual([slot.pk for slot in response.context['slots']], [self.foreign_slot.pk])

    def test_teacher_cannot_read_or_change_another_school_times(self):
        self.assertEqual(self.client.get(self.url('lesson_times'), {'school_id': self.other_school.pk}).status_code, 404)
        for action in ('save', 'delete'):
            response = self.client.post(self.url('lesson_times'), {'school_id': self.school.pk, 'slot_id': self.foreign_slot.pk, 'action': action})
            self.assertEqual(response.status_code, 404)
        self.foreign_slot.refresh_from_db()

    def test_school_time_can_be_created_and_audited(self):
        response = self.client.post(self.url('lesson_times'), {'school_id': self.school.pk,
            'number': 3, 'start_time': '10:20', 'end_time': '11:05', 'is_active': 'on', 'action': 'save'})
        self.assertEqual(response.status_code, 302)
        slot = LessonTime.objects.get(school=self.school, number=3)
        self.assertEqual(str(slot), '3 урок (10:20 — 11:05)')
        self.assertTrue(AuditLog.objects.filter(school=self.school, target_model='LessonTime', target_id=str(slot.pk)).exists())

    def test_invalid_overlapping_duplicate_or_backward_school_times_are_rejected(self):
        for number, start, end in ((3, '08:45', '09:30'), (1, '10:20', '11:05'), (3, '11:05', '10:20'), (31, '10:20', '11:05'), (3, '10:20:15', '11:05')):
            with self.subTest(number=number, start=start):
                response = self.client.post(self.url('lesson_times'), {'number': number, 'start_time': start, 'end_time': end, 'is_active': 'on'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                self.assertEqual(LessonTime.objects.filter(school=self.school).count(), 2)

    def test_slot_edit_preserves_time_of_existing_journal_lessons(self):
        lesson = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 5), topic='Збережений урок', lesson_time=self.slot1.start_time)
        response = self.client.post(self.url('lesson_times'), {'slot_id': self.slot1.pk, 'number': 1,
            'start_time': '08:35', 'end_time': '09:20', 'is_active': 'on'})
        self.assertEqual(response.status_code, 302)
        lesson.refresh_from_db()
        self.slot1.refresh_from_db()
        self.assertEqual(lesson.lesson_time, dt.time(8, 30))
        self.assertEqual(self.slot1.start_time, dt.time(8, 35))

    def test_time_used_in_timetable_cannot_be_deleted(self):
        self.entry()
        response = self.client.post(self.url('lesson_times'), {'slot_id': self.slot1.pk, 'action': 'delete'}, follow=True)
        self.assertContains(response, 'Цей час використовується в тижневому розкладі')
        self.assertTrue(LessonTime.objects.filter(pk=self.slot1.pk).exists())

    def test_unused_time_can_be_deleted_without_removing_dated_lessons(self):
        lesson = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 5), topic='Збережений урок', lesson_time=self.slot2.start_time)
        self.client.post(self.url('lesson_times'), {'slot_id': self.slot2.pk, 'action': 'delete'})
        self.assertFalse(LessonTime.objects.filter(pk=self.slot2.pk).exists())
        lesson.refresh_from_db()
        self.assertEqual(lesson.lesson_time, dt.time(9, 25))

    def test_week_starts_on_monday_and_has_seven_days(self):
        response = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-10'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['week_start'], dt.date(2026, 10, 5))
        self.assertEqual(response.context['week_end'], dt.date(2026, 10, 11))
        self.assertEqual(len(response.context['days']), 7)
        self.assertIn('week=2026-09-28', response.context['previous_url'])
        self.assertIn('week=2026-10-12', response.context['next_url'])

    def test_recurring_schedule_can_be_created_without_creating_journal_lessons(self):
        response = self.client.post(self.url('weekly_schedule'), self.schedule_post(), follow=True)
        self.assertEqual(response.status_code, 200)
        entry = WeeklyScheduleEntry.objects.get(journal=self.journal)
        self.assertEqual((entry.start_date, entry.end_date), (self.year.start_date, self.year.end_date))
        items = self.items(response)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['date'], dt.date(2026, 10, 5))
        self.assertIn('create_date=2026-10-05&create_time=08:30', items[0]['create_url'])
        self.assertFalse(Lesson.objects.filter(journal=self.journal).exists())

    def test_week_combines_recorded_lessons_and_schedule_without_duplicate_cards(self):
        self.entry(room='201')
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 5), topic='Урок уже внесений', lesson_time=self.slot1.start_time)
        response = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-05'})
        items = self.items(response)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['kind'], items[0]['topic']), ('recorded', 'Урок уже внесений'))
        self.assertEqual(items[0]['room'], '201')

    def test_untimed_lesson_is_visible_and_summaries_are_excluded(self):
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 6), topic='Урок без часу')
        Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 6), lesson_number=2, topic='Підсумок', lesson_type='THEMATIC')
        response = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-05'})
        self.assertEqual(len(self.items(response)), 1)
        self.assertContains(response, 'Без часу')
        self.assertNotContains(response, 'Підсумок')

    def test_only_own_teacher_lessons_and_schedule_are_displayed(self):
        self.entry()
        WeeklyScheduleEntry.objects.create(journal=self.colleague_journal, weekday=1, lesson_time=self.slot2,
            start_date=self.year.start_date, end_date=self.year.end_date)
        Lesson.objects.create(journal=self.colleague_journal, date=dt.date(2026, 10, 6), topic='Урок колеги', lesson_time=self.slot2.start_time)
        response = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-05'})
        self.assertEqual({item['journal'].teacher_id for item in self.items(response)}, {self.teacher.pk})
        self.assertNotContains(response, 'Урок колеги')
        self.assertEqual(self.client.get(self.url('weekly_schedule'), {'teacher_id': self.colleague.pk}).status_code, 403)

    def test_foreign_teacher_journal_or_time_cannot_be_added_to_schedule(self):
        for changes, error in (({'journal': self.colleague_journal.pk}, 'journal'),
                               ({'journal': self.foreign_journal.pk}, 'journal'),
                               ({'lesson_time': self.foreign_slot.pk}, 'lesson_time')):
            response = self.client.post(self.url('weekly_schedule'), self.schedule_post(**changes))
            self.assertIn(error, response.context['form'].errors)
        self.assertFalse(WeeklyScheduleEntry.objects.exists())

    def test_teacher_conflict_across_different_classes_is_rejected(self):
        self.entry()
        response = self.client.post(self.url('weekly_schedule'), self.schedule_post(journal=self.second_journal.pk))
        self.assertContains(response, 'Учитель або клас уже має урок у цей час')
        self.assertEqual(WeeklyScheduleEntry.objects.count(), 1)

    def test_class_conflict_across_different_teachers_is_rejected(self):
        self.entry()
        self.client.force_login(self.colleague)
        response = self.client.post(self.url('weekly_schedule'), self.schedule_post(
            teacher_id=self.colleague.pk, journal=self.colleague_journal.pk))
        self.assertContains(response, 'Учитель або клас уже має урок у цей час')
        self.assertEqual(WeeklyScheduleEntry.objects.count(), 1)

    def test_same_time_on_another_day_or_nonoverlapping_period_is_allowed(self):
        self.entry(end_date=dt.date(2026, 12, 31))
        for changes in ({'weekday': '1'}, {'start_date': '2027-01-01', 'end_date': '2027-06-30'}):
            response = self.client.post(self.url('weekly_schedule'), self.schedule_post(**changes))
            self.assertEqual(response.status_code, 302)
        self.assertEqual(WeeklyScheduleEntry.objects.count(), 3)

    def test_schedule_date_range_and_weekday_are_validated(self):
        for changes in ({'start_date': '2028-01-01'}, {'start_date': '2026-12-01', 'end_date': '2026-09-01'}, {'weekday': '7'}):
            response = self.client.post(self.url('weekly_schedule'), self.schedule_post(**changes))
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(WeeklyScheduleEntry.objects.exists())

    def test_entry_is_only_shown_during_its_valid_period(self):
        self.entry(start_date=dt.date(2026, 11, 1))
        before = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-05'})
        after = self.client.get(self.url('weekly_schedule'), {'week': '2026-11-02'})
        self.assertEqual(len(self.items(before)), 0)
        self.assertEqual(len(self.items(after)), 1)

    def test_edit_and_delete_schedule_preserve_actual_lessons(self):
        entry = self.entry()
        lesson = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 5), topic='У журналі', lesson_time=self.slot1.start_time)
        updated = self.client.post(self.url('weekly_schedule'), self.schedule_post(entry_id=entry.pk, weekday='1', room='202'))
        self.assertEqual(updated.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual((entry.weekday, entry.room), (1, '202'))
        deleted = self.client.post(self.url('weekly_schedule'), self.schedule_post(entry_id=entry.pk, action='delete'))
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(WeeklyScheduleEntry.objects.filter(pk=entry.pk).exists())
        lesson.refresh_from_db()

    def test_admin_can_choose_school_and_teacher(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url('weekly_schedule'), {'school_id': self.school.pk, 'teacher_id': self.teacher.pk, 'week': '2026-10-05'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_teacher'], self.teacher)
        self.assertEqual(self.client.get(self.url('weekly_schedule'), {'school_id': self.school.pk, 'teacher_id': self.outsider.pk}).status_code, 404)

    def test_malformed_scope_and_week_return_controlled_errors(self):
        for name, parameters in (('lesson_times', {'school_id': 'bad'}), ('lesson_times', {'edit': 'bad'}),
                                 ('weekly_schedule', {'year_id': 'bad'}), ('weekly_schedule', {'week': 'bad'})):
            self.assertIn(self.client.get(self.url(name), parameters).status_code, (400, 404))

    def test_lesson_forms_offer_school_time_selects_and_no_manual_time_input(self):
        response = self.client.get(reverse('portal:journal_detail', kwargs={'journal_id': self.journal.pk}))
        soup = BeautifulSoup(response.content, 'html.parser')
        for selector in ('#create_lesson_time', '#edit_time_input'):
            select = soup.select_one(selector)
            self.assertEqual(select.name, 'select')
            self.assertEqual([o['value'] for o in select.select('option')], ['', '08:30', '09:25'])
        self.assertIsNone(soup.select_one('input[name="lesson_time"][type="time"]'))

    def test_create_lesson_from_selected_school_time_and_reject_unlisted_time(self):
        url = reverse('portal:lesson_create', kwargs={'journal_id': self.journal.pk})
        post = {'date': '2026-10-05', 'topic': 'Створений за розкладом', 'lesson_type': 'ORDINARY',
                'school_time_list': '1', 'lesson_time': '08:30'}
        self.assertEqual(self.client.post(url, post).status_code, 302)
        lesson = self.journal.lessons.get()
        self.assertEqual(lesson.lesson_time, self.slot1.start_time)
        response = self.client.post(url, {**post, 'date': '2026-10-06', 'lesson_time': '10:20'}, follow=True)
        self.assertContains(response, 'Оберіть час зі шкільного списку')
        self.assertEqual(self.journal.lessons.count(), 1)

    def test_schedule_link_prefills_valid_date_and_time_only(self):
        url = reverse('portal:journal_detail', kwargs={'journal_id': self.journal.pk})
        response = self.client.get(url, {'create_date': '2026-10-05', 'create_time': '08:30'})
        self.assertEqual(response.context['create_lesson_defaults'], {'date': '2026-10-05', 'time': '08:30'})
        for parameters in ({'create_date': '2028-01-01', 'create_time': '08:30'}, {'create_date': '2026-10-05', 'create_time': '10:20'}):
            self.assertEqual(self.client.get(url, parameters).context['create_lesson_defaults'], {})

    def test_inactive_time_is_not_available_for_new_lessons_but_existing_time_can_be_preserved(self):
        self.entry()
        self.slot1.is_active = False
        self.slot1.save()
        schedule = self.client.get(self.url('weekly_schedule'), {'week': '2026-10-05'})
        self.assertFalse(self.items(schedule)[0]['can_create'])
        self.assertNotContains(schedule, '+ Створити урок')
        self.assertContains(schedule, 'Змінити заняття')
        lesson = Lesson.objects.create(journal=self.journal, date=dt.date(2026, 10, 5), topic='Попередній час', lesson_time=self.slot1.start_time)
        url = reverse('portal:lesson_edit', kwargs={'journal_id': self.journal.pk, 'lesson_id': lesson.pk})
        response = self.client.post(url, {'date': '2026-10-05', 'lesson_number': 1, 'topic': 'Нова тема',
            'lesson_type': 'ORDINARY', 'school_time_list': '1', 'lesson_time': '08:30'})
        self.assertEqual(response.status_code, 302)
        lesson.refresh_from_db()
        self.assertEqual((lesson.topic, lesson.lesson_time), ('Нова тема', dt.time(8, 30)))
