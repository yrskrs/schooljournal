from django.urls import path
from . import views_integrations
from . import views_connection_setup
from . import views_api_inbox
from school_sync import django_backend as roster_views
from . import views_public
from django.shortcuts import redirect
from . import views_superadmin, views_teacher, views_journal, views_import_export, views_nz, views_schedule

app_name = 'portal'

def root_redirect(request):
    if not request.user.is_authenticated:
        return redirect('login')
    if request.user.is_school_admin_user:
        return redirect('portal:superadmin_dashboard')
    return redirect('portal:teacher_dashboard')

urlpatterns = [
    path('help/', views_public.guide, name='guide'),
    path('versions/', views_public.versions, name='versions'),
    path('healthz/', views_public.health, name='health'),
    path('api/v2/roster-sync/', roster_views.roster_api, name='roster_sync_api'),
    path('roster-sync/', roster_views.roster_ui, name='roster_sync'),
    path('api/v1/journals/<int:journal_id>/students/', views_integrations.student_list_api, name='student_list_api'),
    path('journal/<int:journal_id>/integrations/', views_integrations.journal_integrations, name='journal_integrations'),
    path('journal/<int:journal_id>/connect/', views_connection_setup.connect_source, name='connect_source'),
    path('journal/<int:journal_id>/connection/<int:connection_id>/delete/', views_connection_setup.delete_source_connection, name='delete_source_connection'),
    path('journal/<int:journal_id>/integrations/<int:connection_id>/pull/', views_integrations.pull_external_grades, name='pull_external_grades'),
    path('journal/<int:journal_id>/sync/<str:source>/', views_integrations.pull_source_grades, name='pull_source_grades'),
    path('journal/<int:journal_id>/external-grades/', views_integrations.external_grade_review, name='external_grade_review'),
    path('journal/<int:journal_id>/api-inbox/', views_api_inbox.api_inbox, name='api_inbox'),
    path('', root_redirect, name='dashboard'),

    # Кабінет суперадміністратора
    path('admin-portal/', views_superadmin.superadmin_dashboard, name='superadmin_dashboard'),
    path('admin-portal/schools/', views_superadmin.schools_list, name='superadmin_schools'),
    path('admin-portal/teachers/', views_superadmin.teachers_list, name='superadmin_teachers'),
    path('admin-portal/curriculum/', views_superadmin.curriculum_management, name='superadmin_curriculum'),
    path('admin-portal/assignments/', views_superadmin.assignments_management, name='superadmin_assignments'),
    path('admin-portal/students/', views_superadmin.students_management, name='superadmin_students'),
    path('admin-portal/audit/', views_superadmin.audit_log_view, name='superadmin_audit'),

    # Кабінет вчителя
    path('teacher-portal/', views_teacher.teacher_dashboard, name='teacher_dashboard'),
    path('teacher-portal/update-class-order/', views_teacher.update_class_order, name='update_class_order'),
    path('teacher-portal/update-journal-order/', views_teacher.update_journal_order, name='update_journal_order'),
    path('teacher-portal/schedule/', views_schedule.weekly_schedule, name='weekly_schedule'),
    path('teacher-portal/lesson-times/', views_schedule.lesson_times, name='lesson_times'),
    path('teacher-portal/api-keys/', views_connection_setup.teacher_api_keys, name='teacher_api_keys'),
    path('teacher-portal/api-keys/delete/', views_connection_setup.delete_api_key_group, name='delete_api_key_group'),
    path('teacher-portal/journal/create/', views_teacher.create_journal, name='create_journal'),

    # Інтерактивний журнал
    path('journal/<int:journal_id>/', views_journal.journal_detail, name='journal_detail'),
    path('journal/<int:journal_id>/grade/update/', views_journal.update_grade, name='update_grade'),
    path('journal/<int:journal_id>/student/<int:student_id>/toggle-ignore/', views_journal.toggle_student_sync_ignore, name='toggle_student_sync_ignore'),
    path('journal/<int:journal_id>/attendance/update/', views_journal.update_attendance, name='update_attendance'),
    path('journal/<int:journal_id>/summary-grade/update/', views_journal.update_summary_grade, name='update_summary_grade'),
    path('journal/<int:journal_id>/lesson/create/', views_journal.lesson_create, name='lesson_create'),
    path('journal/<int:journal_id>/lesson/swap/', views_journal.lesson_swap, name='lesson_swap'),
    path('journal/<int:journal_id>/plan/<int:topic_id>/move/', views_journal.plan_topic_move, name='plan_topic_move'),
    path('journal/<int:journal_id>/topics/copy/', views_journal.topics_copy, name='topics_copy'),
    path('journal/<int:journal_id>/assessment/', views_journal.assessment_calculate, name='assessment_calculate'),
    path('journal/<int:journal_id>/lesson/<int:lesson_id>/edit/', views_journal.lesson_edit, name='lesson_edit'),
    path('journal/<int:journal_id>/lesson/<int:lesson_id>/delete/', views_journal.lesson_delete, name='lesson_delete'),

    # Інтеграція з nz.ua (Нові знання)
    path('nz/status/', views_nz.nz_account_status_view, name='nz_status'),
    path('nz/connect/', views_nz.nz_account_connect_view, name='nz_connect'),
    path('nz/disconnect/', views_nz.nz_account_disconnect_view, name='nz_disconnect'),
    path('nz/journals/', views_nz.nz_journals_list_view, name='nz_journals_list'),
    path('nz/import/', views_nz.nz_journal_import_view, name='nz_journal_import'),
    path('journal/<int:journal_id>/nz/import/', views_nz.nz_journal_import_view, name='nz_journal_import_for_journal'),
    path('journal/<int:journal_id>/nz/preview/', views_nz.nz_sync_preview_view, name='nz_sync_preview'),
    path('journal/<int:journal_id>/nz/sync/', views_nz.nz_sync_execute_view, name='nz_sync_execute'),
    path('journal/<int:journal_id>/nz/ignore-change/', views_nz.nz_ignore_change_view, name='nz_ignore_change'),
    path('journal/<int:journal_id>/nz/history/', views_nz.nz_sync_history_view, name='nz_sync_history'),

    # Імпорт та експорт
    path('journal/<int:journal_id>/export/xlsx/', views_import_export.export_journal_xlsx_view, name='export_journal_xlsx'),
    path('journal/<int:journal_id>/export/pdf/', views_import_export.export_journal_pdf_view, name='export_journal_pdf'),
    path('journal/<int:journal_id>/students/import/', views_import_export.import_students_view, name='import_students'),
    path('journal/<int:journal_id>/students/export/', views_import_export.export_students_view, name='export_students'),
    path('journal/<int:journal_id>/topics/import/', views_import_export.import_topics_view, name='import_topics'),
    path('journal/<int:journal_id>/topics/export/', views_import_export.export_topics_view, name='export_topics'),
    path('template/<str:template_type>/<str:fmt>/', views_import_export.download_template, name='download_template'),
]
