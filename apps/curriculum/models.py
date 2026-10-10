from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models import Q
from django.utils import timezone
from apps.schools.models import School
from apps.accounts.models import User

class AcademicYear(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='academic_years',
        verbose_name="Школа"
    )
    name = models.CharField(max_length=50, verbose_name="Назва (напр. 2024/2025)")
    start_date = models.DateField(verbose_name="Дата початку")
    end_date = models.DateField(verbose_name="Дата завершення")
    is_current = models.BooleanField(default=False, verbose_name="Поточний навчальний рік")
    is_archived = models.BooleanField(default=False, verbose_name="Архівний")

    class Meta:
        verbose_name = "Навчальний рік"
        verbose_name_plural = "Навчальні роки"
        unique_together = ('school', 'name')
        ordering = ['-start_date']

    def save(self, *args, **kwargs):
        if self.is_current:
            # Тільки один рік може бути поточним для даної школи
            AcademicYear.objects.filter(school=self.school, is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)

    def __str__(self):
        status = " (Поточний)" if self.is_current else (" (Архів)" if self.is_archived else "")
        return f"{self.name}{status}"


class Semester(models.Model):
    academic_year = models.ForeignKey(
        AcademicYear,
        on_delete=models.CASCADE,
        related_name='semesters',
        verbose_name="Навчальний рік"
    )
    number = models.PositiveSmallIntegerField(
        choices=[(1, "1-й семестр"), (2, "2-й семестр")],
        verbose_name="Номер семестру"
    )
    name = models.CharField(max_length=50, verbose_name="Назва (напр. I семестр)")
    start_date = models.DateField(verbose_name="Початок семестру")
    end_date = models.DateField(verbose_name="Кінець семестру")

    class Meta:
        verbose_name = "Семестр"
        verbose_name_plural = "Семестри"
        unique_together = ('academic_year', 'number')
        ordering = ['academic_year', 'number']

    def __str__(self):
        return f"{self.name} ({self.academic_year.name})"


class Subject(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='subjects',
        verbose_name="Школа"
    )
    name = models.CharField(max_length=150, verbose_name="Назва предмета")
    code = models.CharField(max_length=50, blank=True, verbose_name="Код / Скорочення")
    is_active = models.BooleanField(default=True, verbose_name="Активний")

    class Meta:
        verbose_name = "Предмет"
        verbose_name_plural = "Предмети"
        unique_together = ('school', 'name')
        ordering = ['name']

    def __str__(self):
        return self.name


class StudentClass(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='classes',
        verbose_name="Школа"
    )
    academic_year = models.ForeignKey(
        AcademicYear,
        on_delete=models.CASCADE,
        related_name='classes',
        verbose_name="Навчальний рік"
    )
    grade_level = models.PositiveSmallIntegerField(verbose_name="Рівень (клас)", default=5)
    letter = models.CharField(max_length=10, blank=True, verbose_name="Літера", default="А")
    name = models.CharField(max_length=50, verbose_name="Назва класу (напр. 5-А)")
    class_teacher = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='homeroom_classes',
        verbose_name="Класний керівник"
    )

    class Meta:
        verbose_name = "Клас"
        verbose_name_plural = "Класи"
        unique_together = ('school', 'academic_year', 'name')
        ordering = ['grade_level', 'name']

    def __str__(self):
        return f"{self.name} ({self.academic_year.name})"


class Student(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Навчається'
        TRANSFERRED = 'TRANSFERRED', 'Переведено'
        EXPELLED = 'EXPELLED', 'Відраховано'
        GRADUATED = 'GRADUATED', 'Випустився'
        ARCHIVED = 'ARCHIVED', 'Архівний'

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='students',
        verbose_name="Школа"
    )
    internal_id = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Внутрішній ID / Особова справа",
        help_text="Унікальний код учня в межах школи для імпорту/експорту"
    )
    nz_student_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="ID учня в nz.ua",
        help_text="Ідентифікатор учня на порталі nz.ua"
    )
    last_name = models.CharField(max_length=100, verbose_name="Прізвище")
    first_name = models.CharField(max_length=100, verbose_name="Ім'я")
    middle_name = models.CharField(max_length=100, blank=True, verbose_name="По батькові")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Дата народження")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        verbose_name="Статус навчання"
    )
    notes = models.TextField(blank=True, verbose_name="Примітки")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Учень"
        verbose_name_plural = "Учні"
        ordering = ['last_name', 'first_name', 'middle_name']

    def get_full_name_ukr(self):
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join([p for p in parts if p]).strip()

    def get_short_name_ukr(self):
        fn = f"{self.first_name[0]}." if self.first_name else ""
        mn = f"{self.middle_name[0]}." if self.middle_name else ""
        return f"{self.last_name} {fn}{mn}".strip()

    def __str__(self):
        return f"{self.get_full_name_ukr()} [{self.internal_id or 'без ID'}]"


class ClassEnrollment(models.Model):
    """
    Зберігає історію зарахування та перебування учня в конкретному класі.
    Забезпечує збереження історичного журналу при переведенні чи вибутті.
    """
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='enrollments',
        verbose_name="Учень"
    )
    student_class = models.ForeignKey(
        StudentClass,
        on_delete=models.CASCADE,
        related_name='enrollments',
        verbose_name="Клас"
    )
    start_date = models.DateField(verbose_name="Дата зарахування", default=timezone.now)
    end_date = models.DateField(null=True, blank=True, verbose_name="Дата вибуття / переведення")
    is_active = models.BooleanField(default=True, verbose_name="Активне зарахування")
    transfer_note = models.CharField(max_length=255, blank=True, verbose_name="Причина / наказ")

    class Meta:
        verbose_name = "Зарахування учня до класу"
        verbose_name_plural = "Зарахування учнів до класів"
        ordering = ['student_class', 'student__last_name']

    def __str__(self):
        status = " (Активний)" if self.is_active else f" (до {self.end_date})"
        return f"{self.student.get_full_name_ukr()} -> {self.student_class.name}{status}"


class TeacherAssignment(models.Model):
    """
    Призначення вчителя до предмета та класу у певному навчальному році.
    """
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='teacher_assignments',
        verbose_name="Школа"
    )
    teacher = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='assignments',
        verbose_name="Вчитель"
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='teacher_assignments',
        verbose_name="Предмет"
    )
    student_class = models.ForeignKey(
        StudentClass,
        on_delete=models.CASCADE,
        related_name='teacher_assignments',
        verbose_name="Клас"
    )
    academic_year = models.ForeignKey(
        AcademicYear,
        on_delete=models.CASCADE,
        related_name='teacher_assignments',
        verbose_name="Навчальний рік"
    )
    is_active = models.BooleanField(default=True, verbose_name="Активне призначення")

    class Meta:
        verbose_name = "Призначення вчителя"
        verbose_name_plural = "Призначення вчителів"
        unique_together = ('teacher', 'subject', 'student_class', 'academic_year')

    def __str__(self):
        return f"{self.teacher.get_full_name_ukr()}: {self.subject.name} ({self.student_class.name})"


class LessonTime(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name='lesson_times', verbose_name='Школа')
    number = models.PositiveSmallIntegerField(verbose_name='Номер уроку', validators=[MinValueValidator(1), MaxValueValidator(30)])
    start_time = models.TimeField(verbose_name='Початок уроку')
    end_time = models.TimeField(verbose_name='Завершення уроку')
    is_active = models.BooleanField(default=True, verbose_name='Доступний для нових уроків')

    class Meta:
        ordering = ['number', 'pk']
        verbose_name = 'Час уроку'
        verbose_name_plural = 'Час уроків'
        constraints = [
            models.UniqueConstraint(fields=['school', 'number'], name='unique_school_lesson_time_number'),
            models.UniqueConstraint(fields=['school', 'start_time'], name='unique_school_lesson_start_time'),
            models.CheckConstraint(condition=Q(end_time__gt=models.F('start_time')), name='lesson_time_end_after_start'),
        ]

    def __str__(self):
        return f'{self.number} урок ({self.start_time:%H:%M} — {self.end_time:%H:%M})'

    def clean(self):
        super().clean()
        if self.start_time and self.end_time:
            for field in ('start_time', 'end_time'):
                value = getattr(self, field)
                if value.second or value.microsecond:
                    raise ValidationError({field: 'Вкажіть час із точністю до хвилини.'})
            if self.end_time <= self.start_time:
                raise ValidationError({'end_time': 'Завершення має бути пізніше початку уроку.'})
            if self.school_id and LessonTime.objects.filter(school_id=self.school_id,
                    start_time__lt=self.end_time, end_time__gt=self.start_time).exclude(pk=self.pk).exists():
                raise ValidationError('Цей час перетинається з іншим уроком у розкладі дзвінків школи.')


class WeeklyScheduleEntry(models.Model):
    WEEKDAYS = [(0, 'Понеділок'), (1, 'Вівторок'), (2, 'Середа'), (3, 'Четвер'),
                (4, "П’ятниця"), (5, 'Субота'), (6, 'Неділя')]
    journal = models.ForeignKey('journals.Journal', on_delete=models.CASCADE, related_name='weekly_schedule', verbose_name='Клас і предмет')
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS, verbose_name='День тижня', validators=[MaxValueValidator(6)])
    lesson_time = models.ForeignKey(LessonTime, on_delete=models.PROTECT, related_name='schedule_entries', verbose_name='Час уроку')
    start_date = models.DateField(verbose_name='Розклад діє з')
    end_date = models.DateField(verbose_name='Розклад діє до')
    room = models.CharField(max_length=50, blank=True, verbose_name='Кабінет')

    class Meta:
        ordering = ['weekday', 'lesson_time__number', 'pk']
        verbose_name = 'Урок у тижневому розкладі'
        verbose_name_plural = 'Тижневий розклад'
        constraints = [
            models.CheckConstraint(condition=Q(weekday__lte=6), name='schedule_valid_weekday'),
            models.CheckConstraint(condition=Q(end_date__gte=models.F('start_date')), name='schedule_valid_date_range'),
        ]

    def __str__(self):
        return f'{self.get_weekday_display()} · {self.lesson_time} · {self.journal.student_class.name} · {self.journal.subject.name}'

    def clean(self):
        super().clean()
        if not self.journal_id or not self.lesson_time_id:
            return
        if self.lesson_time.school_id != self.journal.school_id:
            raise ValidationError({'lesson_time': 'Оберіть час уроку зі своєї школи.'})
        if not self.start_date or not self.end_date:
            return
        year = self.journal.academic_year
        if not year.start_date <= self.start_date <= self.end_date <= year.end_date:
            raise ValidationError('Період розкладу має бути в межах навчального року, а початок — не пізніше завершення.')
        conflicts = WeeklyScheduleEntry.objects.filter(
            journal__school_id=self.journal.school_id, weekday=self.weekday,
            start_date__lte=self.end_date, end_date__gte=self.start_date,
            lesson_time__start_time__lt=self.lesson_time.end_time,
            lesson_time__end_time__gt=self.lesson_time.start_time,
        ).filter(Q(journal__teacher_id=self.journal.teacher_id) |
                 Q(journal__student_class_id=self.journal.student_class_id)).exclude(pk=self.pk)
        if conflicts.exists():
            raise ValidationError('Учитель або клас уже має урок у цей час. Оберіть інший час чи період розкладу.')
