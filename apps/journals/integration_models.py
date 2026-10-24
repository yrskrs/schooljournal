"""Journal-scoped external APIs. Imported lazily at the end of models.py."""
from django.conf import settings
from django.db import models
from .secret_fields import EncryptedTokenField


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
    api_key = EncryptedTokenField(blank=True, verbose_name='Ключ зовнішнього API')
    service_connection = models.ForeignKey('journals.ServiceConnection', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='journal_bindings')
    default_period_days = models.PositiveSmallIntegerField(default=30)
    default_lesson = models.ForeignKey('journals.Lesson', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
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
    server_group = models.CharField(max_length=100, blank=True, default='', verbose_name='Група серверів / Об’єднання IP',
        help_text='Вкажіть однакову назву для підключень до одного сервера з різних IP (дім / школа).')
    location_tag = models.CharField(max_length=50, blank=True, default='', verbose_name='Локація / Мережа',
        help_text='Наприклад: 🏠 Дім, 🏫 Кабінет інформатики, 💻 Ноутбук')
    is_default_for_group = models.BooleanField(default=False, verbose_name='Основна адреса в групі')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def host_display(self):
        """Повертає IP або домен та порт для швидкого розпізнавання адреси."""
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(self.url)
            return parsed.netloc or self.url
        except Exception:
            return self.url

    @property
    def group_display(self):
        return self.server_group or self.name or self.get_source_display()

    @property
    def location_display(self):
        return self.location_tag or ('Основний' if self.is_default_for_group else '')

    @property
    def masked_key(self):
        key = (self.api_key or '').strip()
        return '••••••' if key else '—'

    @property
    def capabilities_description(self):
        caps = self.roster_cache.get('capabilities', []) if isinstance(self.roster_cache, dict) else []
        if 'journal_roster_import' in caps:
            return 'може отримувати та передавати дані'
        return 'може отримувати дані'

    @property
    def key_display_name(self):
        source_title = self.name or self.get_source_display()
        return f"{source_title} (Ключ: {self.masked_key}) — {self.capabilities_description}"


class GradeLessonAssignment(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='lesson_assignments')
    work_key = models.CharField(max_length=64)
    lesson = models.ForeignKey('journals.Lesson', on_delete=models.CASCADE, related_name='api_assignments')
    selected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'work_key'], name='unique_api_work_lesson')]


class GradeStudentMapping(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='student_mappings')
    external_id = models.CharField(max_length=100)
    student = models.ForeignKey('curriculum.Student', on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_id'], name='grade_mapping_external_unique'),
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
    is_separate = models.BooleanField(default=False)
    alias_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='aliases')
    matched_student = models.ForeignKey('curriculum.Student', on_delete=models.PROTECT, null=True, blank=True)
    separate_student = models.OneToOneField('curriculum.Student', on_delete=models.PROTECT,
        null=True, blank=True, related_name='api_extra_identity')
    separate_journal = models.ForeignKey('journals.Journal', on_delete=models.PROTECT,
        null=True, blank=True, related_name='separate_api_students')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_key'], name='extra_student_identity_unique')]
        ordering = ['name', 'pk']


class DeletedAPIIdentity(models.Model):
    """Minimal local tombstone: no name or grade payload is retained here."""
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='deleted_identities')
    external_key = models.CharField(max_length=200)
    deleted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_key'], name='deleted_api_identity_unique')]


class APIInboxResult(models.Model):
    connection = models.ForeignKey(GradeAPIConnection, on_delete=models.CASCADE, related_name='inbox_results')
    external_id = models.CharField(max_length=200)
    extra_student = models.ForeignKey(ExtraStudent, on_delete=models.PROTECT, null=True, blank=True, related_name='results')
    payload = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    resolved = models.BooleanField(default=False)
    is_ignored = models.BooleanField(default=False, verbose_name="Ігноровано")
    ignored_at = models.DateTimeField(null=True, blank=True, verbose_name="Час ігнорування")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'external_id'], name='api_inbox_result_unique')]
        ordering = ['-updated_at', '-pk']
