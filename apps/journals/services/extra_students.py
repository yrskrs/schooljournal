import datetime as dt
from django.db.models import Q
from apps.curriculum.models import Student, StudentClass, ClassEnrollment
from apps.journals.models import (
    Journal, ExtraStudent, DeletedAPIIdentity, AuditLog, GradeStudentMapping,
    ExternalGrade, Lesson, Grade, LessonType
)
from apps.portal.permissions import has_journal_access
from .external_grades import ExternalAPIError


def sync_topics_from_main_journal(extra_journal, main_journal=None):
    """
    Підтягує або оновлює теми уроків з основного навчального журналу
    в окремий позакласний журнал вчителя.
    """
    if not main_journal:
        main_journal = extra_parent(extra_journal)
    if not main_journal:
        return 0
    created_count = 0
    updated_count = 0
    for l in main_journal.lessons.all():
        extra_lesson, created = Lesson.objects.get_or_create(
            journal=extra_journal,
            date=l.date,
            lesson_number=l.lesson_number,
            defaults={
                'topic': l.topic,
                'homework': l.homework,
                'lesson_type': l.lesson_type,
                'semester': l.semester,
                'lesson_time': l.lesson_time,
                'created_by': l.created_by,
            }
        )
        if created:
            created_count += 1
        else:
            to_update = []
            if not extra_lesson.topic and l.topic:
                extra_lesson.topic = l.topic
                to_update.append('topic')
            if not extra_lesson.homework and l.homework:
                extra_lesson.homework = l.homework
                to_update.append('homework')
            if extra_lesson.lesson_type != l.lesson_type and extra_lesson.lesson_type == LessonType.ORDINARY:
                extra_lesson.lesson_type = l.lesson_type
                to_update.append('lesson_type')
            if not extra_lesson.semester_id and l.semester_id:
                extra_lesson.semester = l.semester
                to_update.append('semester')
            if to_update:
                to_update.append('updated_at')
                extra_lesson.save(update_fields=to_update)
                updated_count += 1
    return created_count + updated_count


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
    if identity_records(extra).exclude(student=student).exists():
        raise ExternalAPIError('Відповідність уже має імпортовану історію іншого учня. Переприв’язування заборонено.')
    sep_student = extra.separate_student
    sep_journal = extra.separate_journal
    previous = extra.matched_student_id
    extra.matched_student = student
    extra.is_separate = False
    extra.separate_student = None
    extra.separate_journal = None
    extra.save(update_fields=['matched_student', 'is_separate', 'separate_student', 'separate_journal', 'updated_at'])
    if sep_student:
        if sep_journal:
            sep_student.grades.filter(lesson__journal=sep_journal).delete()
        sep_student.delete()
    audit(extra, user, AuditLog.Action.UPDATE, f'Підтверджено відповідність «{extra.name}»: «{student.get_full_name_ukr()}».',
        matched_student={'old': previous, 'new': student.pk})


def keep_separate(extra, user):
    if extra.matched_student_id or identity_records(extra).exists():
        raise ExternalAPIError('Учень уже має відповідність класу або імпортовану історію. Її перенесення потребує окремого рішення.')
    journal = extra.connection.journal
    if extra.separate_student_id and extra.separate_journal_id:
        return extra.separate_journal, False

    extra_class_name = f"{journal.student_class.name} (Позакласні)"
    extra_class, _ = StudentClass.objects.get_or_create(
        school=journal.school,
        academic_year=journal.academic_year,
        name=extra_class_name,
        defaults={
            'grade_level': journal.student_class.grade_level,
            'letter': 'ПК',
        }
    )

    extra_journal, _ = Journal.objects.get_or_create(
        school=journal.school,
        academic_year=journal.academic_year,
        student_class=extra_class,
        subject=journal.subject,
        defaults={
            'teacher': journal.teacher,
            'nz_journal_id': '',
        }
    )
    if not has_journal_access(user, extra_journal, write=True):
        raise ExternalAPIError('Окремий журнал належить іншому вчителю. Зверніться до адміністратора.')

    student = extra.separate_student
    if not student:
        names = extra.name.strip().split(None, 1)
        last_name = names[0] if names else 'Учень'
        first_name = names[1] if len(names) > 1 else ''
        student = Student.objects.create(
            school=journal.school,
            last_name=last_name,
            first_name=first_name,
            notes=f'Позакласний учень ({journal.student_class.name})'
        )

    ClassEnrollment.objects.get_or_create(
        student=student,
        student_class=extra_class,
        defaults={'is_active': True}
    )

    created = not extra.is_separate
    extra.separate_student = student
    extra.separate_journal = extra_journal
    extra.is_separate = True
    extra.save(update_fields=['separate_student', 'separate_journal', 'is_separate', 'updated_at'])

    # Підтягуємо теми уроків з основного журналу
    sync_topics_from_main_journal(extra_journal, journal)

    # Переносимо результати та оцінки учня в окремий журнал позакласних
    for res in extra.results.filter(is_ignored=False):
        payload = res.payload
        d_str = payload.get('date')
        val = payload.get('value')
        if d_str and val is not None:
            try:
                lesson_date = dt.date.fromisoformat(d_str)
                lesson = extra_journal.lessons.filter(date=lesson_date).first()
                if not lesson:
                    main_lesson = journal.lessons.filter(date=lesson_date).first()
                    topic = main_lesson.topic if main_lesson else (payload.get('title') or 'Позакласна робота')
                    homework = main_lesson.homework if main_lesson else ''
                    l_type = main_lesson.lesson_type if main_lesson else LessonType.ORDINARY
                    sem = main_lesson.semester if main_lesson else None
                    l_num = main_lesson.lesson_number if main_lesson else 1
                    lesson = Lesson.objects.create(
                        journal=extra_journal,
                        date=lesson_date,
                        lesson_number=l_num,
                        topic=topic,
                        homework=homework,
                        lesson_type=l_type,
                        semester=sem,
                        created_by=user
                    )
                Grade.objects.update_or_create(
                    lesson=lesson,
                    student=student,
                    defaults={'value': str(val), 'is_approved': True}
                )
                res.resolved = True
                res.reason = 'Оцінку збережено в журналі позакласних учнів.'
                res.save(update_fields=['resolved', 'reason', 'updated_at'])
            except Exception:
                pass

    if created:
        audit(extra, user, AuditLog.Action.CREATE,
              f'Учня «{extra.name}» додано до журналу позакласних учнів «{extra_journal.student_class.name}».',
              separate_student=student.pk, separate_journal=extra_journal.pk)

    return extra_journal, created


def merge_extra(extra, target, user):
    """Only a teacher's explicit decision may connect old per-work identities."""
    if (extra.pk == target.pk or extra.connection_id != target.connection_id
            or extra.alias_of_id or target.alias_of_id or extra.aliases.exists()
            or extra.matched_student_id or target.matched_student_id
            or extra.separate_student_id or target.separate_student_id
            or identity_records(extra).exists() or identity_records(target).exists()):
        raise ExternalAPIError('Об’єднання недоступне: виберіть два окремі записи одного підключення без імпортованої історії класу.')
    extra.results.update(extra_student=target)
    extra.alias_of = target
    extra.save(update_fields=['alias_of', 'updated_at'])
    if target.is_separate:
        target.results.filter(is_ignored=False).update(resolved=True, reason='Збережено для окремого учня без зарахування до класу.')
    audit(extra, user, AuditLog.Action.UPDATE, 'Учитель підтвердив, що два записи належать одному окремому учню.', canonical_extra=target.pk)


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
    for alias in extra.aliases.all():
        DeletedAPIIdentity.objects.get_or_create(connection=alias.connection, external_key=alias.external_key)
    extra.aliases.all().delete()
    audit(extra, user, AuditLog.Action.DELETE, f'Видалено позакласний запис «{extra.name}» та збережені результати ({results}).',
        results_deleted=results, separate_student_deleted=student.pk if student else None,
        imported_grades_preserved=preserved)
    extra.results.all().delete()
    extra.delete()
    if student:
        student.delete()
    return results, preserved
