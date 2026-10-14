"""Outside grid preserves saved identities, dates and disabled-connection history."""
import datetime as dt
import uuid

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.curriculum.models import ClassEnrollment, Student, StudentClass
from apps.journals.models import (
    APIInboxResult,
    ExtraStudent,
    Grade,
    Journal,
    Lesson,
)
from apps.journals.services.extra_students import keep_separate, merge_extra
from tests import test_simple_exchange as exchange


class OutsideGradebookTests(TestCase):
    setUp = exchange.SimpleExchangeTests.setUp
    connection = exchange.SimpleExchangeTests.connection
    row = exchange.SimpleExchangeTests.row
    pull = exchange.SimpleExchangeTests.pull

    def page(self, **filters):
        return self.client.get(reverse('portal:outside_students'), {'journal': self.journal.pk, **filters})

    def identity(self, connection=None, **values):
        connection = connection or self.connection()
        values.setdefault('source', connection.source)
        identity = str(uuid.uuid4())
        self.pull(connection, self.row(id='work-'+identity, student_key='outside:'+identity,
            outside_student_id=identity, **values))
        return ExtraStudent.objects.get(connection=connection, external_key='outside:'+identity)

    def test_disabled_canonical_identity_and_active_alias_are_visible_with_all_results(self):
        old = self.connection()
        canonical = self.identity(old)
        canonical.is_separate = True; canonical.save()
        new = self.connection()
        alias = self.identity(new, title='Нова робота', value='10')
        merge_extra(alias, canonical, self.teacher)
        old.enabled = False; old.save()
        before = {model: model.objects.count() for model in (StudentClass, Student, Journal, Lesson, Grade)}
        page = self.page()
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context['tables'][0]['rows']), 1)
        row = page.context['tables'][0]['rows'][0]
        marks = [mark for cell in row['cells'] for mark in cell]
        self.assertEqual({mark['value'] for mark in marks}, {'8', '10'})
        self.assertContains(page, 'Підключення призупинено')
        self.assertContains(page, 'outside-grades-table')
        inbox = self.client.get(reverse('portal:api_inbox', args=[self.journal.pk]), {'student': canonical.pk, 'all': '1'})
        self.assertContains(inbox, canonical.name)
        for model, count in before.items():
            self.assertEqual(model.objects.count(), count)

    def test_standalone_rows_follow_calendar_and_multiple_works_share_a_cell(self):
        extra = self.identity()
        APIInboxResult.objects.create(connection=extra.connection, extra_student=extra, external_id='second',
            payload=self.row(id='second', value='9', title='Друга робота'))
        page = self.page()
        table = page.context['tables'][0]
        self.assertEqual([column['date'] for column in table['columns']], [self.l1.date, self.l2.date, self.l3.date])
        self.assertEqual({mark['value'] for mark in table['rows'][0]['cells'][0]}, {'8', '9'})
        soup = BeautifulSoup(page.content, 'html.parser')
        self.assertEqual(len(soup.select('.outside-grades-table tbody tr')), 1)
        self.assertEqual(len(soup.select('.outside-grade-mark')), 2)
        self.assertContains(page, self.l1.topic)
        self.assertContains(page, self.l3.topic)

    def test_result_without_calendar_lesson_remains_visible_in_its_date_column(self):
        self.identity(date='2026-09-08', title='Робота без уроку')
        table = self.page().context['tables'][0]
        column = next(column for column in table['columns'] if column['date'] == dt.date(2026, 9, 8))
        self.assertFalse(column['is_lesson'])
        self.assertEqual(column['number'], 0)
        self.assertContains(self.page(), 'Робота без уроку')

    def test_ambiguous_same_day_does_not_guess_a_lesson_number(self):
        Lesson.objects.create(journal=self.journal, date=self.l1.date, lesson_number=2, topic='Другий урок')
        self.identity()
        table = self.page().context['tables'][0]
        self.assertEqual([column['number'] for column in table['columns'] if column['date'] == self.l1.date], [0, 1, 2])
        self.assertEqual(len(table['rows'][0]['cells'][0]), 1)

    def test_explicit_lesson_number_and_month_semester_filters(self):
        self.identity(date=self.l3.date.isoformat(), lesson_number=1, value='11')
        page = self.page(month='2027-02', semester_id=self.sem2.pk)
        table = page.context['tables'][0]
        self.assertEqual(len(table['columns']), 1)
        self.assertEqual(table['columns'][0]['date'], self.l3.date)
        self.assertEqual(table['rows'][0]['cells'][0][0]['value'], '11')
        page = self.page(month='invalid', semester_id=self.sem1.pk)
        self.assertEqual(len(page.context['tables'][0]['columns']), 2)

    def test_search_source_and_ignored_results_do_not_leak_other_rows(self):
        tests = self.connection()
        work = self.connection(); work.source = 'schoolwork'; work.save()
        self.identity(tests, student_name='Учень Тестів')
        extra = self.identity(work, student_name='Учень Завдань', title='Завдання')
        page = self.page(source='schoolwork', q='Завдань')
        self.assertContains(page, 'Учень Завдань')
        self.assertNotContains(page, 'Учень Тестів')
        extra.results.update(is_ignored=True)
        page = self.page(source='schoolwork')
        self.assertNotContains(page, 'Учень Завдань')
        self.assertEqual(page.context['tables'][0]['rows'], [])

    def test_equal_names_with_different_stable_ids_stay_separate(self):
        connection = self.connection()
        self.identity(connection, student_name='Тезка Учень')
        self.identity(connection, student_name='Тезка Учень')
        table = self.page().context['tables'][0]
        self.assertEqual(len(table['rows']), 2)
        self.assertNotEqual(table['rows'][0]['extra'].pk, table['rows'][1]['extra'].pk)

    def test_existing_separate_grade_keeps_manual_value_and_edit_link(self):
        extra = self.identity()
        legacy, _ = keep_separate(extra, self.teacher)
        grade = Grade.objects.get(student=extra.separate_student)
        grade.value = '12'; grade.save()
        page = self.page()
        table = page.context['tables'][0]
        self.assertEqual(len(table['rows']), 1)
        row = table['rows'][0]
        self.assertEqual(row['cells'][0][0]['value'], '12')
        self.assertTrue(row['cells'][0][0]['is_saved_grade'])
        self.assertContains(page, reverse('portal:journal_detail', args=[legacy.pk]))
        self.assertEqual(Grade.objects.get(pk=grade.pk).value, '12')

    def test_unlinked_legacy_student_and_grade_remain_visible(self):
        group = StudentClass.objects.create(school=self.school, academic_year=self.year,
            name=self.group.name+' (Позакласні)', letter='ПК')
        legacy = Journal.objects.create(school=self.school, academic_year=self.year,
            student_class=group, subject=self.subject, teacher=self.teacher)
        pupil = Student.objects.create(school=self.school, first_name='Учень', last_name='Історичний')
        ClassEnrollment.objects.create(student=pupil, student_class=group)
        lesson = Lesson.objects.create(journal=legacy, date=self.l1.date, lesson_number=1, topic='Історія')
        Grade.objects.create(lesson=lesson, student=pupil, value='7')
        page = self.page()
        self.assertContains(page, pupil.get_full_name_ukr())
        self.assertEqual(page.context['tables'][0]['rows'][0]['cells'][0][0]['value'], '7')

    def test_empty_selected_journal_still_shows_calendar_and_clear_state(self):
        page = self.page()
        self.assertContains(page, 'outside-grades-table')
        self.assertContains(page, 'Окремих учнів за цими фільтрами немає')
        self.assertContains(page, self.l1.topic)
        self.assertEqual(page.context['tables'][0]['rows'], [])

    def test_anonymous_or_other_teacher_cannot_open_journal_history(self):
        self.identity()
        self.client.logout()
        self.assertEqual(self.page().status_code, 302)
        stranger = User.objects.create_user(username='outside-grid-stranger', role=User.Role.TEACHER, school=self.school)
        self.client.force_login(stranger)
        self.assertEqual(self.page().status_code, 403)
        response = self.client.get(reverse('portal:outside_students'))
        self.assertEqual(response.context['tables'], [])

    def test_same_source_work_in_two_connections_is_displayed_once(self):
        identity = str(uuid.uuid4())
        for connection in (self.connection(), self.connection()):
            self.pull(connection, self.row(student_key='outside:'+identity, outside_student_id=identity))
        table = self.page().context['tables'][0]
        self.assertEqual(len(table['rows']), 1)
        self.assertEqual(len(table['rows'][0]['cells'][0]), 1)
