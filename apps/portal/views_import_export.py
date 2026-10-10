import urllib.parse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, Http404
from apps.portal.permissions import has_journal_access, superadmin_required
from apps.curriculum.models import StudentClass, Student
from apps.journals.models import Journal, AuditLog
from apps.importers.services.student_service import StudentImportService
from apps.importers.services.topic_service import TopicImportService
from apps.importers.services.export_service import ExportService
from apps.importers.services.pdf_service import JournalPDFExportService


def _get_journal_for_import_export(user, journal_id, write=False):
    journal = get_object_or_404(
        Journal.objects.select_related('school', 'academic_year', 'student_class', 'subject', 'teacher'),
        id=journal_id
    )
    if not has_journal_access(user, journal, write=write):
        raise PermissionDenied("У вас немає доступу до імпорту/експорту для цього журналу.")
    return journal


def _make_download_response(content, content_type, default_ascii_name, ukr_name):
    """Створює HTTP відповідь із коректними заголовками Content-Disposition за стандартом RFC 5987/6266."""
    encoded_name = urllib.parse.quote(ukr_name)
    response = HttpResponse(content, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{default_ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    return response


# =========================================================================
# Шаблони
# =========================================================================

def download_template(request, template_type, fmt='xlsx'):
    """Завантаження файлів-шаблонів (XLSX або CSV)."""
    fmt = fmt.lower()
    if fmt not in ['xlsx', 'csv']:
        fmt = 'xlsx'

    if template_type == 'students':
        content, content_type = StudentImportService.generate_template(file_format=fmt)
        return _make_download_response(content, content_type, f"template_students.{fmt}", f"shablon_uchni.{fmt}")
    elif template_type == 'topics':
        content, content_type = TopicImportService.generate_template(file_format=fmt)
        return _make_download_response(content, content_type, f"template_topics.{fmt}", f"shablon_tematychnyi_plan.{fmt}")
    else:
        raise Http404("Шаблон не знайдено")


# =========================================================================
# Імпорт та експорт учнів
# =========================================================================

@login_required
def import_students_view(request, journal_id=None):
    """
    Майстер імпорту учнів:
    Крок 1: Завантаження файлу
    Крок 2: Попередній перегляд (Preview) з валідацією, виявленням дублікатів
    Крок 3: Застосування змін у транзакції з детальним звітом
    """
    journal = None
    target_class = None
    school = None

    if journal_id:
        journal = _get_journal_for_import_export(request.user, journal_id, write=True)
        school = journal.school
        target_class = journal.student_class
    else:
        if not request.user.is_superadmin_user:
            raise PermissionDenied()
        school_id = request.GET.get('school_id') or request.POST.get('school_id')
        school = get_object_or_404(request.user.schools if hasattr(request.user, 'schools') else None or school, id=school_id)

    step = 'upload'
    preview_result = None

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'upload_file':
            uploaded_file = request.FILES.get('file')
            if not uploaded_file:
                messages.error(request, "Будь ласка, оберіть файл для завантаження.")
            else:
                try:
                    rows = StudentImportService.parse_file(uploaded_file, uploaded_file.name)
                    if not rows:
                        messages.warning(request, "Файл не містить рядків із даними для імпорту.")
                    else:
                        preview_result = StudentImportService.validate_and_preview(
                            rows, school=school, target_class=target_class
                        )
                        request.session['student_import_preview'] = preview_result['preview_data']
                        request.session['student_import_school_id'] = school.id
                        request.session['student_import_class_id'] = target_class.id if target_class else None
                        step = 'preview'
                except Exception as e:
                    messages.error(request, f"Помилка при читанні файлу: {str(e)}")

        elif action == 'confirm_import':
            preview_data = request.session.get('student_import_preview')
            mode = request.POST.get('import_mode', 'add_and_update')

            if not preview_data:
                messages.error(request, "Дані попереднього перегляду застаріли або відсутні. Спробуйте ще раз.")
                return redirect(request.path)

            try:
                result = StudentImportService.execute_import(
                    preview_data, school=school, mode=mode, user=request.user
                )
                messages.success(
                    request,
                    f"Імпорт завершено! Додано учнів: {result['added']}, "
                    f"Оновлено: {result['updated']}, Пропущено: {result['skipped']}."
                )
                request.session.pop('student_import_preview', None)
                if journal:
                    return redirect('portal:journal_detail', journal_id=journal.id)
                return redirect('portal:superadmin_students')
            except Exception as e:
                messages.error(request, f"Помилка застосування змін. Транзакцію скасовано: {str(e)}")
                step = 'preview'
                preview_result = {'preview_data': preview_data}

    context = {
        'journal': journal,
        'target_class': target_class,
        'school': school,
        'step': step,
        'preview_result': preview_result,
    }
    return render(request, 'portal/importers/student_import.html', context)


@login_required
def export_students_view(request, journal_id=None):
    """Експорт списку учнів (у форматі, придатному для повторного імпорту)."""
    fmt = request.GET.get('format', 'xlsx').lower()
    if journal_id:
        journal = _get_journal_for_import_export(request.user, journal_id, write=False)
        student_class = journal.student_class
        students = Student.objects.filter(
            enrollments__student_class=student_class
        ).distinct().order_by('last_name', 'first_name')
        class_name = student_class.name
    else:
        if not request.user.is_superadmin_user:
            raise PermissionDenied()
        class_id = request.GET.get('class_id')
        student_class = get_object_or_404(StudentClass, id=class_id)
        students = Student.objects.filter(
            enrollments__student_class=student_class
        ).distinct().order_by('last_name', 'first_name')
        class_name = student_class.name

    content, content_type = ExportService.export_students(students, student_class=student_class, file_format=fmt)
    return _make_download_response(
        content, content_type,
        f"students_class_{student_class.id}.{fmt}",
        f"uchni_klas_{class_name}.{fmt}"
    )


# =========================================================================
# Імпорт та експорт тем уроків
# =========================================================================

@login_required
def import_topics_view(request, journal_id):
    """Майстер імпорту календарно-тематичного плану уроків."""
    journal = _get_journal_for_import_export(request.user, journal_id, write=True)
    step = 'upload'
    preview_result = None

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'upload_file':
            uploaded_file = request.FILES.get('file')
            if not uploaded_file:
                messages.error(request, "Оберіть файл плану.")
            else:
                try:
                    rows = TopicImportService.parse_file(uploaded_file, uploaded_file.name)
                    if not rows:
                        messages.warning(request, "Файл порожній.")
                    else:
                        preview_result = TopicImportService.validate_and_preview(rows, journal=journal)
                        request.session['topic_import_preview'] = preview_result['preview_data']
                        step = 'preview'
                except Exception as e:
                    messages.error(request, f"Помилка при читанні файлу: {str(e)}")

        elif action == 'confirm_import':
            preview_data = request.session.get('topic_import_preview')
            mode = request.POST.get('import_mode', 'add_and_update')

            if not preview_data:
                messages.error(request, "Дані попереднього перегляду застаріли.")
                return redirect(request.path)

            try:
                res = TopicImportService.execute_import(preview_data, journal=journal, mode=mode, user=request.user)
                messages.success(
                    request,
                    f"Імпорт тем успішно застосовано! "
                    f"Планові теми: додано {res['added_plan']}, оновлено {res['updated_plan']}. "
                    f"Проведені уроки: створено {res['added_lessons']}, оновлено {res['updated_lessons']}."
                )
                request.session.pop('topic_import_preview', None)
                return redirect('portal:journal_detail', journal_id=journal.id)
            except Exception as e:
                messages.error(request, f"Помилка транзакції: {str(e)}")
                step = 'preview'
                preview_result = {'preview_data': preview_data}

    context = {
        'journal': journal,
        'step': step,
        'preview_result': preview_result,
    }
    return render(request, 'portal/importers/topic_import.html', context)


@login_required
def export_topics_view(request, journal_id):
    """Експорт тематичного плану журналу."""
    journal = _get_journal_for_import_export(request.user, journal_id, write=False)
    fmt = request.GET.get('format', 'xlsx').lower()

    content, content_type = ExportService.export_topics(journal=journal, file_format=fmt)
    return _make_download_response(
        content, content_type,
        f"topics_journal_{journal.id}.{fmt}",
        f"tematychnyi_plan_{journal.student_class.name}_{journal.subject.name}.{fmt}"
    )


# =========================================================================
# Експорт повного журналу (XLSX та PDF)
# =========================================================================

@login_required
def export_journal_xlsx_view(request, journal_id):
    """Експорт журналу в табличний файл XLSX."""
    journal = _get_journal_for_import_export(request.user, journal_id, write=False)
    semester_id = request.GET.get('semester_id')
    semester = journal.academic_year.semesters.filter(id=semester_id).first() if semester_id else None

    content, content_type = ExportService.export_journal_xlsx(journal, semester=semester)
    return _make_download_response(
        content,
        content_type,
        f"journal_{journal.id}.xlsx",
        f"zhurnal_{journal.student_class.name}_{journal.subject.name}.xlsx"
    )


@login_required
def export_journal_pdf_view(request, journal_id):
    """Експорт журналу в друкований PDF документ."""
    journal = _get_journal_for_import_export(request.user, journal_id, write=False)
    semester_id = request.GET.get('semester_id')
    semester = journal.academic_year.semesters.filter(id=semester_id).first() if semester_id else None

    pdf_content = JournalPDFExportService.generate_journal_pdf(journal, semester=semester)
    return _make_download_response(
        pdf_content,
        'application/pdf',
        f"journal_{journal.id}.pdf",
        f"zhurnal_{journal.student_class.name}_{journal.subject.name}.pdf"
    )
