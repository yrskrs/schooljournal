"""Explicit, reviewable calculations. NZ's exact rounding awaits confirmation."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING
from hashlib import sha256
import json

from django.db.models import Q
from django.utils import timezone

from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import Grade, Attendance, LessonType, SummaryGrade, SummaryGradeType
from .scheduling import SUMMARY_TYPES

ROUNDING = {'nearest': ROUND_HALF_UP, 'down': ROUND_FLOOR, 'up': ROUND_CEILING}


def assessment_period(journal, grade_type, placement_date=None, semester_id=None):
    """Use journal dates, never modification timestamps, to delimit a new summary."""
    year = journal.academic_year
    semesters = list(year.semesters.order_by('number'))
    number = {LessonType.SEMESTER_1: 1, LessonType.SEMESTER_2: 2,
              LessonType.SEMESTER_1_NUSH: 1, LessonType.SEMESTER_2_NUSH: 2}.get(grade_type)
    semester = next((s for s in semesters if s.number == number), None) if number else None
    if not semester and semester_id:
        semester = next((s for s in semesters if str(s.pk) == str(semester_id)), None)
    placement_date = placement_date or timezone.localdate()
    lower = semester.start_date if semester else year.start_date
    upper = semester.end_date if semester else year.end_date
    placement_date = max(lower, min(placement_date, upper))
    if not semester:
        semester = next((s for s in semesters if s.start_date <= placement_date <= s.end_date), None)
    start_date = semester.start_date if semester and grade_type != LessonType.ANNUAL else year.start_date
    boundary = None
    if grade_type != LessonType.ANNUAL:
        kinds = ([LessonType.SEMESTER_1_NUSH, LessonType.SEMESTER_2_NUSH] if grade_type in (LessonType.SEMESTER_1_NUSH, LessonType.SEMESTER_2_NUSH) else
                 [LessonType.SEMESTER_1, LessonType.SEMESTER_2] if number else [grade_type])
        # A summary column without marks has not yet assessed the period.
        boundary = journal.lessons.filter(
            lesson_type__in=kinds, date__range=(start_date, placement_date),
            grades__value__regex=r'\S',
        ).order_by('-date', '-lesson_number', '-pk').first()
        if boundary:
            start_date = boundary.date
    return {'date': placement_date, 'start_date': start_date, 'end_date': placement_date,
            'semester': semester, 'boundary': boundary}


def period_lessons(lessons, start_date, end_date, boundary=None):
    """Exclude the previous summary and earlier same-day lessons when applicable."""
    return [lesson for lesson in lessons if start_date <= lesson.date <= end_date and (
        not boundary or start_date < boundary.date or
        (lesson.date, lesson.lesson_number, lesson.pk) > (boundary.date, boundary.lesson_number, boundary.pk))]


def journal_students(journal):
    enrolled = ClassEnrollment.objects.filter(student_class=journal.student_class).values('student_id')
    graded = Grade.objects.filter(lesson__journal=journal).values('student_id')
    summaries = SummaryGrade.objects.filter(journal=journal).values('student_id')
    return Student.objects.filter(school=journal.school).filter(
        Q(pk__in=enrolled) | Q(pk__in=graded) | Q(pk__in=summaries)
    ).order_by('last_name', 'first_name', 'pk')


def legacy_summary_groups(journal):
    groups = {}
    for grade in SummaryGrade.objects.filter(journal=journal).select_related('semester').order_by('updated_at', 'pk'):
        kind = grade.grade_type
        suffix = sha256(grade.thematic_name.encode()).hexdigest()[:16] if grade.thematic_name else ''
        key = f'{kind}:{grade.semester_id or 0}:{suffix}' if suffix or grade.semester_id else kind
        label = dict(SummaryGradeType.choices).get(kind, kind)
        if grade.thematic_name:
            label += f' · {grade.thematic_name}'
        if grade.semester:
            label += f' · {grade.semester.name}'
        groups.setdefault(key, {'label': label, 'grades': {}})['grades'][grade.student_id] = grade
    return groups


def assessment_type_choices(source):
    choices = list(LessonType.choices) + [('ADJUSTED', 'Скоригована оцінка (раніше збережено)')]
    if source == 'current':
        return [choice for choice in choices if choice[0] not in SUMMARY_TYPES and choice[0] != 'ADJUSTED']
    if source == 'thematic':
        return [choice for choice in choices if choice[0] == LessonType.THEMATIC]
    if source == 'semester':
        return [choice for choice in choices if choice[0] in (LessonType.SEMESTER_1, LessonType.SEMESTER_2)]
    return choices if source == 'all' else []


def filter_summary_sources(summaries, source):
    if source == 'current':
        return []
    types = {'thematic': {SummaryGradeType.THEMATIC},
             'semester': {SummaryGradeType.SEMESTER_1, SummaryGradeType.SEMESTER_2}}
    return [s for s in summaries if source == 'all' or s.grade_type in types.get(source, set())]


def calculate(journal, lessons, source, rounding, summaries=None):
    if rounding not in ROUNDING or source not in ('current', 'thematic', 'semester', 'all'):
        raise ValueError('Некоректний спосіб розрахунку.')
    if source == 'current':
        lessons = [l for l in lessons if l.lesson_type not in SUMMARY_TYPES]
    elif source == 'thematic':
        lessons = [l for l in lessons if l.lesson_type == LessonType.THEMATIC]
    elif source == 'semester':
        lessons = [l for l in lessons if l.lesson_type in (LessonType.SEMESTER_1, LessonType.SEMESTER_2)]
    rows = []
    summaries = filter_summary_sources(summaries or [], source)
    grades = list(Grade.objects.filter(lesson__in=lessons, is_approved=True).order_by('pk'))
    absences = list(Attendance.objects.filter(lesson__in=lessons).exclude(status=Attendance.Status.PRESENT).order_by('pk'))
    absent = {(a.lesson_id, a.student_id) for a in absences}
    students = list(journal_students(journal))
    for student in students:
        values = [g.numeric_value for g in grades if g.student_id == student.pk
                  and g.numeric_value is not None and (g.lesson_id, g.student_id) not in absent]
        values.extend(s.numeric_value for s in summaries if s.student_id == student.pk and s.numeric_value is not None)
        average = Decimal(sum(values)) / Decimal(len(values)) if values else None
        value = str(average.quantize(Decimal('1'), rounding=ROUNDING[rounding])) if values else ''
        rows.append({'student': student, 'count': len(values), 'average': average, 'value': value})
    # Changes to source marks, types, student membership or attendance invalidate a preview.
    snapshot = {
        'students': [s.pk for s in students],
        'lessons': [(l.pk, l.lesson_type, l.updated_at.isoformat()) for l in lessons],
        'grades': [(g.pk, g.value, g.updated_at.isoformat()) for g in grades],
        'attendance': [(a.pk, a.status, a.updated_at.isoformat()) for a in absences],
        'legacy': [(s.pk, s.value, s.updated_at.isoformat()) for s in summaries],
    }
    fingerprint = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return rows, fingerprint, lessons
