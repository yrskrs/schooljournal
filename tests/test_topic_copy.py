import datetime as dt
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.db import IntegrityError
from django.test import TestCase, Client
from django.urls import reverse

from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, StudentClass, Subject, Student
from apps.journals.models import Journal, Lesson, LessonPlanTopic, Grade, Attendance, AuditLog, SummaryGrade


class TopicCopyTests(TestCase):
    """All records are fictional; these requests never use the working database."""
    def setUp(self):
        self.school = School.objects.create(name='Вигадана школа')
        self.teacher = User.objects.create_user(username='topic_copy_teacher', role=User.Role.TEACHER, school=self.school)
        self.year = AcademicYear.objects.create(school=self.school, name='2026/2027',
            start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 6, 30))
        self.subject = Subject.objects.create(school=self.school, name='Вигаданий предмет')
        self.class_a = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Клас А')
        self.class_b = StudentClass.objects.create(school=self.school, academic_year=self.year, name='Клас Б')
        self.source = Journal.objects.create(school=self.school, academic_year=self.year,
            subject=self.subject, student_class=self.class_a, teacher=self.teacher)
        self.target = Journal.objects.create(school=self.school, academic_year=self.year,
            subject=self.subject, student_class=self.class_b, teacher=self.teacher, nz_journal_id='fictional-target')
        self.sources = [Lesson.objects.create(journal=self.source, date=date, lesson_number=number,
            topic=f'Тема А{i}', homework=f'ДЗ А{i}', notes='FICTIONAL_PRIVATE_SOURCE_NOTE', lesson_type='QUIZ',
            nz_schedule_id=f'fictional-source-{i}', is_synced_to_nz=True)
            for i, (date, number) in enumerate([
                (dt.date(2026, 9, 2), 1), (dt.date(2026, 9, 4), 1), (dt.date(2026, 9, 4), 2)], start=1)]
        self.source_summary = Lesson.objects.create(journal=self.source, date=dt.date(2026, 9, 3),
            lesson_number=1, topic='Вигадана тематична', lesson_type='THEMATIC')
        self.targets = [Lesson.objects.create(journal=self.target, date=date, lesson_number=number,
            lesson_time=dt.time(11, 30), topic=f'Тема Б{i}', homework=f'ДЗ Б{i}', notes='Примітка Б',
            homework_due_date=dt.date(2026, 10, 20), lesson_type='ORDINARY',
            nz_schedule_id=f'fictional-target-{i}', is_synced_to_nz=True,
            nz_last_synced_topic=f'Тема Б{i}', nz_last_synced_homework=f'ДЗ Б{i}')
            for i, (date, number) in enumerate([
                (dt.date(2026, 9, 12), 1), (dt.date(2026, 10, 15), 1), (dt.date(2026, 10, 15), 2)], start=1)]
        self.url = reverse('portal:topics_copy', kwargs={'journal_id': self.target.pk})
        self.client.force_login(self.teacher)

    def data(self, **kwargs):
        return {'source_journal': str(self.source.pk), 'source_kind': 'lessons', 'source_start': '1',
                'source_end': '3', 'target_kind': 'lessons', 'target_start': '1', 'replace_existing': 'on', **kwargs}

    def preview(self, data):
        response = self.client.post(self.url, {**data, 'action': 'preview'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context['rows'], response.context['form'].errors)
        return response

    def save(self, data):
        preview = self.preview(data)
        return self.client.post(self.url, {**data, 'action': 'save', 'preview_token': preview.context['preview_token']})

    def test_topics_map_by_whole_journal_sequence_across_different_days(self):
        data = self.data()
        preview = self.preview(data)
        self.assertEqual([(r['source_number'], r['target_number']) for r in preview.context['rows']], [(1, 1), (2, 2), (3, 3)])
        self.assertEqual([l.topic for l in self.target.lessons.all()], ['Тема Б1', 'Тема Б2', 'Тема Б3'])
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network request')) as network:
            response = self.client.post(self.url, {**data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        network.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.target.lessons.values_list('topic', flat=True)), ['Тема А1', 'Тема А2', 'Тема А3'])
        self.assertEqual(list(self.source.lessons.exclude(lesson_type='THEMATIC').values_list('topic', flat=True)), ['Тема А1', 'Тема А2', 'Тема А3'])

    def test_copy_preserves_target_dates_marks_attendance_type_and_online_binding(self):
        pupil = Student.objects.create(school=self.school, first_name='Учень', last_name='Вигаданий')
        Grade.objects.create(lesson=self.sources[0], student=pupil, value='12')
        grade = Grade.objects.create(lesson=self.targets[0], student=pupil, value='8', is_synced_to_nz=True, nz_last_synced_value='8')
        attendance = Attendance.objects.create(lesson=self.targets[1], student=pupil, status='ABSENT_EXCUSED', is_synced_to_nz=True)
        summary = SummaryGrade.objects.create(journal=self.target, student=pupil, grade_type='ANNUAL', value='9')
        plan = LessonPlanTopic.objects.create(journal=self.target, topic_number=1, title='План Б', is_conducted=True)
        self.targets[0].linked_plan_topic = plan
        self.targets[0].save()
        self.assertEqual(self.save(self.data()).status_code, 302)
        for i, lesson in enumerate(self.targets, start=1):
            lesson.refresh_from_db()
            self.assertEqual((lesson.lesson_time, lesson.lesson_type, lesson.homework, lesson.notes),
                (dt.time(11, 30), 'ORDINARY', f'ДЗ Б{i}', 'Примітка Б'))
            self.assertEqual(lesson.nz_schedule_id, f'fictional-target-{i}')
            self.assertEqual(lesson.nz_last_synced_topic, f'Тема Б{i}')
            self.assertEqual(lesson.homework_due_date, dt.date(2026, 10, 20))
            self.assertFalse(lesson.is_synced_to_nz)
        self.assertEqual([(l.date, l.lesson_number) for l in self.targets],
            [(dt.date(2026, 9, 12), 1), (dt.date(2026, 10, 15), 1), (dt.date(2026, 10, 15), 2)])
        self.assertEqual(self.targets[0].linked_plan_topic_id, plan.pk)
        grade.refresh_from_db(); attendance.refresh_from_db(); summary.refresh_from_db()
        self.assertEqual((grade.lesson_id, grade.value, grade.is_synced_to_nz), (self.targets[0].pk, '8', True))
        self.assertEqual((attendance.lesson_id, attendance.status, attendance.is_synced_to_nz), (self.targets[1].pk, 'ABSENT_EXCUSED', True))
        self.assertEqual(summary.value, '9')
        self.assertEqual(Grade.objects.count(), 2)
        self.assertEqual(Attendance.objects.count(), 1)

    def test_selected_range_and_target_offset(self):
        self.assertEqual(self.save(self.data(source_start='2', target_start='1')).status_code, 302)
        self.assertEqual(list(self.target.lessons.values_list('topic', flat=True)), ['Тема А2', 'Тема А3', 'Тема Б3'])

    def test_homework_copy_transfers_text_and_preserves_target_assignment(self):
        self.sources[0].homework_due_lesson = self.sources[2]
        self.sources[0].save()
        self.assertEqual(self.save(self.data(include_homework='on')).status_code, 302)
        for i, lesson in enumerate(self.targets, start=1):
            lesson.refresh_from_db()
            self.assertEqual(lesson.homework, f'ДЗ А{i}')
            self.assertEqual(lesson.homework_due_date, dt.date(2026, 10, 20))
            self.assertIsNone(lesson.homework_due_lesson_id)

    def test_copy_to_empty_plan_creates_undated_unconducted_topics(self):
        data = self.data(target_kind='plan', source_end='', include_homework='on')
        self.assertEqual(self.save(data).status_code, 302)
        copied = list(self.target.plan_topics.all())
        self.assertEqual([t.topic_number for t in copied], [1, 2, 3])
        self.assertEqual([t.title for t in copied], ['Тема А1', 'Тема А2', 'Тема А3'])
        for i, topic in enumerate(copied, start=1):
            self.assertIsNone(topic.planned_date)
            self.assertFalse(topic.is_conducted)
            self.assertEqual(topic.homework, f'ДЗ А{i}')
            self.assertEqual(topic.notes, '')
            self.assertEqual(topic.lesson_type, 'QUIZ')
        self.assertEqual(self.target.lessons.count(), 3)

    def test_plan_source_uses_explicit_numbers_and_preserves_number_gaps(self):
        for number in (2, 4):
            LessonPlanTopic.objects.create(journal=self.source, topic_number=number, title=f'План А{number}',
                planned_date=dt.date(2026, 9, number), homework=f'ДЗ плану {number}', notes='SOURCE_PLAN_PRIVATE_NOTE',
                is_conducted=number == 2, lesson_type='LAB')
        LessonPlanTopic.objects.create(journal=self.source, topic_number=3, title='Тематична плану', lesson_type='THEMATIC')
        data = self.data(source_kind='plan', source_start='2', source_end='4', target_kind='plan', target_start='7')
        preview = self.preview(data)
        self.assertEqual([(r['source_number'], r['target_number']) for r in preview.context['rows']], [(2, 7), (4, 9)])
        self.assertEqual(self.save(data).status_code, 302)
        self.assertEqual(list(self.target.plan_topics.values_list('topic_number', 'title', 'planned_date')),
                         [(7, 'План А2', None), (9, 'План А4', None)])

    def test_existing_plan_retains_its_date_type_notes_and_homework(self):
        topic = LessonPlanTopic.objects.create(journal=self.target, topic_number=1, title='Стара тема',
            planned_date=dt.date(2026, 12, 1), lesson_type='PRACTICAL', notes='Примітка плану Б', homework='ДЗ плану Б')
        self.assertEqual(self.save(self.data(source_end='1', target_kind='plan')).status_code, 302)
        topic.refresh_from_db()
        self.assertEqual((topic.title, topic.planned_date, topic.lesson_type, topic.notes, topic.homework),
            ('Тема А1', dt.date(2026, 12, 1), 'PRACTICAL', 'Примітка плану Б', 'ДЗ плану Б'))

    def test_filled_topics_require_explicit_replacement_choice(self):
        data = self.data(replace_existing='')
        preview = self.preview(data)
        self.assertEqual(preview.context['counts'], {'create': 0, 'update': 0, 'skip': 3})
        self.assertNotContains(preview, 'value="save"')
        response = self.client.post(self.url, {**data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.target.lessons.get(pk=self.targets[0].pk).topic, 'Тема Б1')
        self.assertFalse(AuditLog.objects.exists())

    def test_blank_topics_can_be_filled_without_replacement_choice(self):
        self.targets[0].topic = ''
        self.targets[0].save()
        self.assertEqual(self.save(self.data(source_end='1', replace_existing='')).status_code, 302)
        self.targets[0].refresh_from_db()
        self.assertEqual(self.targets[0].topic, 'Тема А1')

    def test_conducted_and_summary_plan_topics_are_skipped(self):
        conducted = LessonPlanTopic.objects.create(journal=self.target, topic_number=1, title='Проведено', is_conducted=True)
        summary = LessonPlanTopic.objects.create(journal=self.target, topic_number=2, title='Підсумок', lesson_type='SEMESTER_1')
        preview = self.preview(self.data(target_kind='plan'))
        self.assertEqual(preview.context['counts'], {'create': 1, 'update': 0, 'skip': 2})
        self.assertEqual(self.save(self.data(target_kind='plan')).status_code, 302)
        conducted.refresh_from_db(); summary.refresh_from_db()
        self.assertEqual((conducted.title, summary.title), ('Проведено', 'Підсумок'))

    def test_source_choices_exclude_other_subjects_and_schools_and_self(self):
        other_subject = Subject.objects.create(school=self.school, name='Інший вигаданий предмет')
        unrelated = Journal.objects.create(school=self.school, academic_year=self.year, student_class=self.class_a,
            subject=other_subject, teacher=self.teacher)
        other_school = School.objects.create(name='Інша вигадана школа')
        foreign = Journal.objects.create(school=other_school, academic_year=self.year, student_class=self.class_b,
            subject=other_subject, teacher=self.teacher)
        response = self.client.get(self.url)
        self.assertEqual(list(response.context['form'].fields['source_journal'].queryset.values_list('pk', flat=True)), [self.source.pk])
        for journal in (unrelated, foreign, self.target):
            with self.subTest(source=journal.pk):
                response = self.client.post(self.url, {**self.data(source_journal=str(journal.pk)), 'action': 'preview'})
                self.assertIsNone(response.context['rows'])
                self.assertIn('source_journal', response.context['form'].errors)
        self.assertFalse(AuditLog.objects.exists())

    def test_target_write_access_and_archive_protection(self):
        other = User.objects.create_user(username='different_fictional_teacher', role=User.Role.TEACHER, school=self.school)
        self.target.teacher = other
        self.target.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {**self.data(), 'action': 'save'}).status_code, 403)
        self.target.teacher = self.teacher
        self.target.is_archived = True
        self.target.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.source.is_archived = True
        self.source.save()
        self.target.is_archived = False
        self.target.save()
        self.assertEqual(self.save(self.data()).status_code, 302)

    def test_save_requires_matching_signed_preview(self):
        for token in ('', 'tampered-token'):
            response = self.client.post(self.url, {**self.data(), 'action': 'save', 'preview_token': token})
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.context['rows'])
        preview = self.preview(self.data())
        response = self.client.post(self.url, {**self.data(include_homework='on'), 'action': 'save',
            'preview_token': preview.context['preview_token']})
        self.assertIsNone(response.context['rows'])
        self.assertFalse(AuditLog.objects.exists())

    def test_source_or_target_edit_invalidates_preview(self):
        for record in (self.sources[0], self.targets[0]):
            with self.subTest(journal=record.journal_id):
                preview = self.preview(self.data())
                record.topic += ' (зміна)'
                record.save()
                response = self.client.post(self.url, {**self.data(), 'action': 'save', 'preview_token': preview.context['preview_token']})
                self.assertIsNone(response.context['rows'])
                self.assertFalse(AuditLog.objects.exists())

    def test_numbering_change_invalidates_preview(self):
        preview = self.preview(self.data())
        Lesson.objects.create(journal=self.source, date=dt.date(2026, 9, 1), lesson_number=1, topic='Нова перша тема')
        response = self.client.post(self.url, {**self.data(), 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertIsNone(response.context['rows'])
        self.assertFalse(AuditLog.objects.exists())

    def test_missing_destination_lesson_rejects_entire_range(self):
        self.targets[2].delete()
        response = self.client.post(self.url, {**self.data(), 'action': 'preview'})
        self.assertIsNone(response.context['rows'])
        self.assertContains(response, 'немає уроку №3')
        self.assertFalse(AuditLog.objects.exists())

    def test_duplicate_plan_numbers_reject_copy(self):
        for title in ('Одна тема', 'Інша тема'):
            LessonPlanTopic.objects.create(journal=self.target, topic_number=1, title=title)
        response = self.client.post(self.url, {**self.data(target_kind='plan'), 'action': 'preview'})
        self.assertIsNone(response.context['rows'])
        self.assertContains(response, 'повторюється номер 1')
        self.assertFalse(AuditLog.objects.exists())

    def test_failure_rolls_back_all_topic_updates_and_audit_records(self):
        data = self.data()
        preview = self.preview(data)
        create = AuditLog.objects.create
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise IntegrityError('Synthetic transaction failure')
            return create(**kwargs)
        with patch('apps.journals.services.topic_copy.AuditLog.objects.create', side_effect=fail_second):
            response = self.client.post(self.url, {**data, 'action': 'save', 'preview_token': preview.context['preview_token']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.target.lessons.values_list('topic', flat=True)), ['Тема Б1', 'Тема Б2', 'Тема Б3'])
        self.assertTrue(all(self.target.lessons.values_list('is_synced_to_nz', flat=True)))
        self.assertFalse(AuditLog.objects.exists())

    def test_preview_contains_no_pupil_data_or_source_notes_and_escapes_topics(self):
        pupil = Student.objects.create(school=self.school, first_name='PRIVATE_FICTIONAL_PUPIL', last_name='PRIVATE_FICTIONAL_SURNAME')
        Grade.objects.create(lesson=self.sources[0], student=pupil, value='12')
        self.sources[0].topic = '<img src=x onerror="fictional">'
        self.sources[0].save()
        preview = self.preview(self.data())
        self.assertNotContains(preview, 'PRIVATE_FICTIONAL_PUPIL')
        self.assertNotContains(preview, 'FICTIONAL_PRIVATE_SOURCE_NOTE')
        self.assertNotContains(preview, 'fictional-source-1')
        self.assertNotContains(preview, '<img src=x')
        self.assertContains(preview, '&lt;img src=x')

    def test_calendar_displays_whole_journal_numbers_after_month_filter(self):
        Lesson.objects.create(journal=self.target, date=dt.date(2026, 10, 2), lesson_number=1,
            topic='Підсумкова колонка', lesson_type='THEMATIC')
        url = reverse('portal:journal_detail', kwargs={'journal_id': self.target.pk})
        response = self.client.get(url, {'month': '2026-10'})
        soup = BeautifulSoup(response.content, 'html.parser')
        numbers = [row.select_one('td').text.strip() for row in soup.select('.calendar-lesson-row')]
        self.assertEqual(numbers, ['—', '2', '3'])
        self.assertTrue(soup.select_one(f'a[href="{self.url}"]'))

    def test_anonymous_access_and_csrf_protection(self):
        self.assertEqual(Client().get(self.url).status_code, 302)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.teacher)
        self.assertEqual(client.post(self.url, {**self.data(), 'action': 'save'}).status_code, 403)

    def test_invalid_range_and_number_overflow_have_no_partial_writes(self):
        invalid = [
            {'source_start': '0'}, {'source_start': '3', 'source_end': '2'},
            {'source_start': '10', 'source_end': ''}, {'target_start': '2147483647', 'target_kind': 'plan'},
            {'source_kind': 'unknown'}, {'target_kind': 'unknown'},
        ]
        for values in invalid:
            with self.subTest(values=values):
                response = self.client.post(self.url, {**self.data(**values), 'action': 'preview'})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context['rows'])
                self.assertFalse(AuditLog.objects.exists())
                self.assertFalse(self.target.plan_topics.exists())
                self.assertEqual(self.target.lessons.get(pk=self.targets[0].pk).topic, 'Тема Б1')

    def test_identical_topics_remain_online_confirmed_and_need_no_write(self):
        for source, target in zip(self.sources, self.targets):
            target.topic = source.topic
            target.save()
        preview = self.preview(self.data())
        self.assertEqual(preview.context['counts'], {'create': 0, 'update': 0, 'skip': 3})
        self.assertNotContains(preview, 'value="save"')
        self.assertTrue(all(self.target.lessons.values_list('is_synced_to_nz', flat=True)))
        self.assertFalse(AuditLog.objects.exists())

    def test_empty_target_defaults_to_plan_without_dates(self):
        self.target.lessons.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.context['form']['target_kind'].value(), 'plan')
        self.assertEqual(self.save(self.data(target_kind='plan')).status_code, 302)
        self.assertEqual(self.target.plan_topics.count(), 3)
        self.assertFalse(self.target.lessons.exists())
