#!/usr/bin/env python3
"""Real HTTP exclusion/restoration against both supported source images."""
import json
from http_check import h


def state(ids):
    return {
        'test': json.loads(h.code('test', f"from app.database import SessionLocal;from app.models import RosterStudent\nwith SessionLocal() as db:\n rows=db.query(RosterStudent).filter(RosterStudent.class_id=={ids['classes']['schooltest']}).all();print(__import__('json').dumps({{'active':[row.id for row in rows if row.active],'all':[row.id for row in rows]}}))")),
        'work': json.loads(h.code('work', f"from feed.models import Student;rows=Student.objects.filter(class_group_id={ids['classes']['schoolwork']});print(__import__('json').dumps({{'active':list(rows.filter(integration_active=True).values_list('pk',flat=True)),'all':list(rows.values_list('pk',flat=True))}}))")),
    }


def main():
    h.login('journal')
    ids = json.loads(h.code('journal', """
import json
from apps.curriculum.models import StudentClass, Student, ClassEnrollment
from apps.journals.models import Journal, GradeAPIConnection, GradeStudentMapping
from apps.journals.services.roster_transfer import send_journal_roster
source=Journal.objects.get(pk=1)
group=StudentClass.objects.create(school=source.school,academic_year=source.academic_year,name='6-В Ігнорування',grade_level=6,letter='В')
journal=Journal.objects.create(school=source.school,academic_year=source.academic_year,student_class=group,subject=source.subject,teacher=source.teacher)
pupils=[Student.objects.create(school=source.school,last_name='СинтетичнийВідбір'+str(i),first_name='Учень') for i in range(2)]
for pupil in pupils:ClassEnrollment.objects.create(student=pupil,student_class=group,is_active=True)
journal.ignored_students.add(pupils[1])
classes={}
for original in source.grade_apis.all():
 connection=GradeAPIConnection.objects.create(journal=journal,name=original.name,source=original.source,contract_version=2,url=original.url,api_key=original.api_key,roster_cache=original.roster_cache,source_subject_id=original.source_subject_id)
 report=send_journal_roster(connection,source.teacher)
 assert report['counts']['created']==1 and not report['counts']['conflicts']
 classes[connection.source]=report['class_id']
 assert connection.student_mappings.count()==1
print(json.dumps({'journal':journal.pk,'pupil':pupils[0].pk,'classes':classes}))
"""))
    before = state(ids)
    assert all(len(value['all']) == len(value['active']) == 1 for value in before.values())
    h.log('A newly ignored pupil is never created in either source')
    path = f"/journal/{ids['journal']}/student/{ids['pupil']}/toggle-ignore/"
    response = h.post('journal', path, {})
    assert response.status_code == 200
    ignored = state(ids)
    assert all(value['all'] == before[key]['all'] and not value['active'] for key, value in ignored.items())
    h.log('Teacher ignore POST deactivates both previously sent pupils while preserving their IDs')
    assert h.get('work', f"/api/students-autocomplete/?class_group_id={ids['classes']['schoolwork']}").json()['students'] == []
    h.log('Ignored pupil is absent from the task student picker over real HTTP')
    response = h.post('journal', path, {})
    assert response.status_code == 200
    assert state(ids) == before
    h.log('Restoring a pupil reactivates both sources with the same native IDs')
    h.code('journal', f"from apps.journals.models import GradeAPIConnection,Grade;from apps.journals.services.roster_transfer import send_journal_roster\nfor connection in GradeAPIConnection.objects.filter(journal_id={ids['journal']}):\n report=send_journal_roster(connection,connection.journal.teacher);assert not report['counts']['created']\nassert not Grade.objects.filter(lesson__journal_id={ids['journal']}).exists()")
    assert state(ids) == before
    h.log('Repeating roster transfer creates neither duplicate pupils nor grades')


if __name__ == '__main__':
    main()
