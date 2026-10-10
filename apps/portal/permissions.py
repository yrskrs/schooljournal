from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from apps.journals.models import Journal


def superadmin_required(view_func):
    """Декоратор для обмеження доступу виключно суперадміністраторам."""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.shortcuts import redirect
            return redirect('login')
        if not request.user.is_superadmin_user:
            raise PermissionDenied("Доступ дозволено лише суперадміністратору системи.")
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def school_admin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.shortcuts import redirect
            return redirect('login')
        if not request.user.is_school_admin_user:
            raise PermissionDenied('Доступ дозволено адміністратору школи.')
        return view_func(request, *args, **kwargs)
    return wrapped


def admin_schools(user):
    from apps.schools.models import School
    return School.objects.all() if user.is_superadmin_user else School.objects.filter(pk=user.school_id)


def get_user_accessible_schools(user):
    """Повертає школи, до яких користувач має доступ (суперадмін — всі, вчитель — лише своя)."""
    from apps.schools.models import School
    if user.is_superadmin_user:
        return School.objects.all()
    from django.db.models import Q
    return School.objects.filter(Q(pk=user.school_id) | Q(journals__nz_owner__user=user)).distinct()


def has_journal_access(user, journal, write=False):
    """
    Перевіряє доступ користувача до конкретного журналу:
    1. Суперадміністратор має повний доступ (перегляд і редагування всіх журналів).
    2. Вчитель іншої школи взагалі НЕ МАЄ доступу (ізоляція даних між школами).
    3. Вчитель своєї школи має право перегляду, якщо це його журнал або журнал його школи.
    4. Для редагування:
       - Якщо журнал архівний — вчитель НЕ МОЖЕ редагувати (потрібен дозвіл суперадміна).
       - Вчитель може редагувати, якщо journal.teacher == user АБО увімкнено journal.allows_override_teacher.
    """
    if not user.is_authenticated:
        return False

    if user.is_superadmin_user:
        return True

    if journal.nz_owner_id:
        owns = journal.nz_owner.user_id == user.pk
        linked = journal.local_profile_id == user.pk
        return (owns or linked) and not (write and journal.is_archived)

    # Ізоляція шкіл
    if not user.school or journal.school_id != user.school_id:
        return False

    if write:
        # Архівний журнал заблоковано для редагування вчителями
        if journal.is_archived:
            return False
        # Перевірка авторства або заміни
        return (journal.teacher_id == user.id) or journal.allows_override_teacher

    return True


def get_user_journals(user):
    """Повертає список доступних журналів для користувача."""
    if user.is_superadmin_user:
        return Journal.objects.all().select_related('school', 'academic_year', 'student_class', 'subject', 'teacher')
    from django.db.models import Q
    return Journal.objects.filter(
        Q(nz_owner__user=user) | Q(local_profile=user) |
        Q(nz_owner__isnull=True, school_id=user.school_id, teacher=user) |
        Q(nz_owner__isnull=True, school_id=user.school_id, allows_override_teacher=True)
    ).select_related('school', 'academic_year', 'student_class', 'subject', 'teacher').distinct()


def models_teacher_filter(user):
    from django.db.models import Q
    return Q(teacher=user) | Q(allows_override_teacher=True)
