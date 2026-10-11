#!/usr/bin/env python3
"""Run after run.py, against its isolated synthetic Docker project only."""
import json
from bs4 import BeautifulSoup
import run as h


def submit_form(path, selector, action, **values):
    page = h.get('journal', path)
    assert page.status_code == 200
    form = BeautifulSoup(page.text, 'html.parser').select_one(selector)
    assert form, selector
    data = {field['name']: field.get('value', '') for field in form.select('input[name]')}
    data.update(action=action, **values)
    response = h.post('journal', form.get('action') or path, data)
    assert response.status_code == 200, response.status_code
    return response


def main():
    h.login('work'); h.login('journal')
    initial_tombstones = int(h.code('journal', 'from apps.journals.models import DeletedAPIIdentity;print(DeletedAPIIdentity.objects.count())'))
    ids = json.loads(h.code('work', """
import json
from django.core.files.base import ContentFile
from django.utils import timezone
from feed.models import Student, Submission, SubmissionFile, Assignment
s=Student.objects.create(class_group_id=1,last_name='ВигаданийДляВидалення',first_name='Учень')
a=Assignment.objects.create(teacher_id=1,subject_id=1,title='Синтетичне видалення',published_at=timezone.now())
a.classes.add(1)
w=Submission.objects.create(student=s,class_group_id=1,assignment=a,teacher_id=1,last_name=s.last_name,first_name=s.first_name,grade='8',graded_by_id=1,graded_at=timezone.now())
w.file.save(f'deletion-check-{s.pk}.txt',ContentFile(b'synthetic deletion check'),save=True)
f=SubmissionFile.objects.create(submission=w);f.file.save(f'deletion-extra-{s.pk}.txt',ContentFile(b'synthetic extra'),save=True)
print(json.dumps({'student':s.pk,'submission':w.pk,'file':w.file.name,'attachment':f.file.name}))
"""))
    original = h.grade_state()
    h.sync('schoolwork')
    extra = json.loads(h.code('journal', f"""
import json
from apps.journals.models import ExtraStudent
e=ExtraStudent.objects.get(connection__journal_id=1,connection__source='schoolwork',external_key='{ids['student']}')
print(json.dumps({{'id':e.pk,'connection':e.connection_id}}))
"""))
    path = '/journal/1/api-inbox/?all=1'
    # Locate the real rendered form, including the localized revision and CSRF.
    def form_selector(extra_id, action):
        return f'form:has(input[name=extra_id][value="{extra_id}"]):has(button[value="{action}"])'
    response = submit_form(path, form_selector(extra['id'], 'match'), 'match', student='1')
    assert response.url.endswith('?all=1') and 'Відповідність підтверджено' in response.text
    assert h.grade_state() == original
    assert h.code('journal', f'from apps.journals.models import ExtraStudent;print(ExtraStudent.objects.get(pk={extra["id"]}).matched_student_id)') == '1'
    h.log('real HTTP confirmation persists, shows its status in all=1 and leaves grades untouched')
    response = submit_form(path, form_selector(extra['id'], 'delete_extra'), 'delete_extra', confirm_delete='on')
    assert 'Цей запис не завантажиться повторно' in response.text
    h.sync('schoolwork')  # Source still has the record: local deletion must win.
    assert h.code('journal', f'from apps.journals.models import ExtraStudent,APIInboxResult;print((ExtraStudent.objects.filter(pk={extra["id"]}).exists(),APIInboxResult.objects.filter(connection_id={extra["connection"]},payload__student_id={ids["student"]}).exists()))') == '(False, False)'
    assert h.grade_state() == original
    h.log('HTTP deletion removes saved identities/results; repeated source export cannot recreate them; class marks survive')
    response = h.post('work', f'/teacher/students/{ids["student"]}/delete/', {})
    assert response.status_code == 200, (response.status_code, response.text[:300])
    state = json.loads(h.code('work', f"""
import json
from django.core.files.storage import default_storage
from feed.models import Student,Submission,SubmissionActivityLog
print(json.dumps({{'student':Student.objects.filter(pk={ids['student']}).exists(),'submission':Submission.objects.filter(pk={ids['submission']}).exists(),'files':[default_storage.exists({ids['file']!r}),default_storage.exists({ids['attachment']!r})],'audit':SubmissionActivityLog.objects.filter(action_type='student_deleted',description__contains='#{ids['student']} ').exists(),'work_audit':SubmissionActivityLog.objects.filter(action_type='delete',description__contains='#{ids['submission']},').exists()}}))
"""))
    assert state == {'student':False,'submission':False,'files':[False,False],'audit':True,'work_audit':True}, state
    h.log('HTTP pupil deletion removes profile, submissions and unshared files; both audits survive')
    kept = int(h.code('journal', f"from apps.journals.models import ExtraStudent;print(ExtraStudent.objects.create(connection_id={extra['connection']},external_key='unmapped:987654321',name='ПозакласнийВигаданий Учень').pk)"))
    before_count = h.code('journal', 'from apps.journals.models import Grade;print(Grade.objects.count())')
    submit_form(path, form_selector(kept, 'keep_separate'), 'keep_separate')
    assert h.code('journal', 'from apps.journals.models import Grade;print(Grade.objects.count())') == before_count
    local = json.loads(h.code('journal', f"""
import json
from apps.journals.models import ExtraStudent,Lesson,APIInboxResult
e=ExtraStudent.objects.get(pk={kept});l=Lesson.objects.create(journal=e.separate_journal,date='2026-10-11',lesson_number=1)
row={{'source':'schoolwork','id':'schoolwork:unmapped:987654321','student_id':None,'student_key':'unmapped:987654321','student_name':e.name,'class_id':int(e.connection.source_class_id),'subject_id':int(e.connection.source_subject_id),'work_id':987654321,'attempt_id':987654321,'title':'Вигадана робота','points':8,'max_points':12,'value':'8','grading_scale':12,'date':'2026-10-11','status':'final','updated_at':'2026-10-11T12:00:00+03:00','is_ai':False,'approved_by_teacher':True}}
r=APIInboxResult.objects.create(connection=e.connection,extra_student=e,external_id=row['id'],payload=row)
print(json.dumps({{'student':e.separate_student_id,'journal':e.separate_journal_id,'result':r.pk,'lesson':l.pk}}))
"""))
    result_selector = f'form:has(input[name=result_id][value="{local["result"]}"]):has(button[value="apply_saved"])'
    submit_form(path, result_selector, 'apply_saved', lesson=str(local['lesson']))
    assert h.code('journal', f'from apps.journals.models import Grade;print(Grade.objects.get(student_id={local["student"]}).value)') == '8'
    # Exercise a legacy dedicated table without the new stored identity link.
    h.code('journal', f'from apps.journals.models import ExtraStudent;e=ExtraStudent.objects.get(pk={kept});e.separate_student=None;e.separate_journal=None;e.save()')
    submit_form(f'/journal/{local["journal"]}/',
        f'form[action="/journal/{local["journal"]}/student/{local["student"]}/delete-extra/"]', '')
    assert h.code('journal', f'from apps.curriculum.models import Student;from apps.journals.models import Grade;print((Student.objects.filter(pk={local["student"]}).exists(),Grade.objects.filter(student_id={local["student"]}).exists()))') == '(False, False)'
    submit_form(path, form_selector(kept, 'delete_extra'), 'delete_extra', confirm_delete='on')
    h.log('HTTP keep-separate has stable state; explicit saved-result import uses its own lesson; pupil-number menu deletes a legacy separate profile without name linking')
    h.dc('restart','work','journal')
    h.until(lambda:h.get('journal','/healthz/').status_code==200 and h.get('work','/teacher/login/').status_code==200,'deletion state survives Docker restart')
    h.sync('schoolwork')
    assert h.grade_state() == original
    assert int(h.code('journal', 'from apps.journals.models import DeletedAPIIdentity;print(DeletedAPIIdentity.objects.count())')) == initial_tombstones + 2
    h.log('deletion tombstones and original marks persist after restart and another pull')


if __name__ == '__main__': main()
