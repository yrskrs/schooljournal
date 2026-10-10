import os, datetime as dt
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, StudentClass, Student, ClassEnrollment, Subject
from apps.journals.models import Journal, Lesson, GradeAPIConnection, GradeStudentMapping
school=School.objects.create(name='Вигадана школа')
teacher=User.objects.create_user(username='synthetic_teacher',password='synthetic-test',school=school,role=User.Role.TEACHER)
User.objects.create_superuser(username='synthetic_admin',password='synthetic-test',role=User.Role.SUPERADMIN)
year=AcademicYear.objects.create(school=school,name='2026/2027',start_date=dt.date(2026,9,1),end_date=dt.date(2027,6,30))
group=StudentClass.objects.create(school=school,academic_year=year,name='7-А',grade_level=7,letter='А')
subject=Subject.objects.create(school=school,name='Вигаданий предмет')
pupil=Student.objects.create(school=school,first_name='Учень',last_name='Вигаданий')
ClassEnrollment.objects.create(student=pupil,student_class=group,start_date=year.start_date,is_active=True)
journal=Journal.objects.create(school=school,academic_year=year,student_class=group,subject=subject,teacher=teacher)
for date in (dt.date(2026,9,5),dt.date(2026,10,9),dt.date(2026,9,6),dt.date(2026,9,9)):
    Lesson.objects.create(journal=journal,date=date,lesson_number=1,topic='Вигаданий урок')
for source,url,token in [('schooltest','http://test:8000/api/v2/journal/grades/','synthetic-test-grades'),('schoolwork','http://work:8000/api/v2/journal/grades/','synthetic-work-grades')]:
    connection=GradeAPIConnection.objects.create(journal=journal,name='Вигадане джерело '+source,url=url,api_key=token,source=source,contract_version=2,source_class_id='1',source_subject_id='1')
    GradeStudentMapping.objects.create(connection=connection,external_id='1',student=pupil)
print('Synthetic journal fixture ready')
