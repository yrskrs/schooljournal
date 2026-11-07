from datetime import datetime,date,timezone as utc_timezone
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.urls import reverse
from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear,StudentClass,Student,ClassEnrollment
from apps.importers.services.student_service import StudentImportService

@override_settings(TIME_ZONE='Europe/Kyiv')
class KyivEnrollmentCalendarTests(TestCase):
    def setUp(self):
        self.school=School.objects.create(name='Вигадана школа часу')
        self.admin=User.objects.create_user(username='clock-admin',password='synthetic',role=User.Role.SCHOOL_ADMIN,school=self.school)
        self.year=AcademicYear.objects.create(school=self.school,name='2026/2027',start_date=date(2026,1,1),end_date=date(2027,6,30))
        self.a=StudentClass.objects.create(school=self.school,academic_year=self.year,name='7-А')
        self.b=StudentClass.objects.create(school=self.school,academic_year=self.year,name='7-Б')
        self.client.force_login(self.admin)

    def test_created_and_transferred_enrollments_use_kyiv_day_after_midnight(self):
        for instant,expected in [(datetime(2026,11,6,22,30,tzinfo=utc_timezone.utc),date(2026,11,7)),(datetime(2026,7,1,21,30,tzinfo=utc_timezone.utc),date(2026,7,2))]:
            with self.subTest(instant=instant),patch('django.utils.timezone.now',return_value=instant):
                name=f'Перевірка-{instant.month}'
                url=reverse('portal:superadmin_students')
                response=self.client.post(url,{'action':'create','last_name':name,'first_name':'Вигаданий','class_id':self.a.pk})
                self.assertEqual(response.status_code,302)
                pupil=Student.objects.get(school=self.school,last_name=name)
                old=ClassEnrollment.objects.get(student=pupil,student_class=self.a)
                self.assertEqual(old.start_date,expected)
                response=self.client.post(url,{'action':'transfer','student_id':pupil.pk,'new_class_id':self.b.pk})
                self.assertEqual(response.status_code,302)
                old.refresh_from_db()
                self.assertEqual(old.end_date,expected)
                self.assertEqual(ClassEnrollment.objects.get(student=pupil,student_class=self.b).start_date,expected)

    def test_import_uses_kyiv_enrollment_date(self):
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,6,22,30,tzinfo=utc_timezone.utc)):
            StudentImportService.execute_import([{'last_name':'Імпорт','first_name':'Вигаданий','class_id':self.a.pk}],self.school,user=self.admin)
        pupil=Student.objects.get(school=self.school,last_name='Імпорт')
        self.assertEqual(ClassEnrollment.objects.get(student=pupil).start_date,date(2026,11,7))

@override_settings(TIME_ZONE='Europe/Kyiv')
class KyivSemesterCalendarTests(TestCase):
    from tests.test_journal_workflows import JournalWorkflowsTests as _Fixture
    setUp=_Fixture.setUp

    def test_current_semester_fallback_uses_kyiv_day(self):
        with patch('django.utils.timezone.now',return_value=datetime(2026,12,31,22,30,tzinfo=utc_timezone.utc)):
            response=self.client.get(f'/journal/{self.journal.pk}/',{'semester_id':'999999'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['selected_semester'].pk,self.sem2.pk)
