import io
import os
from pathlib import Path
from django.conf import settings
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import Journal, Lesson, Grade, SummaryGrade, Attendance, SummaryGradeType
from apps.journals.services.assessment import legacy_summary_groups


def _register_cyrillic_fonts():
    """Реєструє українські TrueType шрифти для ReportLab."""
    fonts_dir = Path(settings.BASE_DIR) / 'static' / 'fonts'
    reg_path = fonts_dir / 'Carlito-Regular.ttf'
    bold_path = fonts_dir / 'Carlito-Bold.ttf'

    # Fallback to system fonts if not in static
    if not reg_path.exists():
        system_reg = Path('/usr/share/fonts/google-carlito-fonts/Carlito-Regular.ttf')
        if system_reg.exists():
            reg_path = system_reg
    if not bold_path.exists():
        system_bold = Path('/usr/share/fonts/google-carlito-fonts/Carlito-Bold.ttf')
        if system_bold.exists():
            bold_path = system_bold

    registered = False
    if reg_path.exists():
        try:
            pdfmetrics.registerFont(TTFont('Carlito', str(reg_path)))
            if bold_path.exists():
                pdfmetrics.registerFont(TTFont('Carlito-Bold', str(bold_path)))
            else:
                pdfmetrics.registerFont(TTFont('Carlito-Bold', str(reg_path)))
            registered = True
        except Exception:
            pass

    return 'Carlito' if registered else 'Helvetica'


class NumberedCanvas(canvas.Canvas):
    """Канвас із нумерацією сторінок (Сторінка X з Y)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            super().showPage()
        super().save()

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Carlito" if "Carlito" in pdfmetrics.getRegisteredFontNames() else "Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        page_text = f"Сторінка {self._pageNumber} з {page_count}"
        self.drawRightString(842 - 36, 20, page_text)
        self.drawString(36, 20, "Електронний шкільний журнал")
        self.restoreState()


class JournalPDFExportService:
    @classmethod
    def generate_journal_pdf(cls, journal, semester=None, start_date=None, end_date=None):
        """Генерує друковану сторінку журналу в PDF формату А4 (альбомна орієнтація)."""
        font_name = _register_cyrillic_fonts()
        font_bold = "Carlito-Bold" if font_name == "Carlito" else "Helvetica-Bold"

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            leftMargin=25,
            rightMargin=25,
            topMargin=25,
            bottomMargin=35
        )

        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'UkrTitle',
            parent=styles['Normal'],
            fontName=font_bold,
            fontSize=13,
            leading=16,
            alignment=1, # Center
            textColor=colors.HexColor("#1E3A8A")
        )
        subtitle_style = ParagraphStyle(
            'UkrSubtitle',
            parent=styles['Normal'],
            fontName=font_bold,
            fontSize=10,
            leading=13,
            alignment=1,
            textColor=colors.HexColor("#0F172A")
        )
        meta_style = ParagraphStyle(
            'UkrMeta',
            parent=styles['Normal'],
            fontName=font_name,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#334155")
        )
        cell_header_style = ParagraphStyle(
            'CellHeader',
            parent=styles['Normal'],
            fontName=font_bold,
            fontSize=7,
            leading=8,
            alignment=1,
            textColor=colors.HexColor("#1E293B")
        )
        cell_style = ParagraphStyle(
            'CellText',
            parent=styles['Normal'],
            fontName=font_name,
            fontSize=7,
            leading=8,
            alignment=0, # Left
            textColor=colors.HexColor("#0F172A")
        )
        center_cell_style = ParagraphStyle(
            'CenterCell',
            parent=styles['Normal'],
            fontName=font_name,
            fontSize=7,
            leading=8,
            alignment=1, # Center
            textColor=colors.HexColor("#0F172A")
        )

        # 1. Заголовки
        elements.append(Paragraph(journal.school.name, title_style))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(
            f"КЛАСНИЙ ЖУРНАЛ: {journal.student_class.name} КЛАС | ПРЕДМЕТ: «{journal.subject.name.upper()}»",
            subtitle_style
        ))
        elements.append(Spacer(1, 4))

        period_label = semester.name if semester else "Весь навчальний рік"
        meta_text = (
            f"<b>Вчитель:</b> {journal.teacher.get_full_name_ukr()} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Навчальний рік:</b> {journal.academic_year.name} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Період:</b> {period_label}"
        )
        elements.append(Paragraph(meta_text, meta_style))
        elements.append(Spacer(1, 8))

        # 2. Отримуємо уроки
        lessons_qs = Lesson.objects.filter(journal=journal).order_by('date', 'lesson_number')
        if semester:
            lessons_qs = lessons_qs.filter(semester=semester)
        if start_date:
            lessons_qs = lessons_qs.filter(date__gte=start_date)
        if end_date:
            lessons_qs = lessons_qs.filter(date__lte=end_date)
        lessons = list(lessons_qs)

        # 3. Отримуємо учнів
        enrolled_student_ids = ClassEnrollment.objects.filter(
            student_class=journal.student_class
        ).values_list('student_id', flat=True)
        graded_student_ids = Grade.objects.filter(
            lesson__journal=journal
        ).values_list('student_id', flat=True)
        all_student_ids = set(enrolled_student_ids).union(set(graded_student_ids))
        students = list(Student.objects.filter(id__in=all_student_ids).order_by('last_name', 'first_name'))

        # Оцінки та відвідування
        grades_map = {}
        for g in Grade.objects.filter(lesson__in=lessons, is_approved=True):
            grades_map[(g.lesson_id, g.student_id)] = g.value

        attendance_map = {}
        for a in Attendance.objects.filter(lesson__in=lessons):
            s = a.get_symbol_ukr()
            if s:
                attendance_map[(a.lesson_id, a.student_id)] = s

        summary_grades_map = {}
        legacy_columns = []
        for key, group in legacy_summary_groups(journal).items():
            if any(sg.value for sg in group['grades'].values()):
                legacy_columns.append((key, group['label']))
                summary_grades_map.update({(sid, key): sg.value for sid, sg in group['grades'].items()})

        # Обмежуємо кількість колонок на сторінку для читабельності, якщо уроків багато
        # На сторінку формату А4 альбомна чудово поміщається 15-20 уроків + ПІБ + підсумки
        max_lessons_per_table = 18
        chunks = [lessons[i:i + max_lessons_per_table] for i in range(0, max(len(lessons), 1), max_lessons_per_table)]

        for chunk_idx, chunk_lessons in enumerate(chunks):
            if chunk_idx > 0:
                elements.append(PageBreak())
                elements.append(Paragraph(
                    f"{journal.student_class.name} клас — {journal.subject.name} (продовження)",
                    subtitle_style
                ))
                elements.append(Spacer(1, 6))

            # Побудова таблиці для цього шматка
            table_data = []

            # Рядок заголовків
            header_row = [
                Paragraph("<b>№</b>", cell_header_style),
                Paragraph("<b>Прізвище, ім'я учня</b>", cell_header_style)
            ]
            for l in chunk_lessons:
                d_str = l.date.strftime('%d.%m')
                t_label = {'TEST': 'КР', 'THEMATIC': 'ТЕМ', 'MODULAR': 'МОД', 'THEMATIC_NUSH': 'ТЕМ НУШ',
                           'SEMESTER_1': 'I сем', 'SEMESTER_2': 'II сем', 'SEMESTER_1_NUSH': 'I сем НУШ',
                           'SEMESTER_2_NUSH': 'II сем НУШ', 'ANNUAL': 'Річна'}.get(l.lesson_type, '')
                lbl = f"<b>{d_str}</b><br/>{t_label}" if t_label else f"<b>{d_str}</b>"
                header_row.append(Paragraph(lbl, cell_header_style))

            # Підсумкові колонки тільки в останньому шматку
            is_last_chunk = (chunk_idx == len(chunks) - 1)
            if is_last_chunk:
                for _, label in legacy_columns:
                    header_row.append(Paragraph(f"<b>{label}</b>", cell_header_style))

            table_data.append(header_row)

            # Рядки учнів
            for s_idx, student in enumerate(students, start=1):
                row = [
                    Paragraph(str(s_idx), center_cell_style),
                    Paragraph(student.get_full_name_ukr(), cell_style)
                ]
                for l in chunk_lessons:
                    v = grades_map.get((l.id, student.id), "")
                    att = attendance_map.get((l.id, student.id), "")
                    val = v
                    if att and not v:
                        val = f"<font color='#DC2626'><b>{att}</b></font>"
                    elif att and v:
                        val = f"{v}/<font color='#DC2626'>{att}</font>"
                    row.append(Paragraph(val, center_cell_style))

                if is_last_chunk:
                    for kind, _ in legacy_columns:
                        value = summary_grades_map.get((student.id, kind), '')
                        row.append(Paragraph(f"<b>{value}</b>", center_cell_style))

                table_data.append(row)

            # Розрахунок ширин колонок
            col_widths = [18, 140]
            lesson_col_w = 26
            for _ in chunk_lessons:
                col_widths.append(lesson_col_w)
            if is_last_chunk:
                col_widths.extend([28] * len(legacy_columns))

            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('LEFTPADDING', (0, 0), (-1, -1), 2),
                ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                ('LINEBELOW', (0, 0), (-1, 0), 1.2, colors.HexColor("#1E3A8A")),
            ]))
            elements.append(t)

        doc.build(elements, canvasmaker=NumberedCanvas)
        pdf_content = buffer.getvalue()
        buffer.close()
        return pdf_content
