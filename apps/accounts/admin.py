from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, TeacherProfile

class TeacherProfileInline(admin.StackedInline):
    model = TeacherProfile
    can_delete = False
    verbose_name_plural = 'Профіль педагога'

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Шкільна інформація та роль', {
            'fields': ('role', 'school', 'patronymic', 'phone'),
        }),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Шкільна інформація та роль', {
            'fields': ('role', 'school', 'patronymic', 'phone'),
        }),
    )
    list_display = ('username', 'last_name', 'first_name', 'patronymic', 'role', 'school', 'is_active')
    list_filter = ('role', 'school', 'is_active', 'is_staff')
    search_fields = ('username', 'last_name', 'first_name', 'patronymic', 'email', 'phone')
    inlines = [TeacherProfileInline]

@admin.register(TeacherProfile)
class TeacherProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'qualification', 'pedagogical_title')
    search_fields = ('user__last_name', 'user__first_name', 'qualification')
