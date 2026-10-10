#!/usr/bin/env python3
"""Only synthetic containers from this directory's isolated Compose project."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
COMMAND=['docker','compose','-f',str(ROOT/'compose.yaml')]
BASE={'test':'http://127.0.0.1:19080','work':'http://127.0.0.1:19000','journal':'http://127.0.0.1:19085'}
TOKENS={'test':'synthetic-test-roster-token','work':'synthetic-work-roster-token','journal':'synthetic-journal-roster-token'}
BROWSER=requests.Session()
SESSIONS={key:BROWSER for key in BASE}
CSRF_COOKIES={'work':'schoolwork2_csrftoken','journal':'schooljournal_csrftoken'}


def dc(*args):
    return subprocess.run(COMMAND+list(args),check=True,capture_output=True,text=True,timeout=50).stdout

def code(service, body):
    header=''
    if service in ('work','journal'):
        module='schoolnet.settings' if service=='work' else 'config.settings'
        header=f"import os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','{module}');import django;django.setup()\n"
    result=subprocess.run(COMMAND+['exec','-T',service,'python','-'],input=header+body,check=True,capture_output=True,text=True,timeout=50)
    return result.stdout.strip()

def log(message):print('PASS:',message,flush=True)

def until(predicate, message, timeout=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            if predicate():log(message);return
        except (requests.RequestException,AssertionError):pass
        time.sleep(1)
    raise AssertionError(message)

def get(service,path,**kwargs):
    response=SESSIONS[service].get(BASE[service]+path,timeout=25,**kwargs)
    return response

def post(service,path,data):
    data=dict(data)
    if service!='test':data['csrfmiddlewaretoken']=SESSIONS[service].cookies.get(CSRF_COOKIES[service],'')
    return SESSIONS[service].post(BASE[service]+path,data=data,headers={'Referer':BASE[service]+path},timeout=45)

def snapshot(service):
    response=get(service,'/api/v2/roster-sync/',headers={'Authorization':'Bearer '+TOKENS[service]})
    assert response.status_code==200,response.text[:500]
    return response.json()

def grade_state():
    return json.loads(code('journal',"from apps.journals.models import Grade,ExternalGrade;print(__import__('json').dumps({'grades':list(Grade.objects.order_by('pk').values('student_id','lesson__date','value','import_record_id')),'imports':ExternalGrade.objects.count()},default=str))"))

def mark(date):return next(row['value'] for row in grade_state()['grades'] if row['student_id']==1 and row['lesson__date']==date)

def sync(source):
    response=post('journal',f'/journal/1/sync/{source}/',{'date_from':'2026-09-01','date_to':'2026-10-31'})
    assert response.status_code==200,response.text[:200]
    return response.text

def login(service):
    path='/accounts/login/' if service=='journal' else '/teacher/login'
    if service=='work':path+='/'
    page=get(service,path);assert page.status_code==200
    response=post(service,path,{'username':'synthetic_teacher','password':'synthetic-test'})
    assert response.status_code==200
    target=get(service, '/teacher/integrations' if service=='test' else '/teacher/roster-sync/' if service=='work' else '/roster-sync/')
    assert 'name="username"' not in target.text, 'Login did not establish teacher session'


def map_rows(service):
    path='/teacher/roster-sync/' if service=='work' else '/roster-sync/'
    for kind in ('class','student'):
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            page=get(service,path);soup=BeautifulSoup(page.text,'html.parser')
            target=None
            for form in soup.select('form'):
                ref=form.select_one('input[name=ref]')
                if not ref:continue
                record=next((row for row in snapshot('test')['records'] if row['ref']==ref['value']),None)
                if record and record['kind']==kind:target=form;break
            if target:
                data={field['name']:field.get('value','') for field in target.select('input[name]') if field.get('type')!='checkbox'}
                data.update(choice='0',native_id='1');response=post(service,path,data);assert response.status_code==200
                break
            time.sleep(1)
        else:raise AssertionError('Mapping form missing: '+service+' '+kind)
    log(service+' explicit class and pupil ID mappings through teacher forms')


def ready():
    rows=[sorted(snapshot(s)['records'],key=lambda row:row['ref']) for s in BASE]
    return len(rows[0])==2 and rows[0]==rows[1]==rows[2]


def simple_connections():
    # Replace only synthetic preconfigured connections with the real teacher flow.
    code('journal', 'from apps.journals.models import GradeAPIConnection;GradeAPIConnection.objects.all().delete()')
    for service in ('test', 'work'):
        path = '/teacher/integrations' if service == 'test' else '/teacher/settings/?tab=api'
        page = get(service, path)
        assert page.status_code == 200
        if service == 'test':
            soup = BeautifulSoup(page.text, 'html.parser')
            response = post(service, path, {'action': 'simple_key', 'csrf': soup.select_one('input[name=csrf]')['value']})
        else:
            response = post(service, path, {'action': 'create_simple_journal_api_key'})
        assert response.status_code == 200
        field = BeautifulSoup(response.text, 'html.parser').select_one('#journal-api-key')
        assert field, 'Source did not provide a plain API key'
        api_key = field.get_text().strip()
        assert not api_key.startswith('{')
        container = dc('ps','-q',service).strip()
        metadata = json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
        source_ip = next(iter(metadata['NetworkSettings']['Networks'].values()))['IPAddress']
        journal_path = '/journal/1/connect/'
        response = post('journal', journal_path, {'action': 'connect', 'source':'schooltest' if service=='test' else 'schoolwork', 'site_url':'http://'+source_ip+':8000','api_key':api_key})
        assert response.status_code == 200, response.text[:200]
        soup = BeautifulSoup(response.text, 'html.parser')
        form = next(form for form in soup.select('form') if form.select_one('button[value=context]'))
        data = {field['name']: field.get('value', '') for field in form.select('input[name]')}
        for name, label in [('source_class_id', '7-А'), ('source_subject_id', 'Вигаданий предмет')]:
            choice = next(option for option in form.select('select[name=' + name + '] option') if option.get_text() == label)
            data[name] = choice['value']
        data['action'] = 'context'
        response = post('journal', journal_path, data)
        assert response.status_code == 200
        form = next(form for form in BeautifulSoup(response.text, 'html.parser').select('form') if form.select_one('button[value=students]'))
        data = {field['name']: field.get('value', '') for field in form.select('input[name]') if field.get('type') != 'checkbox'}
        for field in form.select('select[name^=pupil_]'):
            choice = next(option for option in field.select('option') if option.get_text() == 'Вигаданий Учень')
            data[field['name']] = choice['value']
        data.update(action='students', confirm='on')
        assert post('journal', journal_path, data).status_code == 200
        assert not grade_state()['grades'], 'Setup unexpectedly imported grades'
        log(service + ' API created in its UI; journal uses a plain key and IP, chooses class/subject names and confirms pupils without numeric input')
    assert int(code('journal', "from apps.journals.models import GradeAPIConnection;print(GradeAPIConnection.objects.filter(enabled=True).exclude(roster_cache={}).count())")) == 2


def manual(value,date='2026-09-05',student=1):
    lesson=int(code('journal',f"from apps.journals.models import Lesson;print(Lesson.objects.get(journal_id=1,date='{date}').pk)"))
    response=post('journal','/journal/1/grade/update/',{'lesson_id':lesson,'student_id':student,'value':value})
    assert response.status_code==200,response.text[:250]


def changed_ip():
    """A second isolated interface changes the actual source IP, keeping its DB."""
    network = 'school-api-ip-change-check'
    containers = {service: dc('ps', '-q', service).strip() for service in ('test', 'journal')}
    subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
    before_network = json.loads(subprocess.check_output(['docker', 'inspect', containers['test']], text=True))[0]
    original_address = 'http://' + before_network['NetworkSettings']['Networks']['school-api-checks_default']['IPAddress'] + ':8000'
    attached = []
    try:
        for container in containers.values():
            subprocess.run(['docker', 'network', 'connect', network, container], check=True, capture_output=True)
            attached.append(container)
        metadata = json.loads(subprocess.check_output(['docker', 'inspect', containers['test']], text=True))[0]
        address = 'http://' + metadata['NetworkSettings']['Networks'][network]['IPAddress'] + ':8000'
        connection = json.loads(code('journal', "from apps.journals.models import GradeAPIConnection;import json\nc=GradeAPIConnection.objects.get(source='schooltest',enabled=True);print(json.dumps({'id':c.pk,'key':c.api_key,'url':c.url,'mappings':list(c.student_mappings.values_list('external_id','student_id'))}))"))
        before = grade_state()
        for target in (address, original_address):
            response = post('journal', '/journal/1/connect/', {'action': 'connect', 'connection_id': connection['id'],
                'source': 'schooltest', 'site_url': target, 'api_key': connection['key']})
            assert response.status_code == 200 and '?connection=' in response.url, 'Changing the source IP failed'
            after = json.loads(code('journal', "from apps.journals.models import GradeAPIConnection;import json\nc=GradeAPIConnection.objects.get(source='schooltest',enabled=True);print(json.dumps({'url':c.url,'mappings':list(c.student_mappings.values_list('external_id','student_id')),'count':GradeAPIConnection.objects.filter(source='schooltest').count()}))"))
            assert after['url'] == target + '/api/v2/journal/grades/'
            assert after['count'] == 1 and after['mappings'] == connection['mappings']
            assert grade_state() == before, 'Changing the IP altered grades or imported automatically'
        log('source IP changes over actual HTTP keep the connection, pupil mappings and imported/manual grades; original address restored')
    finally:
        for container in attached:
            subprocess.run(['docker', 'network', 'disconnect', network, container], check=True, capture_output=True)
        subprocess.run(['docker', 'network', 'rm', network], check=True, capture_output=True)


def main():
    for service in ('journal','work','test'):
        print('Seeding',service,flush=True)
        code(service,(ROOT/('seed_'+service+'.py')).read_text())
    for service in BASE:login(service)
    for service,path in [('journal','/journal/1/connect/'),('work','/teacher/settings/?tab=api'),('test','/teacher/integrations')]:
        response=get(service,path,allow_redirects=False)
        assert response.status_code==200, 'A second service login replaced the '+service+' session'
    assert 'schooljournal_sessionid' in BROWSER.cookies and 'schoolwork2_sessionid' in BROWSER.cookies
    log('one browser cookie jar keeps all three teacher logins and independent CSRF tokens on the same IP with different ports')
    map_rows('work');map_rows('journal');until(ready,'three native rosters converge without name matching')
    simple_connections()
    assert not grade_state()['grades'];time.sleep(3);assert not grade_state()['grades'];log('background roster exchange never imports marks')
    for service in BASE:
        assert requests.get(BASE[service]+'/api/v2/roster-sync/',timeout=10).status_code==401
    log('server API requires bearer keys')
    assert requests.post(BASE['journal']+'/journal/1/sync/schooltest/',timeout=10).status_code==403
    assert get('journal','/journal/1/sync/schooltest/').status_code==405
    log('teacher action requires POST, session and CSRF')
    for source in ('schooltest','schoolwork'):sync(source)
    assert mark('2026-09-05')=='8' and mark('2026-10-09')=='9'
    original=grade_state()
    sync('schooltest');sync('schoolwork');assert grade_state()==original
    log('both buttons import separately and repeated clicks do not duplicate')
    code('test',"from app.database import SessionLocal;from app.models import StudentAttempt\nwith SessionLocal() as db:\n a=db.get(StudentAttempt,1);a.score=10;db.commit()")
    code('work',"from feed.models import Submission;from django.utils import timezone;s=Submission.objects.get(pk=1);s.grade='11';s.graded_at=timezone.now();s.save()")
    time.sleep(3);assert mark('2026-09-05')=='8' and mark('2026-10-09')=='9'
    sync('schooltest');sync('schoolwork');assert mark('2026-09-05')=='10' and mark('2026-10-09')=='11'
    assert grade_state()['imports']==2;log('corrected grades update existing imports only after teacher clicks')
    manual('7');code('test',"from app.database import SessionLocal;from app.models import StudentAttempt\nwith SessionLocal() as db:\n a=db.get(StudentAttempt,1);a.score=12;db.commit()")
    sync('schooltest');assert mark('2026-09-05')=='7';log('manual grade ownership survives source correction')
    for value in ('9.5','В','0'):
        code('work',f"from feed.models import Submission;from django.utils import timezone;s=Submission.objects.get(pk=1);s.grade={value!r};s.graded_at=timezone.now();s.save()")
        page=sync('schoolwork');assert mark('2026-10-09')=='11';assert 'Помил' in page or 'помил' in page
    code('test',"from app.database import SessionLocal;from app.models import Test\nwith SessionLocal() as db:\n t=db.get(Test,1);t.max_grade=100;db.commit()")
    sync('schooltest');assert mark('2026-09-05')=='7';log('fractional, level, zero and alternate-scale marks require manual decisions')
    code('test',"from app.database import SessionLocal;from app.models import Test\nwith SessionLocal() as db:\n db.get(Test,1).max_grade=12;db.commit()")
    dc('stop','test')
    assert get('work','/api/students-autocomplete/?class_group_id=1').status_code==200
    assert get('journal','/journal/1/').status_code==200
    connection = code('journal', "from apps.journals.models import GradeAPIConnection;print(GradeAPIConnection.objects.get(source='schooltest').pk)")
    assert 'Клас на сайті джерела' in get('journal','/journal/1/connect/?connection=' + connection).text
    sync('schooltest');assert mark('2026-09-05')=='7'
    code('work',"from feed.models import Submission,Student;from django.utils import timezone;s=Submission.objects.get(pk=1);s.grade='10';s.graded_at=timezone.now();s.save();Student.objects.create(class_group_id=1,first_name='Новий',last_name='Вигаданий')")
    sync('schoolwork');assert mark('2026-10-09')=='10';log('tests offline: work and journal stay usable; work button still imports')
    until(lambda:len(snapshot('journal')['records'])==3,'journal receives newly added native pupil while tests are offline')
    dc('start','test');until(lambda:len(snapshot('test')['records'])==3,'background retry catches up after tests return')
    # Concurrent edits are made under each source's native database lock.
    dc('pause','test','journal','worker')
    code('work',"from feed.models import Student;s=Student.objects.get(pk=1);s.first_name='ВерсіяЗавдань';s.save()")
    edit_test="from app.models import RosterStudent;from app.services.roster_backend import operate\ndef edit(engine,store):\n row=engine.native.db.get(RosterStudent,1);row.first_name='ВерсіяТестів';engine.capture()\noperate(edit)"
    editor_env = dict(os.environ, CHECK_TEST_IP=os.environ.get('CHECK_EDITOR_IP', '10.253.141.20'))
    subprocess.run(COMMAND+['run','--rm','--no-deps','-T','--entrypoint','python','test','-'],input=edit_test,
        env=editor_env, check=True,capture_output=True,text=True,timeout=30)
    dc('unpause','test','journal','worker')
    until(lambda:'Конфлікт' in get('work','/teacher/roster-sync/').text,'concurrent native edits produce a visible manual conflict')
    page=get('work','/teacher/roster-sync/');soup=BeautifulSoup(page.text,'html.parser')
    for form in soup.select('form'):
        action=form.select_one('input[name=action]')
        if not action or action.get('value')!='resolve':continue
        ref=form.select_one('input[name=ref]')
        if ref and any(row['ref']==ref['value'] and row['kind']=='student' for row in snapshot('work')['records']):
            data={field['name']:field.get('value','') for field in form.select('input[name]') if field.get('type')!='checkbox'}
            data['choice']='local';response=post('work','/teacher/roster-sync/',data)
            assert response.status_code==200 and 'Рішення збережено.' in response.text
            break
    else:raise AssertionError('No conflict decision form')
    until(lambda:len({next(row['payload']['first_name'] for row in snapshot(s)['records'] if row['kind']=='student' and row['payload']['last_name']=='Вигаданий' and row['payload']['first_name']!='Новий') for s in BASE})==1,'teacher conflict decision propagates to all replicas')
    dc('stop','journal','test')
    assert get('work','/').status_code==200
    page=get('work','/assignment/1/submit/');assert page.status_code==200
    name=next(row['full_name'] for row in get('work','/api/students-autocomplete/?class_group_id=1').json()['students'] if row['id']==1)
    response=post('work','/assignment/1/submit/',{'full_name':name,'student_id':'1','class_group':'1','comment_student':'Вигадана робота без інших сервісів','coauthor_ids':'[]'})
    assert response.status_code==200
    assert int(code('work',"from feed.models import Submission;print(Submission.objects.count())"))==2
    # Grade the newest attempt with the existing teacher's identity.
    code('work',"from feed.models import Submission;from django.utils import timezone;s=Submission.objects.latest('pk');s.grade='9';s.graded_by_id=2;s.graded_at=timezone.now();s.save()")
    log('work alone accepts and stores a submission linked to native pupil ID')
    dc('start','test');dc('stop','work','worker')
    until(lambda:get('test','/healthz').status_code==200,'tests return healthy')
    page=get('test','/student/login?code=CHECK1');assert page.status_code==200
    response=post('test','/student/login',{'student_name':name,'roster_student_id':'1','access_code':'CHECK1'})
    assert response.status_code==200 and '/student/instruction/' in response.url
    code('test',"from app.database import SessionLocal;from app.models import StudentAttempt;import datetime as dt\nwith SessionLocal() as db:\n a=db.query(StudentAttempt).order_by(StudentAttempt.id.desc()).first();a.score=9;a.max_score=12;a.finished_at=dt.datetime(2026,9,5,14);a.status=__import__('app.models',fromlist=['AttemptStatus']).AttemptStatus.finished;db.commit()")
    log('tests alone accept a pupil and persist a new final attempt')
    dc('start','journal');until(lambda:get('journal','/healthz/').status_code==200,'journal returns healthy')
    dc('stop','test')
    assert get('journal','/journal/1/').status_code==200
    manual('6',date='2026-09-06',student=2)
    sync('schooltest');sync('schoolwork');assert mark('2026-09-05')=='7'
    log('journal alone reads cached roster and stores manual grades despite unavailable sources')
    dc('start','test','work','worker');until(lambda:get('work','/teacher/login/').status_code==200,'work returns healthy')
    sync('schoolwork');assert mark('2026-10-09')=='9';assert grade_state()['imports']==2
    log('accumulated retake is replayed with stable result ID after journal downtime')
    before=grade_state();dc('restart','test','work','journal')
    for service in BASE:until(lambda s=service:get(s,'/healthz' if s=='test' else '/healthz/' if s=='journal' else '/teacher/login/').status_code==200,service+' restart is healthy')
    assert grade_state()==before;assert len(snapshot('test')['records'])==3
    log('restarts preserve grades, native pupil IDs and roster metadata')
    # Genuine two-page HTTP export and server-side fetch, using only synthetic records.
    code('work',"from feed.models import Assignment,Submission;from django.utils import timezone\nworks=Assignment.objects.bulk_create([Assignment(teacher_id=1,subject_id=1,title='Вигадане завдання '+str(i),due_date='2026-09-09',status=Assignment.STATUS_PUBLISHED) for i in range(501)])\nAssignment.classes.through.objects.bulk_create([Assignment.classes.through(assignment_id=w.pk,classgroup_id=1) for w in works])\nSubmission.objects.bulk_create([Submission(assignment_id=w.pk,student_id=1,class_group_id=1,first_name='Вигаданий',last_name='Учень',grade='8',graded_by_id=2,graded_at=timezone.now()) for w in works])")
    params={'class_id':1,'subject_id':1,'date_from':'2026-09-01','date_to':'2026-10-31','cursor':0}
    response=get('work','/api/v2/journal/grades/',params=params,headers={'Authorization':'Bearer synthetic-work-grades'});assert response.status_code==200
    payload=response.json();assert len(payload['grades'])==500 and payload['next_cursor'] is not None
    params['cursor']=payload['next_cursor'];second=get('work','/api/v2/journal/grades/',params=params,headers={'Authorization':'Bearer synthetic-work-grades'}).json()
    assert second['next_cursor'] is None and len(second['grades'])==2
    page=sync('schoolwork');assert grade_state()['imports']==503
    log('501 accumulated assignments are fetched over two actual HTTP pages; occupied cells stay conflicts')
    code('test', "from app.database import SessionLocal;from app.models import Test,TestSession\nwith SessionLocal() as db:\n t=Test(teacher_id=1,title='Вигаданий старий тест',class_name='9-L',subject='Вигаданий предмет');db.add(t);db.flush();db.add(TestSession(test_id=t.id,access_code='LEGACY1'));db.commit()")
    page = get('test', '/teacher/integrations')
    token = BeautifulSoup(page.text, 'html.parser').select_one('input[name=csrf]')['value']
    response = post('test', '/teacher/integrations', {'action': 'simple_key', 'csrf': token})
    assert response.status_code == 200
    assert not get('test', '/api/session-roster?code=LEGACY1').json()['required']
    response = post('test', '/student/login', {'student_name': 'Вигаданий Новий', 'access_code': 'LEGACY1'})
    assert response.status_code == 200 and '/student/instruction/' in response.url
    assert code('test', "from app.database import SessionLocal;from app.models import StudentAttempt,TestSession\nwith SessionLocal() as db:\n s=db.query(TestSession).filter_by(access_code='LEGACY1').one();a=db.query(StudentAttempt).filter_by(session_id=s.id).one();print(s.roster_class_id is None and a.roster_student_id is None)") == 'True'
    log('API creation preserves legacy pupil login with an empty roster; no pupil is merged by name')
    changed_ip()
    log('all isolated Docker integration scenarios completed')

if __name__=='__main__':main()
