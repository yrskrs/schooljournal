"""Real HTTP proof that the one-key flow works with no configured peers."""
import datetime as dt
import json
from pathlib import Path
import tempfile
from bs4 import BeautifulSoup


def run(h):
    dc, code, get, post, until, log = [h[key] for key in ('dc', 'code', 'get', 'post', 'until', 'log')]
    with tempfile.TemporaryDirectory(prefix='school-simple-exchange-check-') as directory:
        override = Path(directory)/'peers-off.yaml'
        override.write_text('services:\n' + ''.join(f"  {name}:\n    environment:\n      ROSTER_PEERS: '[]'\n" for name in ('test', 'work', 'worker', 'journal')))
        h['COMMAND'].extend(['-f', str(override)])
        try:
            dc('up', '-d', '--force-recreate', 'test', 'work', 'worker', 'journal')
            for service in ('test', 'work', 'journal'):
                path='/healthz' if service=='test' else '/healthz/' if service=='journal' else '/teacher/login/'
                until(lambda s=service,p=path:get(s,p).status_code==200, service+' standalone configuration is healthy')
            code('journal', """from apps.curriculum.models import StudentClass,Student,ClassEnrollment
from apps.journals.models import Journal,Lesson
import datetime as dt
group=StudentClass.objects.create(school_id=1,academic_year_id=1,name='10-В',grade_level=10,letter='В')
pupil=Student.objects.create(school_id=1,last_name='Коваленко',first_name='Ірина')
ClassEnrollment.objects.create(student=pupil,student_class=group,start_date=dt.date(2026,9,1),is_active=True)
j=Journal.objects.create(school_id=1,academic_year_id=1,student_class=group,subject_id=1,teacher_id=1)
for date in (dt.date(2026,9,12),dt.date(2026,10,11)):Lesson.objects.create(journal=j,date=date,lesson_number=1,topic='Самостійний сценарій')
print(j.pk)""")
            journal_id=int(code('journal', "from apps.journals.models import Journal;print(Journal.objects.get(student_class__name='10-В').pk)"))
            connections={}
            for service, source in [('test','schooltest'),('work','schoolwork')]:
                previous=int(code('journal', f"from apps.journals.models import GradeAPIConnection;print(GradeAPIConnection.objects.get(journal_id=1,source='{source}').pk)"))
                response=post('journal', f'/journal/{journal_id}/connect/', {'action':'reuse','reuse_connection':previous})
                assert response.status_code==200,response.text[:300]
                connection=json.loads(code('journal', f"from apps.journals.models import GradeAPIConnection;import json;c=GradeAPIConnection.objects.get(journal_id={journal_id},source='{source}');print(json.dumps({{'id':c.pk,'class':c.source_class_id,'subject':c.source_subject_id,'student':c.student_mappings.get().external_id,'enabled':c.enabled}}))"))
                assert connection['enabled'] and connection['class'] and connection['subject']
                connections[service]=connection
                assert code(service, "from app.config import settings;print(settings.ROSTER_PEERS)" if service=='test' else "from django.conf import settings;print(settings.ROSTER_PEERS)")=='[]'
            assert code('journal',f'from apps.journals.models import Grade;print(Grade.objects.filter(lesson__journal_id={journal_id}).count())')=='0'
            log('one existing teacher key per source copies a new journal class and names into both native DBs; no peers, numeric input or grades during setup')
            code('test', """from app.database import SessionLocal
from app.models import Test,TestSession
with SessionLocal() as db:
 t=Test(teacher_id=1,title='Самостійний тест',class_name='10-В',subject='Вигаданий предмет',max_grade=12);db.add(t);db.flush()
 db.add(TestSession(test_id=t.id,access_code='TEN1',is_active=True));db.commit()""")
            dc('stop','journal','work','worker')
            roster=get('test','/api/session-roster?code=TEN1').json()
            assert any(s['name']=='Коваленко Ірина' for s in roster['students'])
            response=post('test','/student/login',{'student_name':'неправильно','roster_student_id':connections['test']['student'],'access_code':'TEN1'})
            assert response.status_code==200 and '/student/instruction/' in response.url
            response=post('test','/student/login',{'student_name':'Коваленк І.','other_student':'1','access_code':'TEN1'})
            assert response.status_code==200 and '/student/instruction/' in response.url
            code('test', """from app.database import SessionLocal
from app.models import StudentAttempt,TestSession,AttemptStatus
import datetime as dt
with SessionLocal() as db:
 session=db.query(TestSession).filter_by(access_code='TEN1').one()
 for a in db.query(StudentAttempt).filter_by(session_id=session.id):
  a.score=8 if a.roster_student_id else 9;a.max_score=12;a.finished_at=dt.datetime(2026,10,11,9);a.status=AttemptStatus.finished
 db.commit()""")
            log('tests alone use their saved journal pupil list and accept an extra typed name; both final results stay local')
            dc('start','work','worker');dc('stop','test')
            until(lambda:get('work','/teacher/login/').status_code==200,'work starts while journal and tests are unavailable')
            class_id=int(connections['work']['class']); subject_id=int(connections['work']['subject'])
            assignment=int(code('work', f"from feed.models import Assignment;a=Assignment.objects.create(teacher_id=1,subject_id={subject_id},title='Самостійне завдання',due_date='2026-09-12',status=Assignment.STATUS_PUBLISHED);a.classes.add({class_id});print(a.pk)"))
            response=post('work',f'/assignment/{assignment}/submit/',{'full_name':'Коваленко Ірина','student_id':connections['work']['student'],'class_group':class_id,'comment_student':'Вигадана автономна відповідь','coauthor_ids':'[]'})
            assert response.status_code==200,response.text[:300]
            code('work',f"from feed.models import Submission;from django.utils import timezone;s=Submission.objects.get(assignment_id={assignment});s.grade='10';s.graded_by_id=2;s.graded_at=timezone.now();s.save()")
            log('work alone accepts the local class-linked pupil and stores a final assignment grade')
            dc('start','test','journal')
            until(lambda:get('journal','/healthz/').status_code==200,'journal returns for explicit grade import')
            for source in ('schooltest','schoolwork'):
                response=post('journal',f'/journal/{journal_id}/sync/{source}/',{'date_from':'2026-09-01','date_to':'2026-10-31'})
                assert response.status_code==200
            state=json.loads(code('journal',f"from apps.journals.models import Grade,ExtraStudent,APIInboxResult;import json;print(json.dumps({{'grades':list(Grade.objects.filter(lesson__journal_id={journal_id}).order_by('lesson__date').values_list('value',flat=True)),'extras':list(ExtraStudent.objects.filter(connection__journal_id={journal_id}).values_list('name',flat=True)),'pending':APIInboxResult.objects.filter(connection__journal_id={journal_id},resolved=False).count()}}))"))
            assert state['grades']==['10','8'],state
            assert 'Коваленк І.' in state['extras'] and state['pending']==1,state
            response=get('journal',f'/journal/{journal_id}/api-inbox/')
            assert 'Коваленк І.' in response.text and 'Коваленко Ірина' in response.text
            soup=BeautifulSoup(response.text,'html.parser')
            form=next(f for f in soup.select('form') if f.select_one('button[value=match]'))
            data={field['name']:field.get('value','') for field in form.select('input[name]')}
            data.update(action='match',student=next(o['value'] for o in form.select('option') if o.get_text()=='Коваленко Ірина'))
            assert post('journal',f'/journal/{journal_id}/api-inbox/',data).status_code==200
            assert code('journal',f'from apps.journals.models import Grade;print(Grade.objects.filter(lesson__journal_id={journal_id}).count())')=='2'
            log('separate buttons recover accumulated dates and grades; typo stays in an extra list until explicit teacher matching, which never imports automatically')
            before=code('journal',f"from apps.journals.models import Grade,ExtraStudent,APIInboxResult;print((Grade.objects.filter(lesson__journal_id={journal_id}).count(),ExtraStudent.objects.filter(connection__journal_id={journal_id}).count(),APIInboxResult.objects.filter(connection__journal_id={journal_id}).count()))")
            dc('restart','test','work','journal')
            until(lambda:get('journal','/healthz/').status_code==200,'simple connections survive restart')
            after=code('journal',f"from apps.journals.models import Grade,ExtraStudent,APIInboxResult;print((Grade.objects.filter(lesson__journal_id={journal_id}).count(),ExtraStudent.objects.filter(connection__journal_id={journal_id}).count(),APIInboxResult.objects.filter(connection__journal_id={journal_id}).count()))")
            assert after==before
            log('standalone copies, native identities, extra pupils and accumulated results persist through restarts')
        finally:
            del h['COMMAND'][-2:]
