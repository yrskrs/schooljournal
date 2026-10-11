from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from apps.curriculum.models import Student
from apps.journals.models import Journal, ExtraStudent
from apps.journals.services.assessment import journal_students
from apps.journals.services.extra_students import delete_separate_student, extra_parent
from apps.journals.services.external_grades import ExternalAPIError
from apps.portal.views_journal import _get_journal_or_403


@login_required
@require_POST
def delete_extracurricular_student(request, journal_id, student_id):
    with transaction.atomic():
        _get_journal_or_403(request.user, journal_id, write=True)
        journal = Journal.objects.get(pk=journal_id)
        parent = extra_parent(journal)
        # Import locks the source journal before its separate table.
        if parent:
            Journal.objects.select_for_update().get(pk=parent.pk)
        Journal.objects.select_for_update().get(pk=journal.pk)
        _get_journal_or_403(request.user, journal_id, write=True)
        journal = Journal.objects.get(pk=journal_id)
        list(ExtraStudent.objects.select_for_update().filter(separate_journal=journal, separate_student_id=student_id))
        student = get_object_or_404(Student.objects.select_for_update(),
            pk=student_id, school=journal.school, pk__in=journal_students(journal).values('pk'))
        try:
            if request.POST.get('confirm_delete') != 'on':
                raise ExternalAPIError('Підтвердьте видалення позакласного учня та його оцінок.')
            from apps.portal.views_api_inbox import _same_revision
            if not _same_revision(student.updated_at, request.POST.get('version')):
                raise ExternalAPIError('Профіль змінився після перегляду. Оновіть сторінку.')
            name = student.get_full_name_ukr()
            delete_separate_student(journal, student, request.user)
            messages.success(request, f'Позакласного учня «{name}» та його локальні записи видалено. Дію записано в історії.')
        except ExternalAPIError as error:
            messages.error(request, str(error))
    return redirect('portal:journal_detail', journal_id=journal_id)
