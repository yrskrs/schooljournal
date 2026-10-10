import datetime as dt, hashlib
from app import crud, models
from app.database import SessionLocal
from app.services.roster_backend import operate
with SessionLocal() as db:
    teacher=crud.create_teacher(db,'synthetic_teacher','Вигаданий Учитель','synthetic-test')
    group=models.RosterClass(name='7-А',grade_level=7,letter='А');subject=models.RosterSubject(name='Вигаданий предмет')
    db.add_all([group,subject]);db.flush()
    pupil=models.RosterStudent(class_id=group.id,first_name='Учень',last_name='Вигаданий',middle_name='')
    db.add(pupil);db.commit()
    test=crud.create_test(db,teacher.id,{'title':'Вигаданий тест','class_name':'7-А','subject':'Вигаданий предмет','allow_retake':True,'questions':[]})
    session=crud.create_session(db,test.id,'CHECK1');session.roster_class_id=group.id;session.roster_subject_id=subject.id;session.lesson_date='2026-09-05'
    attempt=crud.create_attempt(db,session.id,pupil.full_name(),roster_student_id=pupil.id)
    attempt.status=models.AttemptStatus.finished;attempt.score=8;attempt.max_score=12;attempt.finished_at=dt.datetime(2026,9,5,12)
    db.add(models.GradeExportGrant(teacher_id=teacher.id,token_hash=hashlib.sha256(b'synthetic-test-grades').hexdigest()));db.commit()
operate(lambda engine,store:engine.share('class',1))
print('Synthetic test fixture ready')
