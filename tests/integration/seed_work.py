import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','schoolnet.settings')
import django
django.setup()
from django.contrib.auth import get_user_model
from django.utils import timezone
from feed.models import Teacher, ClassGroup, Subject, Student, Assignment, Submission, JournalAPIKey
User=get_user_model();User.objects.create_superuser(username='synthetic_admin',password='synthetic-test')
u=User.objects.create_user(username='synthetic_teacher',password='synthetic-test');teacher=Teacher.objects.create(user=u,full_name='Вигаданий Учитель')
group=ClassGroup.objects.create(name='7-А',grade=7,letter='А');subject=Subject.objects.create(name='Вигаданий предмет');teacher.classes.add(group);teacher.subjects.add(subject)
pupil=Student.objects.create(class_group=group,first_name='Учень',last_name='Вигаданий')
assignment=Assignment.objects.create(teacher=teacher,subject=subject,title='Вигадане завдання',status=Assignment.STATUS_PUBLISHED,due_date='2026-10-09');assignment.classes.add(group)
Submission.objects.create(assignment=assignment,student=pupil,class_group=group,first_name=pupil.first_name,last_name=pupil.last_name,grade='9',graded_by=u,graded_at=timezone.now())
JournalAPIKey.objects.create(teacher=teacher,key='synthetic-work-grades')
print('Synthetic work fixture ready')
