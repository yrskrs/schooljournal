"""Seed only the PostgreSQL database created by outside_grid.compose.yaml."""
import datetime as dt
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()
from django.conf import settings

database = settings.DATABASES['default']
if (database['HOST'], database['NAME'], database['USER']) != ('journaldb', 'outside_grid_checks', 'outside_check'):
    raise RuntimeError('Only the disposable outside-grid database is allowed')

from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, Semester, StudentClass, Subject
from apps.journals.models import (
    APIInboxResult,
    ExtraStudent,
    GradeAPIConnection,
    Journal,
    Lesson,
)
from apps.journals.services.extra_students import merge_extra
from apps.schools.models import School

school = School.objects.create(name='Synthetic Outside Grid School')
teacher = User.objects.create_user(username='outside_audit', password='synthetic-outside-password',
    role=User.Role.TEACHER, school=school)
year = AcademicYear.objects.create(school=school, name='2026/2027',
    start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 6, 30))
for number, start, end in [(1,dt.date(2026,9,1),dt.date(2026,12,31)),(2,dt.date(2027,1,1),dt.date(2027,6,30))]:
    Semester.objects.create(academic_year=year, number=number, start_date=start, end_date=end)
group = StudentClass.objects.create(school=school, academic_year=year, name='7-А')
subject = Subject.objects.create(school=school, name='Синтетичний предмет')
journal = Journal.objects.create(school=school, academic_year=year, student_class=group, subject=subject, teacher=teacher)
for date, topic in [(dt.date(2026,9,8),'Тема 8 вересня'),(dt.date(2026,9,9),'Тема 9 вересня'),(dt.date(2026,10,9),'Тема жовтня')]:
    Lesson.objects.create(journal=journal, date=date, lesson_number=1, topic=topic)
cache = {'instance_id': '11111111-1111-4111-8111-111111111111'}
old = GradeAPIConnection.objects.create(journal=journal, source='schooltest', name='Попереднє підключення',
    url='http://test:8000/api/v2/journal/grades/', roster_cache=cache, enabled=False)
new = GradeAPIConnection.objects.create(journal=journal, source='schooltest', name='Нове підключення',
    url=old.url, roster_cache=cache, enabled=True)
for index, name in enumerate(['Збережений Учень', 'Ще один Учень']):
    extra = ExtraStudent.objects.create(connection=old, external_key='outside:'+str(uuid.uuid4()), name=name, is_separate=True)
    for offset, date in enumerate(['2026-09-08','2026-09-09']):
        APIInboxResult.objects.create(connection=old, extra_student=extra, external_id=f'old-{index}-{offset}',
            payload={'date':date,'value':str(8+offset),'title':f'Тест {offset+1}'})
    alias = ExtraStudent.objects.create(connection=new, external_key='outside:'+str(uuid.uuid4()), name=name)
    APIInboxResult.objects.create(connection=new, extra_student=alias, external_id=f'new-{index}',
        payload={'date':'2026-09-09','value':'10','title':'Нова робота'})
    merge_extra(alias, extra, teacher)
print('Seeded disabled canonical identities, active aliases, six results and the real lesson calendar')
