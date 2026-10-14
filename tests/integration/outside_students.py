"""Run after run.py, only in the named disposable PostgreSQL Compose project."""
import json
import os
import uuid
import run as h


def main():
    if os.environ.get('COMPOSE_PROJECT_NAME') != 'outside-students-check':
        raise RuntimeError('Only COMPOSE_PROJECT_NAME=outside-students-check is allowed')
    first, twin = str(uuid.uuid4()), str(uuid.uuid4())
    work = json.loads(h.code('work', f'''
from feed.models import Teacher,ClassGroup,Subject,Assignment,Submission
from django.utils import timezone
t=Teacher.objects.get(user__username='synthetic_teacher')
c=ClassGroup.objects.create(name='8-З',grade=8,letter='З');t.classes.add(c)
s=Subject.objects.create(name='Окремий предмет');t.subjects.add(s)
for i in range(2):
 a=Assignment.objects.create(teacher=t,subject=s,title='Окрема робота '+str(i),due_date='2026-10-14',status=Assignment.STATUS_PUBLISHED);a.classes.add(c)
 for identity in ([{first!r},{twin!r}] if i==0 else [{first!r}]):
  Submission.objects.create(assignment=a,class_group=c,outside_student_id=identity,last_name='Однаковий',first_name='Учень',grade='8',graded_by=t.user,graded_at=timezone.now())
import json;print(json.dumps({{'class':c.pk,'subject':s.pk}}))
'''))
    test = json.loads(h.code('test', f'''
import json,datetime as dt
from app.database import SessionLocal
from app import models
with SessionLocal() as db:
 t=db.query(models.Teacher).filter_by(username='synthetic_teacher').one()
 c=models.RosterClass(name='8-З',grade_level=8,letter='З');s=models.RosterSubject(name='Окремий предмет');db.add_all([c,s]);db.flush()
 for i in range(2):
  exam=models.Test(teacher_id=t.id,title='Окремий тест '+str(i),class_name=c.name,subject=s.name,max_grade=12);db.add(exam);db.flush()
  session=models.TestSession(test_id=exam.id,access_code='OUTSIDE'+str(i),roster_class_id=c.id,roster_subject_id=s.id,lesson_date='2026-10-14');db.add(session);db.flush()
  for identity in ([{first!r},{twin!r}] if i==0 else [{first!r}]):
   db.add(models.StudentAttempt(session_id=session.id,student_name='Однаковий Учень',outside_student_id=identity,status=models.AttemptStatus.finished,score=8,max_score=12,finished_at=dt.datetime.now()))
 db.commit();print(json.dumps({{'class':c.id,'subject':s.id}}))
'''))
    state = json.loads(h.code('journal', f'''
import json
from django.urls import reverse
from apps.accounts.models import User
from apps.curriculum.models import StudentClass,Subject
from apps.journals.models import Journal,GradeAPIConnection,ExtraStudent,APIInboxResult,Grade
from apps.journals.services.external_grades import fetch_grade_payload,import_grade_records
from apps.journals.services.extra_students import keep_separate
t=User.objects.get(username='synthetic_teacher')
base=Journal.objects.filter(teacher=t).first();year=base.academic_year
c=StudentClass.objects.create(school=t.school,academic_year=year,name='8-З',grade_level=8,letter='З')
s=Subject.objects.create(school=t.school,name='Окремий предмет')
j=Journal.objects.create(school=t.school,academic_year=year,student_class=c,subject=s,teacher=t)
before=(StudentClass.objects.count(),Journal.objects.count(),Grade.objects.count())
for source,host,key,context in [('schoolwork','work','synthetic-work-grades',{work!r}),('schooltest','test','synthetic-test-grades',{test!r})]:
 conn=GradeAPIConnection.objects.create(journal=j,name=source,source=source,contract_version=2,url='http://'+host+':8000/api/v2/journal/grades/',api_key=key,source_class_id=str(context['class']),source_subject_id=str(context['subject']))
 rows=fetch_grade_payload(conn,year.start_date,year.end_date);assert len(rows)==3,rows
 import_grade_records(conn,rows,t,year.start_date,year.end_date)
 assert conn.extra_students.count()==2
 for extra in conn.extra_students.all():keep_separate(extra,t)
 for repeat in range(2):
  log=import_grade_records(conn,fetch_grade_payload(conn,year.start_date,year.end_date),t,year.start_date,year.end_date)
  assert log.counts['errors']==0 and log.counts['unchanged']==3,log.counts
assert before==(StudentClass.objects.count(),Journal.objects.count(),Grade.objects.count())
assert ExtraStudent.objects.filter(connection__journal=j).count()==4
assert APIInboxResult.objects.filter(connection__journal=j,resolved=True).count()==6
print(json.dumps({{'journal':j.pk,'path':reverse('portal:api_inbox',args=[j.pk]),'pupils':4,'results':6}}))
'''))
    h.login('journal')
    page = h.get('journal', state['path'])
    assert page.status_code == 200 and page.text.count('<h3>Однаковий Учень</h3>') == 4
    directory = h.get('journal', '/outside-students/', params={'q': 'Однаковий'})
    assert directory.status_code == 200 and directory.text.count('<h2>Однаковий Учень</h2>') == 4
    h.login('work')
    page = h.get('work', '/teacher/outside-students/', params={'q': 'Однаковий'})
    assert page.status_code == 200 and page.text.count('<h2>Однаковий Учень</h2>') == 2
    h.login('test')
    page = h.get('test', '/teacher/results', params={'pupil_scope': 'outside', 'student_name': 'Однаковий'})
    assert page.status_code == 200 and 'Однаковий Учень' in page.text
    h.log('PostgreSQL/HTTP: both sources keep two same-name identities; repeated works use one card per source; six marks preserved without a class, lesson or Grade')
    print(json.dumps(state, ensure_ascii=False))


if __name__ == '__main__':
    main()
