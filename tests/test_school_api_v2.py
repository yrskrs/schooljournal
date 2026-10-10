import datetime as dt
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from apps.journals.models import GradeAPIConnection, GradeStudentMapping, Grade, ExternalGrade
from apps.journals.services.external_grades import import_grade_records, ExternalAPIError
from school_sync.django_backend import operate, cycle
from tests import test_journal_workflows as workflows

class SchoolAPIV2Tests(TestCase):
    def setUp(self):
        workflows.JournalWorkflowsTests.setUp(self)
        self.connection=GradeAPIConnection.objects.create(journal=self.journal,name='Вигадані тести', source='schooltest',contract_version=2,
            source_class_id='7',source_subject_id='3',url='http://schooltest-api:8000/api/v2/journal/grades/',api_key='synthetic')
        GradeStudentMapping.objects.create(connection=self.connection,external_id='41',student=self.student)
    def row(self, **extra):
        return dict(source='schooltest',id='synthetic-session-student',student_id=41,class_id=7,subject_id=3,work_id=9,
            title='Вигаданий тест',points=8,max_points=12,value='8',grading_scale=12,date=self.l1.date.isoformat(),
            status='final',updated_at='2026-10-10T12:00:00+03:00',attempt_id=1,is_ai=False,approved_by_teacher=True,**extra)
    def pull(self, row):
        return import_grade_records(self.connection,[row],self.teacher,self.year.start_date,self.year.end_date)
    def test_repeat_and_correction_update_one_import(self):
        self.assertEqual(self.pull(self.row()).counts['added'],1)
        self.assertEqual(self.pull(self.row()).counts['unchanged'],1)
        row=self.row();row.update(value='10',points=10,updated_at='2026-10-10T13:00:00+03:00',attempt_id=2)
        self.assertEqual(self.pull(row).counts['updated'],1)
        self.assertEqual(Grade.objects.count(),1);self.assertEqual(Grade.objects.get().value,'10');self.assertEqual(ExternalGrade.objects.count(),1)
        self.assertEqual(self.pull(self.row()).counts['errors'],1);self.assertEqual(Grade.objects.get().value,'10')
    def test_manual_grade_and_manual_edit_are_preserved(self):
        Grade.objects.create(lesson=self.l1,student=self.student,value='11',created_by=self.teacher)
        self.assertEqual(self.pull(self.row()).counts['conflicts'],1);self.assertEqual(Grade.objects.get().value,'11')
        Grade.objects.all().delete();ExternalGrade.objects.all().delete();self.pull(self.row())
        self.client.post(reverse('portal:update_grade',kwargs={'journal_id':self.journal.pk}),{'lesson_id':self.l1.pk,'student_id':self.student.pk,'value':'8'})
        row=self.row();row.update(value='10',points=10,updated_at='2026-10-10T13:00:00+03:00')
        self.assertEqual(self.pull(row).counts['conflicts'],1);self.assertEqual(Grade.objects.get().value,'8')
    def test_only_ready_integer_marks_and_explicit_context(self):
        for changes in ({'value':'8.5'},{'value':'В'},{'value':0},{'grading_scale':100},{'status':'draft'},{'approved_by_teacher':False},{'class_id':8},{'subject_id':4},{'student_id':99}):
            row=self.row();row.update(changes)
            self.assertEqual(self.pull(row).counts['errors'],1,changes)
        self.assertEqual(Grade.objects.count(),0)
    def test_buttons_separate_sources_and_get_has_no_import(self):
        url=reverse('portal:journal_detail',kwargs={'journal_id':self.journal.pk})
        with patch('apps.portal.views_integrations.fetch_grade_payload',return_value=[self.row()]) as remote:
            page=self.client.get(url,{'month':'2026-09'})
            self.assertContains(page,'Синхронізувати з тестами');self.assertContains(page,'Синхронізувати із завданнями')
            remote.assert_not_called();self.assertFalse(Grade.objects.exists())
            url=reverse('portal:pull_source_grades',kwargs={'journal_id':self.journal.pk,'source':'schoolwork'})
            self.client.post(url,{'date_from':'2026-09-01','date_to':'2026-09-30'});remote.assert_not_called()
            url=reverse('portal:pull_source_grades',kwargs={'journal_id':self.journal.pk,'source':'schooltest'})
            self.assertEqual(self.client.get(url).status_code,405)
            self.client.post(url,{'date_from':'2026-09-01','date_to':'2026-09-30'});self.assertEqual(remote.call_count,1)
            self.assertEqual(Grade.objects.get().value,'8')
    def test_source_failure_and_access(self):
        url=reverse('portal:pull_source_grades',kwargs={'journal_id':self.journal.pk,'source':'schooltest'})
        with patch('apps.portal.views_integrations.fetch_grade_payload',side_effect=ExternalAPIError('Джерело недоступне')):
            self.assertEqual(self.client.post(url,{'date_from':'2026-09-01','date_to':'2026-09-30'}).status_code,302)
        self.assertFalse(Grade.objects.exists())
        self.client.logout();self.assertEqual(self.client.post(url).status_code,302)
    def test_roster_local_copy_and_permissions(self):
        with override_settings(ROSTER_SCHOOL_ID=self.school.pk,ROSTER_YEAR_ID=self.year.pk,ROSTER_SYNC_TOKEN='synthetic-roster',ROSTER_PEERS=[]):
            operate(lambda engine,store:engine.share('class',self.group.pk))
            url=reverse('portal:roster_sync_api')
            self.assertEqual(self.client.get(url).status_code,401)
            payload=self.client.get(url,HTTP_AUTHORIZATION='Bearer synthetic-roster').json()
            self.assertEqual(payload['schema_version'],2);self.assertEqual(len(payload['records']),3)
            self.assertEqual(self.client.get(reverse('portal:roster_sync')).status_code,200)
            cycle();self.assertFalse(Grade.objects.exists())
