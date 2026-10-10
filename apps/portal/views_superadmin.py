from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.core.paginator import Paginator
from apps.portal.permissions import school_admin_required, admin_schools
from django.core.exceptions import PermissionDenied
from apps.schools.models import School
from apps.portal.forms_admin import SchoolEditForm
from apps.accounts.models import User, TeacherProfile
from apps.curriculum.models import (
    AcademicYear, Semester, Subject, StudentClass, Student, ClassEnrollment, TeacherAssignment
)
from apps.journals.models import Journal, AuditLog


@school_admin_required
def superadmin_dashboard(request):
    """Головний екран кабінету суперадміністратора."""
    schools = admin_schools(request.user)
    schools_count = schools.count()
    teachers_count = User.objects.filter(role=User.Role.TEACHER, school__in=schools).count()
    students_count = Student.objects.filter(school__in=schools).count()
    journals_count = Journal.objects.filter(school__in=schools).count()

    logs = AuditLog.objects.all() if request.user.is_superadmin_user else AuditLog.objects.filter(school__in=schools)
    recent_logs = logs.select_related('school', 'user').order_by('-timestamp')[:10]
    recent_schools = schools.order_by('-created_at')[:5]

    context = {
        'schools_count': schools_count,
        'teachers_count': teachers_count,
        'students_count': students_count,
        'journals_count': journals_count,
        'recent_logs': recent_logs,
        'recent_schools': recent_schools,
    }
    return render(request, 'portal/superadmin/dashboard.html', context)


@school_admin_required
def schools_list(request):
    """Керування школами: список та створення нової школи."""
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            if not request.user.is_superadmin_user:
                raise PermissionDenied('Створіть додаткову школу через окрему реєстрацію.')
            name = request.POST.get('name', '').strip()
            short_name = request.POST.get('short_name', '').strip()
            edrpou_code = request.POST.get('edrpou_code', '').strip() or None
            address = request.POST.get('address', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()

            if not name:
                messages.error(request, "Повна назва школи є обов'язковою.")
            else:
                school = School.objects.create(
                    name=name,
                    short_name=short_name,
                    edrpou_code=edrpou_code,
                    address=address,
                    phone=phone,
                    email=email,
                    is_active=True
                )
                AuditLog.objects.create(
                    school=school,
                    user=request.user,
                    action=AuditLog.Action.CREATE,
                    target_model='School',
                    target_id=str(school.id),
                    description=f"Створено школу: {school.name}"
                )
                messages.success(request, f"Школу «{school.name}» успішно створено!")
                return redirect('portal:superadmin_schools')

        elif action == 'edit':
            school_id = request.POST.get('school_id')
            school = get_object_or_404(admin_schools(request.user), id=school_id)
            form = SchoolEditForm(request.POST, instance=school)
            if not form.is_valid():
                messages.error(request, form.errors.as_text())
                return redirect('portal:superadmin_schools')
            school = form.save()

            AuditLog.objects.create(
                school=school,
                user=request.user,
                action=AuditLog.Action.UPDATE,
                target_model='School',
                target_id=str(school.id),
                description=f"Оновлено дані школи: {school.name}"
            )
            messages.success(request, f"Дані школи «{school.name}» оновлено!")
            return redirect('portal:superadmin_schools')

        elif action == 'toggle_active':
            if not request.user.is_superadmin_user:
                raise PermissionDenied('Зміну доступності школи виконує суперадміністратор.')
            school_id = request.POST.get('school_id')
            school = get_object_or_404(admin_schools(request.user), id=school_id)
            school.is_active = not school.is_active
            school.save()

            status_str = "активовано" if school.is_active else "заархівовано / деактивовано"
            AuditLog.objects.create(
                school=school,
                user=request.user,
                action=AuditLog.Action.UPDATE,
                target_model='School',
                target_id=str(school.id),
                description=f"Школу «{school.name}» {status_str}"
            )
            messages.info(request, f"Статус школи «{school.name}» змінено: {status_str}.")
            return redirect('portal:superadmin_schools')

    schools = admin_schools(request.user).order_by('-is_active', 'name')
    for school in schools:
        school.edit_form = SchoolEditForm(instance=school)
    return render(request, 'portal/superadmin/schools.html', {'schools': schools})


@school_admin_required
def teachers_list(request):
    """Керування обліковими записами вчителів: створення, блокування, скидання паролів."""
    schools = admin_schools(request.user).filter(is_active=True).order_by('name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            username = request.POST.get('username', '').strip()
            password = request.POST.get('password', '').strip()
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            patronymic = request.POST.get('patronymic', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            school_id = request.POST.get('school_id')
            qualification = request.POST.get('qualification', '').strip()
            pedagogical_title = request.POST.get('pedagogical_title', '').strip()

            if not username or not password or not last_name or not first_name or not school_id:
                messages.error(request, "Будь ласка, заповніть обов'язкові поля: Логін, Пароль, Прізвище, Ім'я та Школу.")
            elif User.objects.filter(username=username).exists():
                messages.error(request, f"Користувач із логіном «{username}» вже існує.")
            else:
                school = get_object_or_404(admin_schools(request.user), id=school_id)
                with transaction.atomic():
                    teacher = User.objects.create_user(
                        username=username,
                        password=password,
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        role=User.Role.TEACHER,
                        school=school,
                        patronymic=patronymic,
                        phone=phone,
                        is_active=True
                    )
                    TeacherProfile.objects.create(
                        user=teacher,
                        qualification=qualification,
                        pedagogical_title=pedagogical_title
                    )
                    AuditLog.objects.create(
                        school=school,
                        user=request.user,
                        action=AuditLog.Action.CREATE,
                        target_model='User',
                        target_id=str(teacher.id),
                        description=f"Створено обліковий запис вчителя: {teacher.get_full_name_ukr()}"
                    )
                messages.success(request, f"Вчителя «{teacher.get_full_name_ukr()}» успішно зареєстровано!")
                return redirect('portal:superadmin_teachers')

        elif action == 'reset_password':
            teacher_id = request.POST.get('teacher_id')
            new_password = request.POST.get('new_password', '').strip()
            teacher = get_object_or_404(User, id=teacher_id, role=User.Role.TEACHER, school__in=admin_schools(request.user))

            if len(new_password) < 6:
                messages.error(request, "Пароль повинен містити щонайменше 6 символів.")
            else:
                teacher.set_password(new_password)
                teacher.save()
                AuditLog.objects.create(
                    school=teacher.school,
                    user=request.user,
                    action=AuditLog.Action.UPDATE,
                    target_model='User',
                    target_id=str(teacher.id),
                    description=f"Скинуто пароль для вчителя: {teacher.get_full_name_ukr()}"
                )
                messages.success(request, f"Пароль для вчителя {teacher.get_full_name_ukr()} успішно змінено!")
                return redirect('portal:superadmin_teachers')

        elif action == 'toggle_active':
            teacher_id = request.POST.get('teacher_id')
            teacher = get_object_or_404(User, id=teacher_id, role=User.Role.TEACHER, school__in=admin_schools(request.user))
            teacher.is_active = not teacher.is_active
            teacher.save()

            st = "розблоковано" if teacher.is_active else "заблоковано"
            AuditLog.objects.create(
                school=teacher.school,
                user=request.user,
                action=AuditLog.Action.UPDATE,
                target_model='User',
                target_id=str(teacher.id),
                description=f"Обліковий запис {teacher.get_full_name_ukr()} {st}"
            )
            messages.info(request, f"Обліковий запис {teacher.get_full_name_ukr()} {st}.")
            return redirect('portal:superadmin_teachers')

    teachers = User.objects.filter(role=User.Role.TEACHER, school__in=admin_schools(request.user)).select_related('school', 'teacher_profile').order_by('last_name')
    return render(request, 'portal/superadmin/teachers.html', {'teachers': teachers, 'schools': schools})


@school_admin_required
def curriculum_management(request):
    """Керування навчальною структурою: роки, семестри, предмети, класи."""
    selected_school_id = request.GET.get('school_id') or request.POST.get('school_id')
    schools = admin_schools(request.user).filter(is_active=True).order_by('name')
    selected_school = get_object_or_404(schools, id=selected_school_id) if selected_school_id else schools.first()

    if request.method == 'POST' and selected_school:
        action = request.POST.get('action')

        if action == 'create_year':
            name = request.POST.get('year_name', '').strip()
            start_date = request.POST.get('start_date')
            end_date = request.POST.get('end_date')
            is_current = request.POST.get('is_current') == 'on'

            if name and start_date and end_date:
                ay, created = AcademicYear.objects.get_or_create(
                    school=selected_school,
                    name=name,
                    defaults={'start_date': start_date, 'end_date': end_date, 'is_current': is_current}
                )
                if created:
                    # Automatically generate 2 semesters
                    Semester.objects.create(
                        academic_year=ay,
                        number=1,
                        name="I семестр",
                        start_date=start_date,
                        end_date=timezone.datetime.strptime(start_date, '%Y-%m-%d').date() + timezone.timedelta(days=115)
                    )
                    Semester.objects.create(
                        academic_year=ay,
                        number=2,
                        name="II семестр",
                        start_date=timezone.datetime.strptime(start_date, '%Y-%m-%d').date() + timezone.timedelta(days=125),
                        end_date=end_date
                    )
                    messages.success(request, f"Навчальний рік «{name}» та семестри успішно створено!")
                else:
                    messages.warning(request, f"Навчальний рік «{name}» вже існує.")

        elif action == 'create_subject':
            name = request.POST.get('subject_name', '').strip()
            code = request.POST.get('subject_code', '').strip()
            if name:
                subj, created = Subject.objects.get_or_create(
                    school=selected_school,
                    name=name,
                    defaults={'code': code}
                )
                if created:
                    messages.success(request, f"Предмет «{name}» успішно додано!")
                else:
                    messages.warning(request, f"Предмет «{name}» вже існує.")

        elif action == 'create_class':
            name = request.POST.get('class_name', '').strip()
            academic_year_id = request.POST.get('academic_year_id')
            grade_level = int(request.POST.get('grade_level', 5))
            teacher_id = request.POST.get('class_teacher_id')

            year = get_object_or_404(AcademicYear, id=academic_year_id, school=selected_school)
            ct = User.objects.filter(id=teacher_id, school=selected_school).first() if teacher_id else None

            if name:
                sc, created = StudentClass.objects.get_or_create(
                    school=selected_school,
                    academic_year=year,
                    name=name,
                    defaults={'grade_level': grade_level, 'class_teacher': ct}
                )
                if created:
                    messages.success(request, f"Клас «{name}» успішно створено!")
                else:
                    messages.warning(request, f"Клас «{name}» у цьому навчальному році вже існує.")

        return redirect(f"{request.path}?school_id={selected_school.id}")

    years = AcademicYear.objects.filter(school=selected_school).prefetch_related('semesters') if selected_school else []
    subjects = Subject.objects.filter(school=selected_school).order_by('name') if selected_school else []
    classes = StudentClass.objects.filter(school=selected_school).select_related('academic_year', 'class_teacher') if selected_school else []
    teachers = User.objects.filter(school=selected_school, role=User.Role.TEACHER, is_active=True).order_by('last_name') if selected_school else []

    context = {
        'schools': schools,
        'selected_school': selected_school,
        'years': years,
        'subjects': subjects,
        'classes': classes,
        'teachers': teachers,
    }
    return render(request, 'portal/superadmin/curriculum.html', context)


@school_admin_required
def assignments_management(request):
    """Призначення вчителів до предметів і класів."""
    selected_school_id = request.GET.get('school_id') or request.POST.get('school_id')
    schools = admin_schools(request.user).filter(is_active=True).order_by('name')
    selected_school = get_object_or_404(schools, id=selected_school_id) if selected_school_id else schools.first()

    if request.method == 'POST' and selected_school:
        action = request.POST.get('action')

        if action == 'create':
            teacher_id = request.POST.get('teacher_id')
            subject_id = request.POST.get('subject_id')
            class_id = request.POST.get('class_id')
            year_id = request.POST.get('year_id')

            teacher = get_object_or_404(User, id=teacher_id, school=selected_school)
            subject = get_object_or_404(Subject, id=subject_id, school=selected_school)
            student_class = get_object_or_404(StudentClass, id=class_id, school=selected_school)
            year = get_object_or_404(AcademicYear, id=year_id, school=selected_school)

            assignment, created = TeacherAssignment.objects.get_or_create(
                school=selected_school,
                teacher=teacher,
                subject=subject,
                student_class=student_class,
                academic_year=year,
                defaults={'is_active': True}
            )
            if created:
                AuditLog.objects.create(
                    school=selected_school,
                    user=request.user,
                    action=AuditLog.Action.CREATE,
                    target_model='TeacherAssignment',
                    target_id=str(assignment.id),
                    description=f"Призначено вчителя {teacher.get_full_name_ukr()} на {subject.name} ({student_class.name})"
                )
                messages.success(request, f"Призначення успішно створено для {teacher.get_full_name_ukr()}!")
            else:
                messages.warning(request, "Таке призначення вже існує.")

        elif action == 'delete':
            assignment_id = request.POST.get('assignment_id')
            assignment = get_object_or_404(TeacherAssignment, id=assignment_id, school=selected_school)
            assignment.delete()
            messages.info(request, "Призначення видалено.")

        return redirect(f"{request.path}?school_id={selected_school.id}")

    assignments = TeacherAssignment.objects.filter(
        school=selected_school
    ).select_related('teacher', 'subject', 'student_class', 'academic_year').order_by('student_class__name', 'subject__name') if selected_school else []

    teachers = User.objects.filter(school=selected_school, role=User.Role.TEACHER, is_active=True).order_by('last_name') if selected_school else []
    subjects = Subject.objects.filter(school=selected_school, is_active=True).order_by('name') if selected_school else []
    classes = StudentClass.objects.filter(school=selected_school).order_by('name') if selected_school else []
    years = AcademicYear.objects.filter(school=selected_school).order_by('-start_date') if selected_school else []

    context = {
        'schools': schools,
        'selected_school': selected_school,
        'assignments': assignments,
        'teachers': teachers,
        'subjects': subjects,
        'classes': classes,
        'years': years,
    }
    return render(request, 'portal/superadmin/assignments.html', context)


@school_admin_required
def students_management(request):
    """
    Реєстр учнів: створення, редагування, переведення між класами з фіксацією історії.
    """
    selected_school_id = request.GET.get('school_id') or request.POST.get('school_id')
    schools = admin_schools(request.user).filter(is_active=True).order_by('name')
    selected_school = get_object_or_404(schools, id=selected_school_id) if selected_school_id else schools.first()

    selected_class_id = request.GET.get('class_id')
    selected_class = None
    if selected_class_id and selected_school:
        selected_class = StudentClass.objects.filter(id=selected_class_id, school=selected_school).first()

    if request.method == 'POST' and selected_school:
        action = request.POST.get('action')

        if action == 'create':
            last_name = request.POST.get('last_name', '').strip()
            first_name = request.POST.get('first_name', '').strip()
            middle_name = request.POST.get('middle_name', '').strip()
            internal_id = request.POST.get('internal_id', '').strip()
            class_id = request.POST.get('class_id')

            if not last_name or not first_name:
                messages.error(request, "Прізвище та ім'я учня є обов'язковими.")
            else:
                with transaction.atomic():
                    student = Student.objects.create(
                        school=selected_school,
                        internal_id=internal_id,
                        last_name=last_name,
                        first_name=first_name,
                        middle_name=middle_name,
                        status=Student.Status.ACTIVE
                    )
                    if class_id:
                        sc = get_object_or_404(StudentClass, id=class_id, school=selected_school)
                        ClassEnrollment.objects.create(
                            student=student,
                            student_class=sc,
                            start_date=timezone.now().date(),
                            is_active=True,
                            transfer_note="Початкове зарахування"
                        )
                    AuditLog.objects.create(
                        school=selected_school,
                        user=request.user,
                        action=AuditLog.Action.CREATE,
                        target_model='Student',
                        target_id=str(student.id),
                        description=f"Додано учня: {student.get_full_name_ukr()}"
                    )
                messages.success(request, f"Учня «{student.get_full_name_ukr()}» успішно додано!")
                return redirect(f"{request.path}?school_id={selected_school.id}")

        elif action == 'transfer':
            # Переведення учня до іншого класу зі збереженням історії
            student_id = request.POST.get('student_id')
            new_class_id = request.POST.get('new_class_id')
            reason = request.POST.get('reason', '').strip() or "Переведення до іншого класу"

            student = get_object_or_404(Student, id=student_id, school=selected_school)
            new_class = get_object_or_404(StudentClass, id=new_class_id, school=selected_school)

            with transaction.atomic():
                # 1. Завершуємо попередні активні зарахування (НЕ видаляємо!)
                old_enrollments = student.enrollments.filter(is_active=True)
                old_classes_names = []
                for enr in old_enrollments:
                    old_classes_names.append(enr.student_class.name)
                    enr.is_active = False
                    enr.end_date = timezone.now().date()
                    enr.transfer_note = f"Переведено до {new_class.name}: {reason}"
                    enr.save()

                # 2. Створюємо нове зарахування
                ClassEnrollment.objects.create(
                    student=student,
                    student_class=new_class,
                    start_date=timezone.now().date(),
                    is_active=True,
                    transfer_note=reason
                )

                old_name_str = ", ".join(old_classes_names) or "Без класу"
                AuditLog.objects.create(
                    school=selected_school,
                    user=request.user,
                    action=AuditLog.Action.UPDATE,
                    target_model='Student',
                    target_id=str(student.id),
                    description=f"Переведення учня {student.get_full_name_ukr()}: {old_name_str} -> {new_class.name}",
                    changes={'from_class': old_name_str, 'to_class': new_class.name, 'reason': reason}
                )

            messages.success(request, f"Учня {student.get_full_name_ukr()} переведено до {new_class.name}! Попередня історія оцінок збережена.")
            return redirect(f"{request.path}?school_id={selected_school.id}")

    students_qs = Student.objects.filter(school=selected_school).prefetch_related('enrollments__student_class') if selected_school else Student.objects.none()
    if selected_class:
        students_qs = students_qs.filter(enrollments__student_class=selected_class, enrollments__is_active=True)

    classes = StudentClass.objects.filter(school=selected_school).order_by('name') if selected_school else []

    context = {
        'schools': schools,
        'selected_school': selected_school,
        'selected_class': selected_class,
        'students': students_qs.distinct(),
        'classes': classes,
    }
    return render(request, 'portal/superadmin/students.html', context)


@school_admin_required
def audit_log_view(request):
    """Перегляд журналу аудиту дій із фільтрацією."""
    selected_school_id = request.GET.get('school_id')
    action_filter = request.GET.get('action')
    search_query = request.GET.get('q', '').strip()

    logs = AuditLog.objects.all() if request.user.is_superadmin_user else AuditLog.objects.filter(school__in=admin_schools(request.user))
    logs_qs = logs.select_related('school', 'user').order_by('-timestamp')

    if selected_school_id:
        logs_qs = logs_qs.filter(school_id=selected_school_id)
    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)
    if search_query:
        logs_qs = logs_qs.filter(description__icontains=search_query)

    paginator = Paginator(logs_qs, 30)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    schools = admin_schools(request.user).order_by('name')

    context = {
        'page_obj': page_obj,
        'schools': schools,
        'selected_school_id': selected_school_id,
        'action_filter': action_filter,
        'search_query': search_query,
        'actions': AuditLog.Action.choices,
    }
    return render(request, 'portal/superadmin/audit_log.html', context)
