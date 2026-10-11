"""Journal-scoped external APIs. Imported lazily at the end of models.py."""
from django.conf import settings
from django.db import models


class RosterReplica(models.Model):
    ref = models.CharField(max_length=100, primary_key=True)
    data = models.JSONField(default=dict)


class RosterState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    data = models.JSONField(default=dict)


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
    source = models.CharField(max_length=16, default='generic', choices=[
        ('generic', 'Інший API'), ('schooltest', 'Сайт тестування'), ('schoolwork', 'Сайт завдань')])
    contract_version = models.PositiveSmallIntegerField(default=1, choices=[(1, 'Чинний API v1'), (2, 'Спільний контракт v2')])
    source_class_id = models.CharField(max_length=100, blank=True, verbose_name='ID класу на сайті джерела')
    source_subject_id = models.CharField(max_length=100, blank=True, verbose_name='ID предмета на сайті джерела')
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
    roster_cache = models.JSONField(default=dict, db_default={}, blank=True)
    roster_checked_at = models.DateTimeField(null=True, blank=True)
    roster_sent_at = models.DateTimeField(null=True, blank=True)
    roster_report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class GradeStudentMapping(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='student_mappings')
    external_id = models.CharField(max_length=100)
    student = models.ForeignKey('curriculum.Student', on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_id'], name='grade_mapping_external_unique'),
            models.UniqueConstraint(fields=['connection', 'student'], name='grade_mapping_student_unique'),
        ]


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
    source_payload = models.JSONField(default=dict, blank=True)
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


class ExtraStudent(models.Model):
    """A source identity outside the confirmed class; never an enrollment."""
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='extra_students')
    external_key = models.CharField(max_length=200)
    name = models.CharField(max_length=200)
    matched_student = models.ForeignKey('curriculum.Student', on_delete=models.PROTECT, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_key'], name='extra_student_identity_unique')]
        ordering = ['name', 'pk']


class APIInboxResult(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='inbox_results')
    external_id = models.CharField(max_length=200)
    extra_student = models.ForeignKey(ExtraStudent, on_delete=models.PROTECT, null=True, blank=True, related_name='results')
    payload = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    resolved = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_id'], name='api_inbox_result_unique')]
        ordering = ['-updated_at', '-pk']
