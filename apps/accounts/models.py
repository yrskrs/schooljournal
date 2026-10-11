from django.db import models
from django.contrib.auth.models import AbstractUser
from apps.schools.models import School


class InitialSetupState(models.Model):
    """Persist completion even if the first administrator is later deleted."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPERADMIN = 'SUPERADMIN', 'Суперадміністратор'
        TEACHER = 'TEACHER', 'Вчитель'
        SCHOOL_ADMIN = 'SCHOOL_ADMIN', 'Адміністратор школи'

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.TEACHER,
        verbose_name="Роль"
    )
    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='users',
        verbose_name="Школа",
        help_text="Для вчителів вибір школи є обов'язковим для ізоляції даних."
    )
    patronymic = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="По батькові"
    )
    phone = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Телефон"
    )
    is_nz_personal = models.BooleanField(default=False, verbose_name='Особистий профіль nz.ua')

    class Meta:
        verbose_name = "Користувач"
        verbose_name_plural = "Користувачі"
        ordering = ['last_name', 'first_name', 'username']

    @property
    def is_superadmin_user(self):
        return self.role == self.Role.SUPERADMIN or self.is_superuser

    @property
    def is_school_admin_user(self):
        return self.is_superadmin_user or (self.role == self.Role.SCHOOL_ADMIN and self.school_id is not None)

    @property
    def is_teacher_user(self):
        return self.role == self.Role.TEACHER

    def get_full_name_ukr(self):
        """ПІБ українською: Прізвище Ім'я По-батькові"""
        parts = [self.last_name, self.first_name, self.patronymic]
        full = " ".join([p for p in parts if p]).strip()
        return full if full else self.username

    def __str__(self):
        full = self.get_full_name_ukr()
        role_label = self.get_role_display()
        return f"{full} ({role_label})"


class TeacherProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='teacher_profile',
        verbose_name="Користувач"
    )
    qualification = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Кваліфікаційна категорія",
        help_text="Наприклад: Спеціаліст вищої категорії, перша категорія тощо."
    )
    pedagogical_title = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Педагогічне звання",
        help_text="Наприклад: Вчитель-методист, Старший вчитель."
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Примітки"
    )
    class_order = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Порядок класів на дашборді"
    )
    journal_order = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Порядок журналів на дашборді"
    )

    class Meta:
        verbose_name = "Профіль вчителя"
        verbose_name_plural = "Профілі вчителів"

    def __str__(self):
        return f"Профіль: {self.user.get_full_name_ukr()}"
