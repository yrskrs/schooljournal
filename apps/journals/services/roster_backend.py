from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from apps.curriculum.models import StudentClass, Student, ClassEnrollment
from apps.journals.models import RosterReplica, RosterState
from apps.portal.permissions import get_user_journals, has_journal_access
from school_sync.core import SyncError


def classes():
    return StudentClass.objects.filter(school_id=settings.ROSTER_SCHOOL_ID, academic_year_id=settings.ROSTER_YEAR_ID)


def can_access(user):
    return user.is_authenticated and (user.is_teacher_user or user.is_school_admin_user or user.is_superadmin_user)


def allowed_classes(user):
    rows = classes()
    if user.is_superadmin_user:
        return list(rows.values('id', 'name'))
    if user.is_school_admin_user:
        return list(rows.filter(school_id=user.school_id).values('id', 'name'))
    allowed = [journal.student_class_id for journal in get_user_journals(user) if has_journal_access(user, journal, write=True)]
    return list(rows.filter(pk__in=allowed).values('id', 'name'))


class Native:
    def students(self, class_id):
        ids = ClassEnrollment.objects.filter(student_class_id=class_id).values_list('student_id', flat=True).distinct()
        return [pk for pk in ids if (data := self.read('student', pk)) and data['native_class_id'] == class_id]

    def read(self, kind, native_id):
        if kind == 'class':
            row = classes().filter(pk=native_id).first()
            return None if row is None else {'name': row.name, 'grade_level': row.grade_level, 'letter': row.letter, 'active': row.integration_active}
        row = Student.objects.filter(pk=native_id, school_id=settings.ROSTER_SCHOOL_ID).first()
        if not row:
            return None
        enrollment = row.enrollments.filter(student_class__in=classes()).order_by('-is_active', '-start_date', '-pk').first()
        if not enrollment:
            return None
        return {'name': row.get_full_name_ukr(), 'first_name': row.first_name, 'last_name': row.last_name,
            'middle_name': row.middle_name, 'native_class_id': enrollment.student_class_id,
            'active': enrollment.is_active and row.status == Student.Status.ACTIVE}

    def collision(self, kind, payload):
        if kind == 'class':
            return classes().filter(name=payload['name']).exists()
        return any(row.get_full_name_ukr().casefold() == payload['name'].casefold() for row in
            Student.objects.filter(enrollments__student_class_id=payload['native_class_id']).distinct())

    def write(self, kind, native_id, payload):
        if kind == 'class':
            row = classes().get(pk=native_id)
            row.name, row.grade_level, row.letter = payload['name'], payload.get('grade_level', row.grade_level), payload.get('letter', row.letter)
            row.integration_active = payload['active']
            row.full_clean()
            row.save()
            return
        group = classes().get(pk=payload['native_class_id'])
        row = Student.objects.get(pk=native_id, school_id=settings.ROSTER_SCHOOL_ID)
        row.first_name, row.last_name, row.middle_name = payload.get('first_name', ''), payload.get('last_name', ''), payload.get('middle_name', '')
        if payload['active']:
            row.status = Student.Status.ACTIVE
        row.full_clean()
        row.save()
        # Close previous enrollment instead of rewriting/deleting journal history.
        for enrollment in row.enrollments.filter(student_class__academic_year_id=settings.ROSTER_YEAR_ID, is_active=True):
            if enrollment.student_class_id != group.pk or not payload['active']:
                enrollment.is_active = False
                enrollment.end_date = timezone.localdate()
                enrollment.save(update_fields=['is_active', 'end_date'])
        if payload['active']:
            if not row.enrollments.filter(student_class=group, is_active=True).exists():
                ClassEnrollment.objects.create(student=row, student_class=group,
                    start_date=max(group.academic_year.start_date, min(timezone.localdate(), group.academic_year.end_date)), is_active=True)

    def create(self, kind, payload):
        if kind == 'class':
            row = StudentClass(school_id=settings.ROSTER_SCHOOL_ID, academic_year_id=settings.ROSTER_YEAR_ID,
                name=payload['name'], grade_level=payload.get('grade_level', 1), letter=payload.get('letter', ''), integration_active=payload['active'])
            row.full_clean()
            row.save()
            return row.pk
        group = classes().get(pk=payload['native_class_id'])
        row = Student(school_id=settings.ROSTER_SCHOOL_ID, first_name=payload.get('first_name', ''),
            last_name=payload.get('last_name', ''), middle_name=payload.get('middle_name', ''))
        row.full_clean()
        row.save()
        ClassEnrollment.objects.create(student=row, student_class=group, start_date=group.academic_year.start_date,
            is_active=payload['active'], end_date=None if payload['active'] else group.academic_year.start_date)
        return row.pk
