from django.contrib import admin
from .models import AcademicYear, Semester, Subject, StudentClass, Student, ClassEnrollment, TeacherAssignment, LessonTime, WeeklyScheduleEntry

@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'start_date', 'end_date', 'is_current', 'is_archived')
    list_filter = ('school', 'is_current', 'is_archived')

@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ('name', 'academic_year', 'number', 'start_date', 'end_date')
    list_filter = ('academic_year__school', 'academic_year')

@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'code', 'is_active')
    list_filter = ('school', 'is_active')
    search_fields = ('name', 'code')

@admin.register(StudentClass)
class StudentClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'school', 'academic_year', 'grade_level', 'class_teacher')
    list_filter = ('school', 'academic_year', 'grade_level')
    search_fields = ('name',)

class ClassEnrollmentInline(admin.TabularInline):
    model = ClassEnrollment
    extra = 1

@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('get_full_name_ukr', 'internal_id', 'school', 'status', 'birth_date')
    list_filter = ('school', 'status')
    search_fields = ('last_name', 'first_name', 'middle_name', 'internal_id')
    inlines = [ClassEnrollmentInline]

@admin.register(ClassEnrollment)
class ClassEnrollmentAdmin(admin.ModelAdmin):
    list_display = ('student', 'student_class', 'start_date', 'end_date', 'is_active')
    list_filter = ('student_class__school', 'student_class__academic_year', 'is_active')
    search_fields = ('student__last_name', 'student__first_name', 'student__internal_id')

@admin.register(TeacherAssignment)
class TeacherAssignmentAdmin(admin.ModelAdmin):
    list_display = ('teacher', 'subject', 'student_class', 'academic_year', 'school', 'is_active')
    list_filter = ('school', 'academic_year', 'is_active')
    search_fields = ('teacher__last_name', 'subject__name', 'student_class__name')


@admin.register(LessonTime)
class LessonTimeAdmin(admin.ModelAdmin):
    list_display = ('school', 'number', 'start_time', 'end_time', 'is_active')
    list_filter = ('school', 'is_active')


@admin.register(WeeklyScheduleEntry)
class WeeklyScheduleEntryAdmin(admin.ModelAdmin):
    list_display = ('journal', 'weekday', 'lesson_time', 'start_date', 'end_date', 'room')
    list_filter = ('journal__school', 'journal__teacher', 'weekday')
