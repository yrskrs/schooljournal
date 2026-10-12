"""Standalone synthetic teachers and classes, without server roster settings."""
import datetime as dt
import json
from apps.accounts.models import User
from apps.schools.models import School
from apps.curriculum.models import AcademicYear, StudentClass, Subject, Student, ClassEnrollment
from apps.journals.models import Journal, NZAccount

school = School.objects.create(name='Вигадана школа списків')
year = AcademicYear.objects.create(school=school, name='2026/2027', start_date=dt.date(2026, 9, 1), end_date=dt.date(2027, 6, 30))
teacher = User.objects.create_user(username='roster_teacher', password='roster-only-tests', school=school, role=User.Role.TEACHER)
personal = User.objects.create_user(username='roster_personal', password='roster-only-tests', role=User.Role.TEACHER, is_nz_personal=True)
owner = NZAccount.objects.create(user=personal, login='synthetic-roster', phpsessid='synthetic-roster', is_active=True)
subject = Subject.objects.create(school=school, name='Синтетична математика')
groups = []
for name in ('5-А', '6-Б'):
    group = StudentClass.objects.create(school=school, academic_year=year, name=name)
    groups.append(group)
    journal = Journal.objects.create(school=school, academic_year=year, student_class=group, subject=subject, teacher=teacher)
    for i in range(3):
        student = Student.objects.create(school=school, first_name=f'Учень{i}', last_name='Синтетичний')
        ClassEnrollment.objects.create(student=student, student_class=group, is_active=True)
    journal.ignored_students.add(student)
personal_group = StudentClass.objects.create(school=school, academic_year=year, name='7-В особистий')
Journal.objects.create(school=school, academic_year=year, student_class=personal_group, subject=subject, teacher=personal, nz_owner=owner)
print(json.dumps({'classes': [group.pk for group in groups], 'personal': personal_group.pk}))
