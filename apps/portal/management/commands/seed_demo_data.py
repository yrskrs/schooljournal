import os
import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from apps.schools.models import School
from apps.accounts.models import User, TeacherProfile
from apps.curriculum.models import (
    AcademicYear, Semester, Subject, StudentClass, Student, ClassEnrollment, TeacherAssignment
)
from apps.journals.models import (
    Journal, LessonPlanTopic, Lesson, Grade, SummaryGrade, Attendance,
    LessonType, SummaryGradeType, AuditLog
)
from apps.importers.services.student_service import StudentImportService
from apps.importers.services.topic_service import TopicImportService


class Command(BaseCommand):
    help = "Створює демонстраційні дані для тестування системи шкільного журналу"

    def handle(self, *args, **options):
        self.stdout.write("Початок створення демонстраційних даних...")

        with transaction.atomic():
            # Global administrators are created only by the browser bootstrap.
            # 2. Школа
            school, _ = School.objects.get_or_create(
                name="Київський ліцей № 142",
                defaults={
                    'short_name': "Ліцей 142",
                    'edrpou_code': "12345678",
                    'address': "м. Київ, вул. Політехнічна, 2-А",
                    'phone': "+38 (044) 456-78-90",
                    'email': "lyceum142@kyiv.edu.ua",
                    'is_active': True,
                }
            )
            self.stdout.write(f"Заклад освіти: {school.name}")

            # 3. Навчальний рік та семестри
            year, _ = AcademicYear.objects.get_or_create(
                school=school,
                name="2024/2025",
                defaults={
                    'start_date': datetime.date(2024, 9, 1),
                    'end_date': datetime.date(2025, 6, 27),
                    'is_current': True,
                    'is_archived': False,
                }
            )
            sem1, _ = Semester.objects.get_or_create(
                academic_year=year,
                number=1,
                defaults={
                    'name': "I семестр",
                    'start_date': datetime.date(2024, 9, 1),
                    'end_date': datetime.date(2024, 12, 27),
                }
            )
            sem2, _ = Semester.objects.get_or_create(
                academic_year=year,
                number=2,
                defaults={
                    'name': "II семестр",
                    'start_date': datetime.date(2025, 1, 13),
                    'end_date': datetime.date(2025, 6, 27),
                }
            )

            # 4. Вчителі
            teacher_math, t1_created = User.objects.get_or_create(
                username="kovalenko.o",
                defaults={
                    'email': "kovalenko@school.local",
                    'role': User.Role.TEACHER,
                    'school': school,
                    'last_name': "Коваленко",
                    'first_name': "Олена",
                    'patronymic': "Миколаївна",
                    'phone': "+38 (050) 111-22-33",
                    'is_active': True,
                }
            )
            if t1_created:
                teacher_math.set_password("TeacherPass123!")
                teacher_math.save()
                TeacherProfile.objects.create(
                    user=teacher_math,
                    qualification="Вища категорія",
                    pedagogical_title="Вчитель-методист"
                )

            teacher_lang, t2_created = User.objects.get_or_create(
                username="melnyk.v",
                defaults={
                    'email': "melnyk@school.local",
                    'role': User.Role.TEACHER,
                    'school': school,
                    'last_name': "Мельник",
                    'first_name': "Василь",
                    'patronymic': "Петрович",
                    'phone': "+38 (067) 222-33-44",
                    'is_active': True,
                }
            )
            if t2_created:
                teacher_lang.set_password("TeacherPass123!")
                teacher_lang.save()
                TeacherProfile.objects.create(
                    user=teacher_lang,
                    qualification="Перша категорія",
                    pedagogical_title="Старший вчитель"
                )

            # 5. Предмети
            subj_math, _ = Subject.objects.get_or_create(school=school, name="Математика", defaults={'code': "МАТ"})
            subj_ukr, _ = Subject.objects.get_or_create(school=school, name="Українська мова", defaults={'code': "УКР-МОВ"})
            subj_lit, _ = Subject.objects.get_or_create(school=school, name="Українська література", defaults={'code': "УКР-ЛІТ"})
            subj_info, _ = Subject.objects.get_or_create(school=school, name="Інформатика", defaults={'code': "ІНФ"})

            # 6. Класи
            class_5a, _ = StudentClass.objects.get_or_create(
                school=school,
                academic_year=year,
                name="5-А",
                defaults={'grade_level': 5, 'letter': "А", 'class_teacher': teacher_math}
            )
            class_7b, _ = StudentClass.objects.get_or_create(
                school=school,
                academic_year=year,
                name="7-Б",
                defaults={'grade_level': 7, 'letter': "Б", 'class_teacher': teacher_lang}
            )

            # 7. Призначення вчителів
            assign_math, _ = TeacherAssignment.objects.get_or_create(
                school=school,
                teacher=teacher_math,
                subject=subj_math,
                student_class=class_5a,
                academic_year=year,
                defaults={'is_active': True}
            )
            TeacherAssignment.objects.get_or_create(
                school=school,
                teacher=teacher_lang,
                subject=subj_ukr,
                student_class=class_5a,
                academic_year=year,
                defaults={'is_active': True}
            )
            TeacherAssignment.objects.get_or_create(
                school=school,
                teacher=teacher_lang,
                subject=subj_lit,
                student_class=class_7b,
                academic_year=year,
                defaults={'is_active': True}
            )

            # 8. Учні 5-А класу
            students_data_5a = [
                ("STD-5A-01", "Бойко", "Андрій", "Олександрович"),
                ("STD-5A-02", "Григоренко", "Софія", "Володимирівна"),
                ("STD-5A-03", "Дмитренко", "Максим", "Ігорович"),
                ("STD-5A-04", "Коваленко", "Марія", "Олексіївна"),
                ("STD-5A-05", "Кравченко", "Денис", "Сергійович"),
                ("STD-5A-06", "Лисенко", "Анна", "Михайлівна"),
                ("STD-5A-07", "Поліщук", "Олександр", "Романович"),
                ("STD-5A-08", "Шевчук", "Дарина", "Василівна"),
            ]
            students_5a = []
            for uid, ln, fn, mn in students_data_5a:
                st, _ = Student.objects.get_or_create(
                    school=school,
                    internal_id=uid,
                    defaults={'last_name': ln, 'first_name': fn, 'middle_name': mn, 'status': Student.Status.ACTIVE}
                )
                ClassEnrollment.objects.get_or_create(
                    student=st,
                    student_class=class_5a,
                    defaults={'start_date': datetime.date(2024, 9, 1), 'is_active': True}
                )
                students_5a.append(st)

            # Учні 7-Б класу
            students_data_7b = [
                ("STD-7B-01", "Васильєв", "Олег", "Петрович"),
                ("STD-7B-02", "Захарченко", "Юлія", "Вікторівна"),
                ("STD-7B-03", "Мороз", "Артем", "Дмитрович"),
                ("STD-7B-04", "Ткаченко", "Катерина", "Тарасівна"),
            ]
            for uid, ln, fn, mn in students_data_7b:
                st, _ = Student.objects.get_or_create(
                    school=school,
                    internal_id=uid,
                    defaults={'last_name': ln, 'first_name': fn, 'middle_name': mn, 'status': Student.Status.ACTIVE}
                )
                ClassEnrollment.objects.get_or_create(
                    student=st,
                    student_class=class_7b,
                    defaults={'start_date': datetime.date(2024, 9, 1), 'is_active': True}
                )

            # 9. Журнал 5-А: Математика
            journal, _ = Journal.objects.get_or_create(
                school=school,
                academic_year=year,
                student_class=class_5a,
                subject=subj_math,
                defaults={'teacher': teacher_math, 'is_archived': False}
            )

            # 10. Планові теми
            plan_topics_data = [
                (1, "Повторення: Додавання та віднімання натуральних чисел", "2024-09-02", "№ 12, 15", LessonType.ORDINARY),
                (2, "Множення та ділення натуральних чисел", "2024-09-04", "№ 24, 28", LessonType.ORDINARY),
                (3, "Самостійна робота з теми «Арифметика натуральних чисел»", "2024-09-06", "Повторити правила", LessonType.QUIZ),
                (4, "Відрізок. Довжина відрізка. Промінь", "2024-09-09", "№ 45-48", LessonType.ORDINARY),
                (5, "Координатний промінь. Шкала", "2024-09-11", "№ 60, 62", LessonType.ORDINARY),
                (6, "Діагностична контрольна робота №1", "2024-09-13", "", LessonType.TEST),
                (7, "Аналіз контрольної роботи. Рівняння", "2024-09-16", "№ 85, 87", LessonType.ORDINARY),
                (8, "Текстові задачі на рух", "2024-09-18", "№ 102, 105", LessonType.ORDINARY),
                (9, "Тематичне оцінювання з теми 1", "2024-09-20", "", LessonType.THEMATIC),
            ]
            for num, title, p_date, hw, l_type in plan_topics_data:
                LessonPlanTopic.objects.get_or_create(
                    journal=journal,
                    topic_number=num,
                    defaults={
                        'title': title,
                        'planned_date': datetime.date.fromisoformat(p_date) if p_date else None,
                        'homework': hw,
                        'lesson_type': l_type,
                        'is_conducted': True
                    }
                )

            # 11. Проведені уроки та заповнення оцінок/відвідування
            lessons_sample = [
                ("2024-09-02", 1, "Повторення: Додавання та віднімання натуральних чисел", "№ 12, 15", LessonType.ORDINARY),
                ("2024-09-04", 1, "Множення та ділення натуральних чисел", "№ 24, 28", LessonType.ORDINARY),
                ("2024-09-06", 1, "Самостійна робота з теми «Арифметика натуральних чисел»", "Повторити правила", LessonType.QUIZ),
                ("2024-09-09", 1, "Відрізок. Довжина відрізка. Промінь", "№ 45-48", LessonType.ORDINARY),
                ("2024-09-11", 1, "Координатний промінь. Шкала", "№ 60, 62", LessonType.ORDINARY),
                ("2024-09-13", 1, "Діагностична контрольна робота №1", "", LessonType.TEST),
            ]

            grades_matrix = [
                # Бойко:
                ["10", "11", "10", "9", "", "11"],
                # Григоренко:
                ["11", "12", "12", "11", "12", "12"],
                # Дмитренко:
                ["7", "8", "7", "", "8", "8"],
                # Коваленко:
                ["9", "9", "10", "10", "", "10"],
                # Кравченко:
                ["6", "", "6", "7", "6", "6"],
                # Лисенко:
                ["10", "11", "", "11", "10", "11"],
                # Поліщук:
                ["8", "8", "9", "8", "9", "9"],
                # Шевчук:
                ["11", "10", "11", "12", "11", "12"],
            ]

            attendance_matrix = [
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.ABSENT_EXCUSED, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.ABSENT_UNEXCUSED, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.ABSENT_EXCUSED, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.LATE, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.ABSENT_EXCUSED, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
                [Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT, Attendance.Status.PRESENT],
            ]

            for l_idx, (d_str, l_num, t_name, hw_text, lt_type) in enumerate(lessons_sample):
                l_date = datetime.date.fromisoformat(d_str)
                lesson, _ = Lesson.objects.get_or_create(
                    journal=journal,
                    date=l_date,
                    lesson_number=l_num,
                    defaults={
                        'semester': sem1,
                        'topic': t_name,
                        'homework': hw_text,
                        'lesson_type': lt_type,
                        'created_by': teacher_math
                    }
                )

                for s_idx, st in enumerate(students_5a):
                    g_val = grades_matrix[s_idx][l_idx]
                    if g_val:
                        Grade.objects.get_or_create(
                            lesson=lesson,
                            student=st,
                            defaults={'value': g_val, 'created_by': teacher_math}
                        )

                    att_val = attendance_matrix[s_idx][l_idx]
                    if att_val != Attendance.Status.PRESENT:
                        Attendance.objects.get_or_create(
                            lesson=lesson,
                            student=st,
                            defaults={'status': att_val, 'reason': "За запискою батьків" if att_val == Attendance.Status.ABSENT_EXCUSED else ""}
                        )

            # Підсумкові оцінки за 1 семестр
            for s_idx, st in enumerate(students_5a):
                sem_grade = ["10", "12", "8", "10", "6", "11", "9", "11"][s_idx]
                SummaryGrade.objects.get_or_create(
                    journal=journal,
                    student=st,
                    grade_type=SummaryGradeType.SEMESTER_1,
                    defaults={'value': sem_grade, 'semester': sem1}
                )

            # 12. Створення зразків файлів імпорту
            samples_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))), 'samples')
            os.makedirs(samples_dir, exist_ok=True)

            # Учні (CSV та XLSX)
            st_csv, _ = StudentImportService.generate_template('csv')
            with open(os.path.join(samples_dir, 'sample_students.csv'), 'wb') as f:
                f.write(st_csv)

            st_xlsx, _ = StudentImportService.generate_template('xlsx')
            with open(os.path.join(samples_dir, 'sample_students.xlsx'), 'wb') as f:
                f.write(st_xlsx)

            # Теми (CSV та XLSX)
            tp_csv, _ = TopicImportService.generate_template('csv')
            with open(os.path.join(samples_dir, 'sample_topics.csv'), 'wb') as f:
                f.write(tp_csv)

            tp_xlsx, _ = TopicImportService.generate_template('xlsx')
            with open(os.path.join(samples_dir, 'sample_topics.xlsx'), 'wb') as f:
                f.write(tp_xlsx)

        self.stdout.write(self.style.SUCCESS("Демонстраційні дані та файли-зразки успішно створено!"))
        self.stdout.write(self.style.NOTICE('Суперадміністратор створюється через початкове налаштування у браузері.'))
        self.stdout.write(self.style.NOTICE("Вчитель математики: kovalenko.o / TeacherPass123!"))
        self.stdout.write(self.style.NOTICE("Вчитель мови/літ.: melnyk.v / TeacherPass123!"))
