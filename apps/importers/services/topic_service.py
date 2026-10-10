import io
import csv
import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from django.db import transaction
from apps.journals.models import Journal, LessonPlanTopic, Lesson, LessonType, AuditLog


class TopicImportService:
    @staticmethod
    def generate_template(file_format='xlsx'):
        """Генерує шаблон календарно-тематичного плану."""
        headers = [
            "№ уроку",
            "Тема уроку",
            "Дата уроку (РРРР-ММ-ДД або ДД.ММ.РРРР)",
            "Домашнє завдання",
            "Тип уроку (ORDINARY/QUIZ/TEST/THEMATIC)",
            "Примітки"
        ]
        sample_rows = [
            [1, "Вступний урок. Правила безпеки", "2024-09-02", "Повторити §1", "ORDINARY", "Ознайомлення"],
            [2, "Повторення вивченого за попередній рік", "2024-09-05", "Вправи 1-5", "ORDINARY", ""],
            [3, "Діагностична контрольна робота", "2024-09-09", "", "TEST", "Контрольна робота"],
            [4, "Аналіз контрольної роботи", "2024-09-12", "Робота над помилками", "ORDINARY", ""],
            [5, "Тема 1. Основні поняття", "", "§2, вивчити правила", "ORDINARY", "Без дати - планова тема"],
            [6, "Тематичне оцінювання з теми 1", "", "", "THEMATIC", "Тематичний підсумок"],
        ]

        if file_format == 'csv':
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            for row in sample_rows:
                writer.writerow(row)
            return output.getvalue().encode('utf-8-sig'), 'text/csv'
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Тематичний план"

            header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
            header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")

            ws.append(headers)
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

            for row in sample_rows:
                ws.append(row)

            for col in ws.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = openpyxl.utils.get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            return output.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    @classmethod
    def parse_file(cls, file_obj, filename):
        """Парсить завантажений файл XLSX або CSV."""
        rows = []
        is_csv = filename.lower().endswith('.csv')

        if is_csv:
            content = file_obj.read()
            decoded = None
            for enc in ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1']:
                try:
                    decoded = content.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if decoded is None:
                raise ValueError("Не вдалося розпізнати кодування файлу CSV. Використовуйте UTF-8.")

            reader = csv.reader(io.StringIO(decoded))
            raw_rows = list(reader)
        else:
            wb = openpyxl.load_workbook(file_obj, data_only=True)
            ws = wb.active
            raw_rows = []
            for row in ws.iter_rows(values_only=True):
                if any(c is not None and str(c).strip() != '' for c in row):
                    raw_rows.append(list(row))

        if not raw_rows:
            return []

        raw_headers = [str(c or '').strip() for c in raw_rows[0]]
        header_map = cls._map_headers(raw_headers)

        for row_num, raw_row in enumerate(raw_rows[1:], start=2):
            if not any(c is not None and str(c).strip() != '' for c in raw_row):
                continue

            item = {'_row_num': row_num}
            for key, col_idx in header_map.items():
                if col_idx is not None and col_idx < len(raw_row):
                    val = raw_row[col_idx]
                    item[key] = str(val).strip() if val is not None else ""
                else:
                    item[key] = ""
            rows.append(item)

        return rows

    @classmethod
    def _map_headers(cls, headers):
        mapping = {
            'topic_number': None,
            'title': None,
            'date': None,
            'homework': None,
            'lesson_type': None,
            'notes': None,
        }
        for idx, h in enumerate(headers):
            hl = h.lower().strip()
            if any(k in hl for k in ['№', 'номер', 'порядок', 'number']):
                mapping['topic_number'] = idx
            elif any(k in hl for k in ['тема', 'назва', 'topic', 'title']):
                mapping['title'] = idx
            elif any(k in hl for k in ['дата', 'число', 'date']):
                mapping['date'] = idx
            elif any(k in hl for k in ['дз', 'домашнє', 'завдання', 'homework']):
                mapping['homework'] = idx
            elif any(k in hl for k in ['тип', 'type']):
                mapping['lesson_type'] = idx
            elif any(k in hl for k in ['приміт', 'нотат', 'notes']):
                mapping['notes'] = idx

        if mapping['title'] is None and len(headers) >= 2:
            mapping['title'] = 1
        if mapping['topic_number'] is None:
            mapping['topic_number'] = 0

        return mapping

    @classmethod
    def _parse_date(cls, date_str):
        if not date_str:
            return None
        formats = ['%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%Y.%m.%d', '%d-%m-%Y']
        for fmt in formats:
            try:
                return datetime.datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        # Also handle ISO with time
        try:
            return datetime.date.fromisoformat(date_str[:10])
        except (ValueError, TypeError):
            return None

    @classmethod
    def validate_and_preview(cls, rows, journal):
        """
        Перевіряє коректність тем, виявляє наявні уроки/теми та конфлікти.
        """
        preview_data = []
        errors = []
        warnings = []

        existing_plan_topics = {t.topic_number: t for t in LessonPlanTopic.objects.filter(journal=journal)}
        existing_lessons = {l.date: l for l in Lesson.objects.filter(journal=journal)}

        for idx, row in enumerate(rows, start=1):
            row_num = row.get('_row_num', idx)
            raw_num = row.get('topic_number', '').strip()
            title = row.get('title', '').strip()
            raw_date = row.get('date', '').strip()
            homework = row.get('homework', '').strip()
            lesson_type_str = row.get('lesson_type', '').strip().upper() or LessonType.ORDINARY
            notes = row.get('notes', '').strip()

            row_errors = []
            row_warnings = []

            # Перевірка теми
            if not title:
                row_errors.append("Тема уроку є обов'язковою")

            # Перевірка номера
            try:
                topic_number = int(raw_num) if raw_num else idx
            except ValueError:
                topic_number = idx
                row_warnings.append(f"Некоректний номер уроку '{raw_num}'. Призначено автоматично: {idx}")

            # Перевірка дати
            parsed_date = None
            if raw_date:
                parsed_date = cls._parse_date(raw_date)
                if not parsed_date:
                    row_errors.append(f"Некоректний формат дати '{raw_date}'. Використовуйте РРРР-ММ-ДД або ДД.ММ.РРРР")

            # Перевірка типу уроку
            valid_types = [t[0] for t in LessonType.choices]
            if lesson_type_str not in valid_types:
                lesson_type_str = LessonType.ORDINARY

            # Виявлення збігів/конфліктів
            exists_in_plan = topic_number in existing_plan_topics
            exists_lesson = parsed_date in existing_lessons if parsed_date else False

            if exists_in_plan:
                row_warnings.append(f"У тематичному плані вже є урок №{topic_number}: «{existing_plan_topics[topic_number].title}»")
            if exists_lesson:
                row_warnings.append(f"У журналі на дату {parsed_date.strftime('%d.%m.%Y')} вже є проведений урок: «{existing_lessons[parsed_date].topic}»")

            preview_item = {
                'row_num': row_num,
                'topic_number': topic_number,
                'title': title,
                'date': parsed_date.isoformat() if parsed_date else "",
                'date_display': parsed_date.strftime('%d.%m.%Y') if parsed_date else "Не вказано (план)",
                'homework': homework,
                'lesson_type': lesson_type_str,
                'notes': notes,
                'exists_in_plan': exists_in_plan,
                'exists_lesson': exists_lesson,
                'is_valid': len(row_errors) == 0,
                'errors': row_errors,
                'warnings': row_warnings,
            }
            preview_data.append(preview_item)
            if row_errors:
                errors.append(f"Рядок {row_num}: {'; '.join(row_errors)}")
            if row_warnings:
                warnings.append(f"Рядок {row_num}: {'; '.join(row_warnings)}")

        return {
            'preview_data': preview_data,
            'total_rows': len(preview_data),
            'valid_rows': len([p for p in preview_data if p['is_valid']]),
            'error_rows': len([p for p in preview_data if not p['is_valid']]),
            'errors': errors,
            'warnings': warnings,
        }

    @classmethod
    @transaction.atomic
    def execute_import(cls, preview_data, journal, mode='add_and_update', user=None):
        """
        Застосовує імпорт тем уроків.
        ВАЖЛИВО: Оновлення тем НЕ змінює та НЕ видаляє виставлені оцінки та відвідуваність!
        """
        added_plan = 0
        updated_plan = 0
        added_lessons = 0
        updated_lessons = 0
        skipped = 0

        # Семестри журналу для автоматичної прив'язки уроків
        semesters = list(journal.academic_year.semesters.all())

        for item in preview_data:
            if not item.get('is_valid', True):
                skipped += 1
                continue

            topic_num = item.get('topic_number')
            title = item.get('title')
            date_str = item.get('date')
            homework = item.get('homework')
            lesson_type = item.get('lesson_type', LessonType.ORDINARY)
            notes = item.get('notes', '')

            lesson_date = datetime.date.fromisoformat(date_str) if date_str else None

            # 1. Оновлення або створення в LessonPlanTopic
            plan_topic = LessonPlanTopic.objects.filter(journal=journal, topic_number=topic_num).first()
            if plan_topic:
                if mode != 'add_only':
                    plan_topic.title = title
                    if lesson_date:
                        plan_topic.planned_date = lesson_date
                    if homework:
                        plan_topic.homework = homework
                    plan_topic.lesson_type = lesson_type
                    if notes:
                        plan_topic.notes = notes
                    plan_topic.save()
                    updated_plan += 1
                else:
                    skipped += 1
            else:
                if mode != 'update_only':
                    plan_topic = LessonPlanTopic.objects.create(
                        journal=journal,
                        topic_number=topic_num,
                        title=title,
                        planned_date=lesson_date,
                        homework=homework,
                        lesson_type=lesson_type,
                        notes=notes
                    )
                    added_plan += 1

            # 2. Якщо вказано дату — створюємо або оновлюємо реальний урок (Lesson)
            if lesson_date:
                # Визначаємо семестр для дати
                target_sem = None
                for sem in semesters:
                    if sem.start_date <= lesson_date <= sem.end_date:
                        target_sem = sem
                        break

                lesson = Lesson.objects.filter(journal=journal, date=lesson_date, lesson_number=1).first()
                if lesson:
                    if mode != 'add_only':
                        # Оновлюємо тему та ДЗ уроку, але ЗБЕРІГАЄМО ОЦІНКИ ТА ВІДВІДУВАНІСТЬ!
                        lesson.topic = title
                        if homework:
                            lesson.homework = homework
                        lesson.lesson_type = lesson_type
                        lesson.is_synced_to_nz = False
                        if notes:
                            lesson.notes = notes
                        if plan_topic:
                            lesson.linked_plan_topic = plan_topic
                        lesson.save()
                        updated_lessons += 1
                else:
                    if mode != 'update_only':
                        lesson = Lesson.objects.create(
                            journal=journal,
                            semester=target_sem,
                            date=lesson_date,
                            lesson_number=1,
                            topic=title,
                            homework=homework,
                            lesson_type=lesson_type,
                            notes=notes,
                            linked_plan_topic=plan_topic,
                            created_by=user
                        )
                        added_lessons += 1

                if plan_topic and not plan_topic.is_conducted:
                    plan_topic.is_conducted = True
                    plan_topic.save()

        # AuditLog
        AuditLog.objects.create(
            school=journal.school,
            user=user,
            action=AuditLog.Action.IMPORT,
            target_model='LessonPlanTopic',
            target_id=str(journal.id),
            description=f"Імпорт тем для {journal}: теми плану (+{added_plan}, ~{updated_plan}), уроки (+{added_lessons}, ~{updated_lessons})",
            changes={
                'added_plan': added_plan,
                'updated_plan': updated_plan,
                'added_lessons': added_lessons,
                'updated_lessons': updated_lessons,
                'skipped': skipped
            }
        )

        return {
            'added_plan': added_plan,
            'updated_plan': updated_plan,
            'added_lessons': added_lessons,
            'updated_lessons': updated_lessons,
            'skipped': skipped,
        }
