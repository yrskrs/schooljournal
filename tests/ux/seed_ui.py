"""Synthetic scrollable journal; execute only in tests/ux/compose.yaml."""
import datetime as dt
from apps.accounts.models import User
from apps.curriculum.models import StudentClass, Student, ClassEnrollment
from apps.journals.models import Journal, Lesson, Grade, NZAccount

teacher = User.objects.get(username='synthetic_teacher')
source = Journal.objects.get(pk=1)
group = StudentClass.objects.create(school=source.school, academic_year=source.academic_year,
                                  name='6-Б UI', grade_level=6, letter='Б')
journal = Journal.objects.create(school=source.school, academic_year=source.academic_year, student_class=group,
                                 subject=source.subject, teacher=teacher, nz_journal_id='synthetic-ui')
for i in range(25):
    student = Student.objects.create(school=source.school, first_name=f'Учень{i:02}', last_name='Синтетичний', nz_student_id=f'synthetic-ui-{i}')
    ClassEnrollment.objects.create(student=student, student_class=group, is_active=True)
lessons = []
for i in range(60):
    lessons.append(Lesson.objects.create(journal=journal, date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
                   lesson_number=1, topic=f'Синтетична тема {i:02}', is_synced_to_nz=True,
                   nz_schedule_id=f'synthetic-ui-lesson-{i}', nz_last_synced_topic=f'Синтетична тема {i:02}'))
Grade.objects.create(lesson=lessons[0], student=student, value='8', nz_last_synced_value='7')
NZAccount.objects.update_or_create(user=teacher, defaults={'login': 'synthetic-ui', 'phpsessid': 'synthetic-ui-session', 'is_active': True})
print(journal.pk)
