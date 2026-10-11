from collections import OrderedDict
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import JsonResponse
from apps.accounts.models import TeacherProfile
from apps.curriculum.models import TeacherAssignment, AcademicYear, StudentClass, Subject
from apps.journals.models import Journal, Lesson, Grade, Attendance, AuditLog
from apps.portal.permissions import get_user_journals


@login_required
def teacher_dashboard(request):
    """Головний екран кабінету вчителя."""
    if request.user.is_superadmin_user:
        return redirect('portal:superadmin_dashboard')

    if not request.user.school:
        return render(request, 'portal/teacher/no_school.html')

    # Журнали вчителя
    from django.db.models import Q
    my_journals = get_user_journals(request.user).filter(
        Q(teacher=request.user) | Q(nz_owner__user=request.user) | Q(local_profile=request.user)
    ).order_by('academic_year', 'student_class__name')

    # Журнали з дозволом на заміну
    override_journals = Journal.objects.filter(
        school=request.user.school,
        allows_override_teacher=True,
        nz_owner__isnull=True
    ).exclude(teacher=request.user).select_related('academic_year', 'student_class', 'subject', 'teacher')

    # Призначення вчителя
    assignments = TeacherAssignment.objects.filter(
        school=request.user.school,
        teacher=request.user,
        is_active=True
    ).select_related('academic_year', 'student_class', 'subject')

    # Знаходимо призначення, для яких журнал ще НЕ створено
    existing_combinations = set(
        my_journals.values_list('academic_year_id', 'student_class_id', 'subject_id')
    )
    uncreated_assignments = []
    for a in assignments:
        key = (a.academic_year_id, a.student_class_id, a.subject_id)
        if key not in existing_combinations:
            uncreated_assignments.append(a)

    nz_account = getattr(request.user, 'nz_account', None)
    for j in my_journals:
        j.unsynced_count = (
            Grade.objects.filter(lesson__journal=j, is_synced_to_nz=False, is_ignored_for_nz=False).count() +
            Attendance.objects.filter(lesson__journal=j, is_synced_to_nz=False, is_ignored_for_nz=False).count() +
            Lesson.objects.filter(journal=j, is_synced_to_nz=False, is_ignored_for_nz=False).count()
        )

    # Групування журналів за блоками класів із можливістю впорядкування
    profile, _ = TeacherProfile.objects.get_or_create(user=request.user)
    saved_order = profile.class_order or []

    class_groups = OrderedDict()
    for j in my_journals:
        sc = j.student_class
        if sc.id not in class_groups:
            class_groups[sc.id] = {
                'student_class': sc,
                'journals': [],
            }
        class_groups[sc.id]['journals'].append(j)

    def class_sort_key(item):
        cid, data = item
        if cid in saved_order:
            return (0, saved_order.index(cid))
        return (1, data['student_class'].grade_level, data['student_class'].name)

    class_blocks = [data for _, data in sorted(class_groups.items(), key=class_sort_key)]

    context = {
        'my_journals': my_journals,
        'class_blocks': class_blocks,
        'override_journals': override_journals,
        'assignments': assignments,
        'uncreated_assignments': uncreated_assignments,
        'school': request.user.school,
        'nz_account': nz_account,
    }
    return render(request, 'portal/teacher/dashboard.html', context)


@login_required
def update_class_order(request):
    """Оновлення послідовності розміщення блоків класів у кабінеті вчителя."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Метод не підтримується'}, status=405)

    profile, _ = TeacherProfile.objects.get_or_create(user=request.user)
    try:
        data = json.loads(request.body.decode('utf-8'))
        class_ids = data.get('class_ids', [])
    except Exception:
        class_ids = request.POST.getlist('class_ids[]') or request.POST.getlist('class_ids')

    valid_ids = [int(cid) for cid in class_ids if str(cid).isdigit()]
    profile.class_order = valid_ids
    profile.save(update_fields=['class_order'])
    return JsonResponse({'status': 'ok', 'class_order': valid_ids})


@login_required
def create_journal(request):
    """
    Створення журналу вчителем.
    Вчитель обирає навчальний рік, клас і предмет виключно зі списку доступних йому призначень.
    """
    if request.user.is_superadmin_user:
        messages.info(request, "Суперадміністратор може створювати журнали через технічну панель або призначення.")
        return redirect('portal:superadmin_dashboard')

    if not request.user.school:
        raise PermissionDenied("Ви не закріплені за жодною школою.")

    assignments = TeacherAssignment.objects.filter(
        school=request.user.school,
        teacher=request.user,
        is_active=True
    ).select_related('academic_year', 'student_class', 'subject')

    if request.method == 'POST':
        assignment_id = request.POST.get('assignment_id')
        assignment = get_object_or_404(
            TeacherAssignment,
            id=assignment_id,
            teacher=request.user,
            school=request.user.school
        )

        try:
            journal, created = Journal.objects.get_or_create(
                school=request.user.school,
                academic_year=assignment.academic_year,
                student_class=assignment.student_class,
                subject=assignment.subject,
                defaults={
                    'teacher': request.user,
                    'is_archived': False
                }
            )
            if created:
                AuditLog.objects.create(
                    school=request.user.school,
                    user=request.user,
                    action=AuditLog.Action.CREATE,
                    target_model='Journal',
                    target_id=str(journal.id),
                    description=f"Вчитель {request.user.get_full_name_ukr()} створив журнал: {journal}"
                )
                messages.success(request, f"Журнал «{journal}» успішно створено!")
            else:
                messages.info(request, f"Журнал «{journal}» уже був створений раніше.")

            return redirect('portal:journal_detail', journal_id=journal.id)

        except IntegrityError:
            messages.warning(request, "Журнал для цього класу та предмета у вибраному році вже існує в системі.")
            return redirect('portal:teacher_dashboard')

    context = {
        'assignments': assignments,
    }
    return render(request, 'portal/teacher/create_journal.html', context)
