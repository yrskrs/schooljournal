from django import template

register = template.Library()

@register.simple_tag
def get_grade(grades_map, lesson_id, student_id):
    """Отримує об'єкт Grade для комбінації (lesson_id, student_id)."""
    if not grades_map:
        return None
    return grades_map.get((lesson_id, student_id))

@register.simple_tag
def get_attendance(attendances_map, lesson_id, student_id):
    """Отримує об'єкт Attendance для комбінації (lesson_id, student_id)."""
    if not attendances_map:
        return None
    return attendances_map.get((lesson_id, student_id))

@register.simple_tag
def get_summary_grade(summary_grades_map, student_id, grade_type):
    """Отримує підсумкову оцінку для учня."""
    if not summary_grades_map:
        return None
    return summary_grades_map.get((student_id, grade_type))

@register.filter
def get_dict_item(dictionary, key):
    """Отримує значення зі словника за ключем."""
    if not dictionary:
        return None
    return dictionary.get(key)


@register.simple_tag
def get_cell_display(grade, attendance):
    """Повертає позначку відвідування ('Н', 'ХВ', 'ЗП') або оцінку ('1'..'12')."""
    if attendance:
        sym = attendance.get_symbol_ukr()
        if sym:
            return sym
    if grade and grade.value:
        return grade.value
    return ""


@register.simple_tag
def get_cell_class(grade, attendance):
    """Повертає CSS-клас для фарбування клітинки за типом відмітки."""
    if attendance:
        sym = attendance.get_symbol_ukr()
        if sym == 'Н':
            return 'is-absent'
        elif sym == 'ХВ':
            return 'is-excused'
        elif sym == 'ЗП':
            return 'is-late'
    if grade and grade.value:
        return 'is-grade'
    return ""


@register.simple_tag
def get_cell_sync_state(grade, attendance, lesson):
    if grade and not grade.is_approved:
        return {'css': 'sync-tentative', 'symbol': '◌', 'label': 'ШІ · попередня оцінка — очікує схвалення, вивантаження заборонене'}
    records = [record for record in (grade, attendance) if record]
    if not records:
        return None
    if any(not record.is_synced_to_nz for record in records):
        return {'css': 'sync-local', 'symbol': '●', 'label': 'Локальна зміна — ще не вивантажено в nz.ua'}
    if lesson.nz_schedule_id:
        active = attendance if attendance and attendance.get_symbol_ukr() else grade
        confirmed = active and (
            (active is grade and grade.nz_last_synced_value == grade.value) or
            (active is attendance and attendance.nz_last_synced_status == attendance.status)
        )
        if confirmed:
            return {'css': 'sync-online', 'symbol': '✓', 'label': 'Підтверджено синхронізацію з nz.ua'}
    return {'css': 'sync-local', 'symbol': '●', 'label': 'Локальний запис — синхронізацію з nz.ua не підтверджено'}


@register.simple_tag
def get_cell_version(grade, attendance):
    return str(max([record.updated_at.timestamp() for record in (grade, attendance) if record] or [0]))
