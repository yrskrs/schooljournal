from django.db import models
from django.utils import timezone
from apps.schools.models import School
from apps.accounts.models import User
from apps.curriculum.models import AcademicYear, Semester, StudentClass, Subject, Student


class LessonType(models.TextChoices):
    ORDINARY = 'ORDINARY', 'Звичайний урок'
    QUIZ = 'QUIZ', 'Самостійна робота'
    TEST = 'TEST', 'Контрольна робота'
    THEMATIC = 'THEMATIC', 'Тематичне оцінювання'
    MODULAR = 'MODULAR', 'Модульне оцінювання'
    THEMATIC_NUSH = 'THEMATIC_NUSH', 'Тематичне оцінювання НУШ'
    SEMESTER_1 = 'SEMESTER_1', 'Оцінка за I семестр'
    SEMESTER_2 = 'SEMESTER_2', 'Оцінка за II семестр'
    SEMESTER_1_NUSH = 'SEMESTER_1_NUSH', 'Оцінка за I семестр НУШ'
    SEMESTER_2_NUSH = 'SEMESTER_2_NUSH', 'Оцінка за II семестр НУШ'
    ANNUAL = 'ANNUAL', 'Річна оцінка'
    PRACTICAL = 'PRACTICAL', 'Практична робота'
    LAB = 'LAB', 'Лабораторна робота'
    EXAM = 'EXAM', 'Залік / Іспит'


class Journal(models.Model):
    """
    Клас журналу. Пов'язаний зі школою, навчальним роком, класом, предметом і вчителем.
    Обмеження в базі даних запобігають випадковому створенню дублікатів.
    """
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name='journals',
        verbose_name="Школа"
    )
    academic_year = models.ForeignKey(
        AcademicYear,
        on_delete=models.CASCADE,
        related_name='journals',
        verbose_name="Навчальний рік"
    )
    student_class = models.ForeignKey(
        StudentClass,
        on_delete=models.CASCADE,
        related_name='journals',
        verbose_name="Клас"
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name='journals',
        verbose_name="Предмет"
    )
    teacher = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='journals',
        verbose_name="Вчитель"
    )
    is_archived = models.BooleanField(
        default=False,
        verbose_name="Архівний",
        help_text="Архівні журнали доступні лише для читання та експорту."
    )
    allows_override_teacher = models.BooleanField(
        default=False,
        verbose_name="Дозволити редагування заміні (іншим вчителям)"
    )
    nz_journal_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="ID журналу в nz.ua"
    )
    nz_subgroup_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="ID підгрупи в nz.ua"
    )
    nz_last_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Час останньої синхронізації з nz.ua"
    )
    nz_owner = models.ForeignKey('NZAccount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='owned_journals', verbose_name='Особистий акаунт nz.ua')
    local_profile = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='linked_nz_journals', verbose_name='Додатковий локальний профіль')
    ignored_students = models.ManyToManyField(
        Student,
        blank=True,
        related_name='ignored_in_journals',
        verbose_name="Ігноровані учні при синхронізації"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Журнал"
        verbose_name_plural = "Журнали"
        unique_together = ('academic_year', 'student_class', 'subject')
        ordering = ['student_class__name', 'subject__name']

    def __str__(self):
        archived = " [АРХІВ]" if self.is_archived else ""
        return f"{self.student_class.name} — {self.subject.name} ({self.teacher.get_full_name_ukr()}){archived}"


class LessonPlanTopic(models.Model):
    """
    Планова тема уроку з календарно-тематичного плану.
    Може бути імпортована без дати як майбутній план або з датою.
    """
    journal = models.ForeignKey(
        Journal,
        on_delete=models.CASCADE,
        related_name='plan_topics',
        verbose_name="Журнал"
    )
    topic_number = models.PositiveIntegerField(verbose_name="№ уроку за планом")
    title = models.CharField(max_length=255, verbose_name="Тема уроку")
    planned_date = models.DateField(null=True, blank=True, verbose_name="Запланована дата")
    homework = models.TextField(blank=True, verbose_name="Домашнє завдання")
    lesson_type = models.CharField(
        max_length=50,
        choices=LessonType.choices,
        default=LessonType.ORDINARY,
        verbose_name="Тип уроку"
    )
    notes = models.CharField(max_length=255, blank=True, verbose_name="Примітки")
    is_conducted = models.BooleanField(default=False, verbose_name="Проведено")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Планова тема уроку"
        verbose_name_plural = "Планові теми уроків"
        ordering = ['topic_number']

    def __str__(self):
        return f"№{self.topic_number}. {self.title}"


class Lesson(models.Model):
    """
    Фактично проведений урок у журналі.
    Може бути кілька уроків на одну дату (поле lesson_number).
    """
    journal = models.ForeignKey(
        Journal,
        on_delete=models.CASCADE,
        related_name='lessons',
        verbose_name="Журнал"
    )
    semester = models.ForeignKey(
        Semester,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lessons',
        verbose_name="Семестр"
    )
    date = models.DateField(verbose_name="Дата уроку")
    lesson_time = models.TimeField(null=True, blank=True, verbose_name="Час уроку")
    lesson_number = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="Номер уроку на дату",
        help_text="Для розрізнення кількох уроків на один день (1, 2, ...)"
    )
    topic = models.CharField(max_length=255, verbose_name="Тема уроку")
    homework = models.TextField(blank=True, verbose_name="Домашнє завдання")
    homework_due_lesson = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_homework', verbose_name="Домашнє завдання на урок"
    )
    homework_due_plan_topic = models.ForeignKey(
        LessonPlanTopic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_homework', verbose_name="Домашнє завдання на планову тему"
    )
    homework_for_next_lesson = models.BooleanField(default=False, verbose_name="ДЗ на наступний урок")
    homework_due_date = models.DateField(null=True, blank=True, verbose_name="Дата виконання ДЗ з nz.ua")
    lesson_type = models.CharField(
        max_length=50,
        choices=LessonType.choices,
        default=LessonType.ORDINARY,
        verbose_name="Тип уроку"
    )
    notes = models.CharField(max_length=255, blank=True, verbose_name="Примітка")
    linked_plan_topic = models.ForeignKey(
        LessonPlanTopic,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='real_lessons',
        verbose_name="Пов'язана тема з плану"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Автор запису"
    )
    nz_schedule_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="ID уроку (schedule) в nz.ua"
    )
    is_synced_to_nz = models.BooleanField(
        default=False,
        verbose_name="Синхронізовано з nz.ua"
    )
    is_ignored_for_nz = models.BooleanField(
        default=False,
        verbose_name="Ігнорувати для nz.ua"
    )
    nz_last_synced_topic = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Остання синхронізована тема в nz.ua"
    )
    nz_last_synced_homework = models.TextField(
        blank=True,
        default="",
        verbose_name="Останнє синхронізоване ДЗ в nz.ua"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Проведений урок"
        verbose_name_plural = "Проведені уроки"
        unique_together = ('journal', 'date', 'lesson_number')
        ordering = ['date', 'lesson_number']

    def __str__(self):
        return f"{self.date.strftime('%d.%m.%Y')} (#{self.lesson_number}): {self.topic}"

    def get_calendar_lesson_number(self):
        """
        Порядковий номер уроку за календарним плануванням / навчальним журналом.
        1) Якщо урок прив'язаний до теми календарного плану — номер теми з плану (topic_number).
        2) Якщо sequence_number вже закешовано/визначено на екземплярі — використовуємо його.
        3) Інакше — порядковий номер серед навчальних уроків журналу.
        """
        if self.linked_plan_topic_id:
            try:
                if self.linked_plan_topic and self.linked_plan_topic.topic_number:
                    return self.linked_plan_topic.topic_number
            except Exception:
                pass
        if hasattr(self, 'sequence_number') and self.sequence_number:
            return self.sequence_number
        if not self.journal_id or not self.pk:
            return self.lesson_number
        from apps.journals.services.topic_copy import numbered_lessons
        lessons = self.journal.lessons.order_by('date', 'lesson_number', 'pk')
        seq_map = {l.pk: num for num, l in numbered_lessons(lessons).items()}
        return seq_map.get(self.pk, self.lesson_number)

    def get_homework_due_lesson(self):
        if self.homework_due_lesson_id:
            return self.homework_due_lesson
        if self.homework_due_plan_topic_id:
            return self.homework_due_plan_topic.real_lessons.order_by('date', 'lesson_number').first()
        if self.homework_due_date:
            return None
        if self.homework_for_next_lesson or self.homework:
            from django.db.models import Q
            return self.journal.lessons.filter(
                Q(date__gt=self.date) | Q(date=self.date, lesson_number__gt=self.lesson_number)
            ).exclude(lesson_type__in=[LessonType.THEMATIC, LessonType.SEMESTER_1, LessonType.SEMESTER_2, LessonType.ANNUAL]).order_by('date', 'lesson_number').first()
        return None

    def get_homework_target_label(self):
        target = self.get_homework_due_lesson()
        if target:
            target_num = target.get_calendar_lesson_number()
            return f"На {target.date:%d.%m.%Y}, урок №{target_num}: {target.topic}"
        if self.homework_due_plan_topic_id:
            return f"На тему №{self.homework_due_plan_topic.topic_number}: {self.homework_due_plan_topic.title}"
        if self.homework_due_date:
            return f"На {self.homework_due_date:%d.%m.%Y}"
        if self.homework_for_next_lesson or self.homework:
            return "На наступний урок (дату ще не заплановано)"
        return ""


SPECIAL_GRADE_VALUES = ('ЗАР', 'НЕЗАР', 'ЗВ', 'Н/А', 'Н/З', 'ВИВЧ', 'П', 'С', 'Д', 'В', 'Н/О', 'ЗАУВ', 'П/П', 'К')


class Grade(models.Model):
    """
    Оцінка учня за конкретний урок.
    Порожня клітинка — це відсутність запису або порожній рядок (не нуль!).
    Базова шкала: 1-12 балів.
    Поле updated_at використовується для оптимістичного блокування при паралельному редагуванні.
    """
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='grades',
        verbose_name="Урок"
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='grades',
        verbose_name="Учень"
    )
    value = models.CharField(
        max_length=10,
        blank=True,
        default="",
        verbose_name="Оцінка"
    )
    numeric_value = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Числове значення для статистики"
    )
    comment = models.CharField(max_length=255, blank=True, verbose_name="Коментар")
    is_approved = models.BooleanField(default=True, verbose_name='Остаточна оцінка')
    import_record = models.ForeignKey('ExternalGrade', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='local_grades', verbose_name='Джерело імпорту')
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Хто виставив"
    )
    is_synced_to_nz = models.BooleanField(
        default=False,
        verbose_name="Синхронізовано з nz.ua"
    )
    is_ignored_for_nz = models.BooleanField(
        default=False,
        verbose_name="Ігнорувати для nz.ua"
    )
    nz_last_synced_value = models.CharField(
        max_length=10,
        blank=True,
        default="",
        verbose_name="Останнє синхронізоване значення в nz.ua"
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Час останнього оновлення")

    class Meta:
        verbose_name = "Оцінка"
        verbose_name_plural = "Оцінки"
        unique_together = ('lesson', 'student')
        ordering = ['student', 'lesson__date']

    def save(self, *args, **kwargs):
        val = self.value.strip() if self.value else ""
        self.value = val
        if val.isdigit() and self.is_approved:
            num = int(val)
            if 1 <= num <= 12:
                self.numeric_value = num
            else:
                self.numeric_value = None
        else:
            self.numeric_value = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student.get_short_name_ukr()}: {self.value} ({self.lesson.date})"


class SummaryGradeType(models.TextChoices):
    THEMATIC = 'THEMATIC', 'Тематична'
    SEMESTER_1 = 'SEMESTER_1', 'I Семестр'
    SEMESTER_2 = 'SEMESTER_2', 'II Семестр'
    ANNUAL = 'ANNUAL', 'Річна'
    ADJUSTED = 'ADJUSTED', 'Скоригована'


class SummaryGrade(models.Model):
    """
    Підсумкові оцінки: Тематичні, семестрові, річні.
    Вчитель вводить їх вручну.
    """
    journal = models.ForeignKey(
        Journal,
        on_delete=models.CASCADE,
        related_name='summary_grades',
        verbose_name="Журнал"
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='summary_grades',
        verbose_name="Учень"
    )
    semester = models.ForeignKey(
        Semester,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Семестр"
    )
    grade_type = models.CharField(
        max_length=20,
        choices=SummaryGradeType.choices,
        verbose_name="Тип підсумкової оцінки"
    )
    thematic_name = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="Назва теми (для тематичної)"
    )
    value = models.CharField(max_length=10, blank=True, default="", verbose_name="Оцінка")
    numeric_value = models.PositiveSmallIntegerField(null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True, verbose_name="Коментар")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Підсумкова оцінка"
        verbose_name_plural = "Підсумкові оцінки"
        ordering = ['student', 'grade_type']

    def save(self, *args, **kwargs):
        val = self.value.strip() if self.value else ""
        self.value = val
        if val.isdigit():
            num = int(val)
            if 1 <= num <= 12:
                self.numeric_value = num
            else:
                self.numeric_value = None
        else:
            self.numeric_value = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student.get_short_name_ukr()} [{self.get_grade_type_display()}]: {self.value}"


class Attendance(models.Model):
    """
    Відвідуваність на уроці: Присутній, Н, ХВ, ЗП.
    Поле updated_at використовується для оптимістичного блокування.
    """
    class Status(models.TextChoices):
        PRESENT = 'PRESENT', 'Присутній'
        ABSENT_UNEXCUSED = 'ABSENT_UNEXCUSED', 'Н (Неповажна)'
        ABSENT_EXCUSED = 'ABSENT_EXCUSED', 'ХВ (Поважна / Хвороба)'
        LATE = 'LATE', 'ЗП (Запізнення)'

    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='attendances',
        verbose_name="Урок"
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name='attendances',
        verbose_name="Учень"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PRESENT,
        verbose_name="Статус присутності"
    )
    reason = models.CharField(max_length=255, blank=True, verbose_name="Причина відсутності")
    is_synced_to_nz = models.BooleanField(
        default=False,
        verbose_name="Синхронізовано з nz.ua"
    )
    is_ignored_for_nz = models.BooleanField(
        default=False,
        verbose_name="Ігнорувати для nz.ua"
    )
    nz_last_synced_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="Останній синхронізований статус в nz.ua"
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Час останнього оновлення")

    class Meta:
        verbose_name = "Відвідування"
        verbose_name_plural = "Відвідування"
        unique_together = ('lesson', 'student')
        ordering = ['student', 'lesson__date']

    def get_symbol_ukr(self):
        if self.status == self.Status.ABSENT_UNEXCUSED:
            return 'Н'
        elif self.status == self.Status.ABSENT_EXCUSED:
            return 'ХВ'
        elif self.status == self.Status.LATE:
            return 'ЗП'
        return ''

    def __str__(self):
        return f"{self.student.get_short_name_ukr()}: {self.get_symbol_ukr() or 'Присутній'}"


class AuditLog(models.Model):
    """
    Журнал аудиту змін: оцінки, відвідування, теми, списки учнів.
    Зберігає автора, час, попереднє та нове значення.
    """
    class Action(models.TextChoices):
        CREATE = 'CREATE', 'Створення'
        UPDATE = 'UPDATE', 'Зміна'
        DELETE = 'DELETE', 'Видалення'
        IMPORT = 'IMPORT', 'Імпорт'
        EXPORT = 'EXPORT', 'Експорт'

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='audit_logs',
        verbose_name="Школа"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Користувач (автор)"
    )
    timestamp = models.DateTimeField(default=timezone.now, verbose_name="Дата та час")
    action = models.CharField(max_length=20, choices=Action.choices, verbose_name="Дія")
    target_model = models.CharField(max_length=50, verbose_name="Модель")
    target_id = models.CharField(max_length=50, blank=True, verbose_name="ID об'єкта")
    description = models.CharField(max_length=255, verbose_name="Опис операції")
    changes = models.JSONField(default=dict, blank=True, verbose_name="Змінені поля (old/new)")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP адреса")

    class Meta:
        verbose_name = "Запис аудиту"
        verbose_name_plural = "Журнал аудиту"
        ordering = ['-timestamp']

    def __str__(self):
        user_name = self.user.get_full_name_ukr() if self.user else "Система"
        return f"[{self.timestamp.strftime('%d.%m.%Y %H:%M')}] {user_name} - {self.description}"


class NZAccount(models.Model):
    """
    Акаунт викладача для підключення до порталу nz.ua.
    Зберігає збережені кукі/сесію та логін.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='nz_account',
        verbose_name="Вчитель"
    )
    login = models.CharField(max_length=150, blank=True, verbose_name="Логін nz.ua")
    phpsessid = models.CharField(max_length=255, blank=True, verbose_name="PHPSESSID сесії")
    cookies = models.JSONField(default=dict, blank=True, verbose_name="Збережені cookies")
    is_active = models.BooleanField(default=False, verbose_name="Активна сесія")
    last_synced_at = models.DateTimeField(null=True, blank=True, verbose_name="Остання перевірка")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Акаунт nz.ua"
        verbose_name_plural = "Акаунти nz.ua"

    def __str__(self):
        return f"nz.ua ({self.login or self.user.username})"


class NZSyncLog(models.Model):
    """
    Журнал синхронізації журналу з порталом nz.ua.
    Фіксує детальний звіт: які оцінки/теми відправлено, чи успішно.
    """
    class Status(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Успішно'
        PARTIAL = 'PARTIAL', 'Частково'
        FAILED = 'FAILED', 'Помилка'

    journal = models.ForeignKey(
        Journal,
        on_delete=models.CASCADE,
        related_name='nz_sync_logs',
        verbose_name="Журнал"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Користувач"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUCCESS,
        verbose_name="Статус"
    )
    items_count = models.PositiveIntegerField(default=0, verbose_name="Кількість записів")
    success_count = models.PositiveIntegerField(default=0, verbose_name="Успішно оновлено")
    error_count = models.PositiveIntegerField(default=0, verbose_name="Помилок")
    details = models.JSONField(default=dict, blank=True, verbose_name="Детальний звіт")
    error_message = models.TextField(blank=True, verbose_name="Текст помилки")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Час синхронізації")

    class Meta:
        verbose_name = "Звіт синхронізації nz.ua"
        verbose_name_plural = "Звіти синхронізації nz.ua"
        ordering = ['-created_at']

    def __str__(self):
        return f"Синхронізація {self.journal} ({self.created_at.strftime('%d.%m.%Y %H:%M')}) - {self.get_status_display()}"



from .integration_models import StudentAPIGrant, GradeAPIConnection, GradeLessonAssignment, GradeStudentMapping, ExternalGrade, GradePullLog, RosterReplica, RosterState, ExtraStudent, APIInboxResult, DeletedAPIIdentity  # noqa: E402,F401
from .connection_models import NetworkProfile, NetworkSelection, ServiceConnection, ServiceEndpoint  # noqa: E402,F401
