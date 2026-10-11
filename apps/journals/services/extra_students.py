"""Teacher decisions about one stable source identity, never a name match."""
from django.db.models import Q
from apps.curriculum.models import Student, StudentClass, ClassEnrollment
from apps.journals.models import Journal, ExtraStudent, DeletedAPIIdentity, AuditLog, GradeStudentMapping, ExternalGrade
from apps.portal.permissions import has_journal_access
from .external_grades import ExternalAPIError


def extra_parent(journal):
    """Recognize the existing dedicated table, never identify a pupil by name."""
    linked = ExtraStudent.objects.filter(separate_journal=journal).select_related('connection__journal').first()
    if linked:
        return linked.connection.journal
    suffix = ' (Позакласні)'
    if journal.nz_journal_id or journal.student_class.letter != 'ПК' or not journal.student_class.name.endswith(suffix):
        return None
    return Journal.objects.filter(school=journal.school, academic_year=journal.academic_year,
        subject=journal.subject, teacher=journal.teacher,
        student_class__name=journal.student_class.name[:-len(suffix)]).first()


def delete_separate_student(journal, student, user):
    parent = extra_parent(journal)
    if not parent or not has_journal_access(user, parent, write=True):
        raise ExternalAPIError('Це не окремий позакласний журнал або немає права його змінювати.')
    linked = ExtraStudent.objects.select_for_update().filter(separate_student=student, separate_journal=journal).first()
    if linked:
        return delete_extra(linked, user)
    if (student.enrollments.exclude(student_class=journal.student_class).exists()
            or GradeStudentMapping.objects.filter(student=student).exists()
            or ExtraStudent.objects.filter(matched_student=student).exists()
            or student.grades.exclude(lesson__journal=journal).exists()
            or student.attendances.exclude(lesson__journal=journal).exists()
            or student.summary_grades.exclude(journal=journal).exists()
            or ExternalGrade.objects.filter(student=student).exclude(lesson__journal=journal).exists()):
        raise ExternalAPIError('Учень має інші прив’язки. Видалення чужої історії заборонено.')
    pk, name, grades = student.pk, student.get_full_name_ukr(), student.grades.count()
    AuditLog.objects.create(school=journal.school, user=user, action=AuditLog.Action.DELETE,
        target_model='Student', target_id=str(pk), description=f'Видалено позакласного учня «{name}» та оцінки ({grades}).'[:255],
        changes={'journal_id': parent.pk, 'separate_journal_id': journal.pk, 'grades_deleted': grades})
    student.delete()
    return grades, 0


def identity_records(extra):
    query = Q(external_id__in=extra.results.values('external_id'))
    if extra.external_key.isdigit():
        query |= Q(source_payload__student_id=int(extra.external_key)) | Q(source_payload__student_id=extra.external_key)
    elif extra.external_key.startswith('unmapped:') and extra.external_key[9:].isdigit():
        query |= Q(source_payload__attempt_id=int(extra.external_key[9:]))
    return extra.connection.records.filter(query)


def audit(extra, user, action, description, **changes):
    return AuditLog.objects.create(school=extra.connection.journal.school, user=user, action=action,
        target_model='ExtraStudent', target_id=str(extra.pk), description=description[:255],
        changes={'journal_id': extra.connection.journal_id, 'connection_id': extra.connection_id,
            'external_key': extra.external_key, **changes})


def match_student(extra, student, user):
    if extra.separate_student_id:
        raise ExternalAPIError('Учня вже залишено в окремому журналі. Видалення або перенесення його історії потребує окремого рішення.')
    if identity_records(extra).exclude(student=student).exists():
        raise ExternalAPIError('Відповідність уже має імпортовану історію іншого учня. Переприв’язування заборонено.')
    previous = extra.matched_student_id
    extra.matched_student = student
    extra.save(update_fields=['matched_student', 'updated_at'])
    audit(extra, user, AuditLog.Action.UPDATE, f'Підтверджено відповідність «{extra.name}»: «{student.get_full_name_ukr()}».',
        matched_student={'old': previous, 'new': student.pk})


def keep_separate(extra, user):
    if extra.matched_student_id or identity_records(extra).exists():
        raise ExternalAPIError('Учень уже має відповідність класу або імпортовану історію. Її перенесення потребує окремого рішення.')
    if extra.separate_student_id:
        return extra.separate_journal, False
    journal = extra.connection.journal
    group, _ = StudentClass.objects.get_or_create(school=journal.school, academic_year=journal.academic_year,
        name=f'{journal.student_class.name} (Позакласні)',
        defaults={'grade_level': journal.student_class.grade_level, 'letter': 'ПК'})
    separate, _ = Journal.objects.get_or_create(school=journal.school, academic_year=journal.academic_year,
        student_class=group, subject=journal.subject, defaults={'teacher': journal.teacher})
    if not has_journal_access(user, separate, write=True):
        raise ExternalAPIError('Окремий журнал належить іншому вчителю. Зверніться до адміністратора.')
    names = extra.name.strip().split(None, 1)
    # Reuse only this stored identity, never an existing pupil with the same name.
    student = Student.objects.create(school=journal.school, last_name=names[0] if names else 'Учень',
        first_name=names[1] if len(names) > 1 else '', notes=f'Позакласний учень ({journal.student_class.name})')
    ClassEnrollment.objects.create(student=student, student_class=group)
    extra.separate_student, extra.separate_journal = student, separate
    extra.save(update_fields=['separate_student', 'separate_journal', 'updated_at'])
    audit(extra, user, AuditLog.Action.CREATE, f'Учня «{extra.name}» залишено в окремому журналі.',
        separate_student=student.pk, separate_journal=separate.pk)
    return separate, True


def delete_extra(extra, user):
    journal = extra.connection.journal
    student = extra.separate_student
    if student:
        if not has_journal_access(user, extra.separate_journal, write=True):
            raise ExternalAPIError('Немає права редагувати окремий журнал.')
        if (student.enrollments.exclude(student_class=extra.separate_journal.student_class).exists()
                or GradeStudentMapping.objects.filter(student=student).exists()
                or ExtraStudent.objects.filter(matched_student=student).exists()
                or student.grades.exclude(lesson__journal=extra.separate_journal).exists()
                or student.attendances.exclude(lesson__journal=extra.separate_journal).exists()
                or student.summary_grades.exclude(journal=extra.separate_journal).exists()
                or ExternalGrade.objects.filter(student=student).exclude(lesson__journal=extra.separate_journal).exists()):
            raise ExternalAPIError('Цей учень уже має інші прив’язки. Спочатку перевірте їх; автоматичне видалення чужої історії заборонено.')
    results = extra.results.count()
    imported = identity_records(extra)
    # Confirmed ordinary classmates and their grades remain independent.
    preserved = imported.count() if not student else 0
    if student:
        imported.filter(student=student, lesson__journal=extra.separate_journal).delete()
    DeletedAPIIdentity.objects.get_or_create(connection=extra.connection, external_key=extra.external_key)
    audit(extra, user, AuditLog.Action.DELETE, f'Видалено позакласний запис «{extra.name}» та збережені результати ({results}).',
        results_deleted=results, separate_student_deleted=student.pk if student else None,
        imported_grades_preserved=preserved)
    extra.results.all().delete()
    extra.delete()
    if student:
        student.delete()
    return results, preserved
