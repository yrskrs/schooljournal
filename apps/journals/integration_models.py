"""Journal-scoped external APIs. Imported lazily at the end of models.py."""
from django.conf import settings
from django.db import models


def default_student_fields():
    return ['id', 'name']


class StudentAPIGrant(models.Model):
    journal = models.OneToOneField('journals.Journal', on_delete=models.CASCADE, related_name='student_api')
    enabled = models.BooleanField(default=False, verbose_name='Дозволити API списку учнів')
    fields = models.JSONField(default=default_student_fields, verbose_name='Поля відповіді')
    token_hash = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='Ключ діє до')
    updated_at = models.DateTimeField(auto_now=True)


class GradeAPIConnection(models.Model):
    journal = models.ForeignKey('journals.Journal', on_delete=models.CASCADE, related_name='grade_apis')
    name = models.CharField(max_length=100, verbose_name='Назва сервера робіт')
    url = models.URLField(max_length=500, verbose_name='Адреса API оцінок')
    enabled = models.BooleanField(default=True, verbose_name='Підключення активне')
    api_key = models.TextField(blank=True, verbose_name='Ключ зовнішнього API')
    student_identifier = models.CharField(max_length=15, default='id', choices=[
        ('id', 'Локальний ID учня'), ('nz_student_id', 'ID учня nz.ua'), ('internal_id', 'Внутрішній ID учня')],
        verbose_name='Ідентифікатор учня на сервері робіт')
    records_key = models.CharField(max_length=80, default='grades', blank=True, verbose_name='Список оцінок у JSON')
    id_key = models.CharField(max_length=80, default='id', verbose_name='ID роботи / оцінки')
    student_key = models.CharField(max_length=80, default='student_id', verbose_name='ID учня')
    date_key = models.CharField(max_length=80, default='date', verbose_name='Дата уроку')
    value_key = models.CharField(max_length=80, default='value', verbose_name='Оцінка')
    ai_key = models.CharField(max_length=80, default='is_ai', verbose_name='Оцінка виставлена ШІ')
    approved_key = models.CharField(max_length=80, default='approved_by_teacher', verbose_name='Схвалено вчителем')
    lesson_key = models.CharField(max_length=80, default='lesson_number', verbose_name='Номер уроку на дату')
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ExternalGrade(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Очікує схвалення'
        APPLIED = 'APPLIED', 'Додано до журналу'
        CONFLICT = 'CONFLICT', 'У клітинці є інша оцінка / відвідування'
        REJECTED = 'REJECTED', 'Відхилено вчителем'

    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.RESTRICT, related_name='records')
    external_id = models.CharField(max_length=200)
    student = models.ForeignKey('curriculum.Student', on_delete=models.CASCADE)
    lesson = models.ForeignKey('journals.Lesson', on_delete=models.CASCADE)
    value = models.CharField(max_length=10)
    is_ai = models.BooleanField(default=False)
    externally_approved = models.BooleanField(default=False)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING)
    fingerprint = models.CharField(max_length=64)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_id'], name='unique_external_grade_id')]
        ordering = ['-lesson__date', '-pk']


class GradePullLog(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='pull_logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    counts = models.JSONField(default=dict)
    errors = models.JSONField(default=list)
