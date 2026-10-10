from django.contrib import admin
from .models import Journal, LessonPlanTopic, Lesson, Grade, SummaryGrade, Attendance, AuditLog

@admin.register(Journal)
class JournalAdmin(admin.ModelAdmin):
    list_display = ('student_class', 'subject', 'teacher', 'academic_year', 'school', 'is_archived')
    list_filter = ('school', 'academic_year', 'is_archived')
    search_fields = ('student_class__name', 'subject__name', 'teacher__last_name')

@admin.register(LessonPlanTopic)
class LessonPlanTopicAdmin(admin.ModelAdmin):
    list_display = ('topic_number', 'title', 'journal', 'planned_date', 'lesson_type', 'is_conducted')
    list_filter = ('journal', 'lesson_type', 'is_conducted')
    search_fields = ('title', 'homework')

@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ('date', 'lesson_number', 'topic', 'journal', 'lesson_type', 'created_by')
    list_filter = ('journal', 'lesson_type', 'date')
    search_fields = ('topic', 'homework', 'notes')

@admin.register(Grade)
class GradeAdmin(admin.ModelAdmin):
    list_display = ('student', 'lesson', 'value', 'is_approved', 'numeric_value', 'updated_at')
    list_filter = ('lesson__journal', 'is_approved', 'value')
    search_fields = ('student__last_name', 'student__first_name', 'value')

@admin.register(SummaryGrade)
class SummaryGradeAdmin(admin.ModelAdmin):
    list_display = ('student', 'journal', 'grade_type', 'thematic_name', 'value')
    list_filter = ('journal', 'grade_type')
    search_fields = ('student__last_name', 'value')

@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('student', 'lesson', 'status', 'reason', 'updated_at')
    list_filter = ('lesson__journal', 'status')
    search_fields = ('student__last_name', 'reason')

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'school', 'action', 'target_model', 'description')
    list_filter = ('school', 'action', 'target_model')
    search_fields = ('description', 'target_id', 'user__username')
    readonly_fields = ('timestamp', 'user', 'school', 'action', 'target_model', 'target_id', 'description', 'changes', 'ip_address')
