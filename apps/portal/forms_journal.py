from django import forms
import datetime
from apps.curriculum.models import LessonTime
from apps.journals.models import Journal, LessonType
from apps.journals.services.scheduling import SUMMARY_TYPES
from apps.journals.services.assessment import legacy_summary_groups, assessment_type_choices


class LessonTimeSelect(forms.Select):
    def format_value(self, value):
        if isinstance(value, datetime.time):
            value = value.strftime('%H:%M')
        elif isinstance(value, str) and value:
            try:
                value = datetime.time.fromisoformat(value).strftime('%H:%M')
            except ValueError:
                pass
        return super().format_value(value)


class SchoolTimeSelectionMixin:
    def __init__(self, *args, journal=None, preserve_time=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.journal = journal
        self.preserve_time = preserve_time
        slots = list(LessonTime.objects.filter(school=journal.school, is_active=True)) if journal else []
        self.allowed_times = {slot.start_time for slot in slots}
        choices = [('', 'Без часу')] + [(slot.start_time.strftime('%H:%M'), str(slot)) for slot in slots]
        if preserve_time and preserve_time not in self.allowed_times:
            choices.append((preserve_time.strftime('%H:%M'), f'Раніше збережено: {preserve_time:%H:%M}'))
        if 'lesson_time' in self.fields:
            self.fields['lesson_time'].widget = LessonTimeSelect(choices=choices, attrs={'class': 'form-control'})

    def clean_lesson_time(self):
        value = self.cleaned_data.get('lesson_time')
        if self.data.get('school_time_list') == '1' and value and value not in self.allowed_times and value != self.preserve_time:
            raise forms.ValidationError('Оберіть час зі шкільного списку «Час уроків».')
        return value


class LessonForm(SchoolTimeSelectionMixin, forms.Form):
    date = forms.DateField()
    lesson_number = forms.IntegerField(min_value=1, max_value=32767, required=False)
    lesson_time = forms.TimeField(required=False)
    topic = forms.CharField(max_length=255)
    homework = forms.CharField(required=False, widget=forms.Textarea)
    lesson_type = forms.ChoiceField(choices=LessonType.choices)
    notes = forms.CharField(max_length=255, required=False)
    plan_topic_id = forms.IntegerField(min_value=1, required=False)
    insert_before_id = forms.IntegerField(min_value=1, required=False)
    homework_target = forms.CharField(required=False)


class LessonCreateForm(LessonForm):
    repeat_frequency = forms.ChoiceField(required=False, choices=[
        ('none', 'Не повторювати'), ('weekly', 'Щотижня'),
        ('biweekly', 'Кожні 2 тижні'), ('custom', 'Кожні N тижнів'),
    ])
    repeat_interval_weeks = forms.IntegerField(required=False, min_value=1, max_value=52)
    repeat_end = forms.ChoiceField(required=False, choices=[('count', 'За кількістю уроків'), ('date', 'До дати')])
    repeat_count = forms.IntegerField(required=False, min_value=2, max_value=100)
    repeat_until = forms.DateField(required=False)

    def clean(self):
        data = super().clean()
        frequency = data.get('repeat_frequency') or 'none'
        data['repeat_frequency'] = frequency
        if frequency == 'none':
            return data
        if frequency == 'custom' and data.get('repeat_interval_weeks') is None:
            self.add_error('repeat_interval_weeks', 'Вкажіть інтервал повторення у тижнях.')
        end = data.get('repeat_end') or 'count'
        data['repeat_end'] = end
        if end == 'count' and data.get('repeat_count') is None:
            self.add_error('repeat_count', 'Вкажіть кількість уроків, включно з першим.')
        if end == 'date' and data.get('repeat_until') is None:
            self.add_error('repeat_until', 'Оберіть дату завершення повторень.')
        if end == 'date' and data.get('repeat_until') and data.get('date') and data['repeat_until'] < data['date']:
            self.add_error('repeat_until', 'Кінцева дата має бути не раніше першого уроку.')
        return data


class SummaryColumnForm(SchoolTimeSelectionMixin, forms.Form):
    lesson_type = forms.ChoiceField(choices=[choice for choice in LessonType.choices if choice[0] in SUMMARY_TYPES])
    date = forms.DateField()
    lesson_time = forms.TimeField(required=False)
    title = forms.CharField(max_length=255, required=False)
    plan_topic_id = forms.IntegerField(min_value=1, required=False)

    def clean(self):
        data = super().clean()
        if self.data.get('repeat_frequency', 'none') not in ('', 'none'):
            raise forms.ValidationError('Повторення застосовується до навчальних уроків. Підсумкову колонку додайте окремо.')
        if self.data.get('insert_before_id'):
            raise forms.ValidationError('Для підсумкової колонки оберіть дату «Відображати після».')
        return data


class TopicJournalChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, journal):
        return f'{journal.student_class.name} · {journal.subject.name} · {journal.academic_year.name}'


class TopicCopyForm(forms.Form):
    source_journal = TopicJournalChoiceField(label='З якого журналу копіювати', queryset=Journal.objects.none())
    source_kind = forms.ChoiceField(label='Джерело тем', choices=[('lessons', 'Уроки журналу'), ('plan', 'Календарний план')])
    source_start = forms.IntegerField(label='З номера уроку', min_value=1, max_value=2147483647)
    source_end = forms.IntegerField(label='До номера включно (порожньо — до кінця)', min_value=1, max_value=2147483647, required=False)
    target_kind = forms.ChoiceField(label='Куди копіювати', choices=[('lessons', 'Наявні уроки за порядком'), ('plan', 'Календарний план (нові теми без дат)')])
    target_start = forms.IntegerField(label='Початковий номер у цьому журналі', min_value=1, max_value=2147483647)
    include_homework = forms.BooleanField(label='Також копіювати текст домашнього завдання', required=False)
    replace_existing = forms.BooleanField(label='Замінити вже заповнені теми', required=False)

    def __init__(self, *args, journal, user, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.portal.permissions import has_journal_access
        candidates = Journal.objects.filter(school=journal.school, subject=journal.subject).exclude(pk=journal.pk).select_related(
            'student_class', 'subject', 'academic_year')
        accessible_ids = [candidate.pk for candidate in candidates if has_journal_access(user, candidate)]
        self.fields['source_journal'].queryset = candidates.filter(pk__in=accessible_ids)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-control'

    def clean(self):
        data = super().clean()
        if data.get('source_end') and data.get('source_start') and data['source_end'] < data['source_start']:
            self.add_error('source_end', 'Кінцевий номер має бути не меншим за початковий.')
        return data


class AssessmentForm(SchoolTimeSelectionMixin, forms.Form):
    grade_type = forms.ChoiceField(choices=[c for c in LessonType.choices if c[0] in SUMMARY_TYPES])
    date = forms.DateField(label='Положення оцінки в журналі', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    lesson_time = forms.TimeField(label='Час уроку', required=False, widget=forms.TimeInput(attrs={'type': 'time'}))
    title = forms.CharField(label='Назва теми / колонки', max_length=255, required=False)
    start_date = forms.DateField(label='Оцінки з дати', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    end_date = forms.DateField(label='Оцінки до дати', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    source = forms.ChoiceField(label='Враховувати', choices=[
        ('current', 'Поточні оцінки (без підсумкових)'),
        ('thematic', 'Лише тематичні оцінки'),
        ('semester', 'Лише семестрові оцінки'),
        ('all', 'Усі вибрані оцінки'),
    ])
    source_types = forms.MultipleChoiceField(label='Типи уроків / оцінок для розрахунку', required=False,
        choices=assessment_type_choices('all'), widget=forms.CheckboxSelectMultiple)
    rounding = forms.ChoiceField(label='Округлення', choices=[
        ('nearest', 'До найближчого цілого (8,5 → 9)'),
        ('down', 'Донизу (8,5 → 8)'), ('up', 'Догори (8,1 → 9)'),
    ])
    lesson_ids = forms.MultipleChoiceField(label='Уроки й теми для розрахунку', widget=forms.CheckboxSelectMultiple)
    target_lesson = forms.ChoiceField(label='Колонка для результату', required=False)

    def __init__(self, *args, journal, compact=False, **kwargs):
        super().__init__(*args, journal=journal, **kwargs)
        source = self.data.get('source', self.initial.get('source', 'current'))
        default_types = [choice[0] for choice in assessment_type_choices(source)]
        self.fields['source_types'].initial = default_types
        if compact:
            self.fields['lesson_ids'].required = False
            self.fields['source_types'].initial = (
                [choice[0] for choice in assessment_type_choices('current')]
                if self.initial.get('grade_type') in (LessonType.THEMATIC, LessonType.MODULAR, LessonType.THEMATIC_NUSH) else
                [LessonType.SEMESTER_1, LessonType.SEMESTER_2]
                if self.initial.get('grade_type') == LessonType.ANNUAL else [])
        # Preserve previews submitted from a page opened before type filters
        # were introduced; an explicitly empty new selection stays invalid.
        if self.is_bound and not self.data.get('source_types_present') and not self.data.get('source_types'):
            self.data = self.data.copy()
            if hasattr(self.data, 'setlist'):
                self.data.setlist('source_types', default_types)
            else:
                self.data['source_types'] = default_types
        lessons = list(journal.lessons.order_by('date', 'lesson_number'))
        self.fields['lesson_ids'].choices = [(str(l.pk), f'{l.date:%d.%m.%Y} · №{l.lesson_number} · {l.topic} ({l.get_lesson_type_display()})') for l in lessons]
        self.fields['lesson_ids'].choices += [
            (f'legacy:{key}', f'Раніше збережено: {group["label"]} (без дати; лише за окремим вибором)')
            for key, group in legacy_summary_groups(journal).items()
        ]
        self.fields['target_lesson'].choices = [('', 'Створити нову колонку після вибраної дати')] + [
            (str(l.pk), f'{l.date:%d.%m.%Y} · {l.topic}') for l in lessons if l.lesson_type in SUMMARY_TYPES
        ]
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxSelectMultiple):
                field.widget.attrs['class'] = 'form-control'

    def clean(self):
        data = super().clean()
        for name in ('date', 'start_date', 'end_date'):
            if data.get(name) and not self.journal.academic_year.start_date <= data[name] <= self.journal.academic_year.end_date:
                self.add_error(name, 'Оберіть дату в межах навчального року.')
        if data.get('start_date') and data.get('end_date') and data['start_date'] > data['end_date']:
            raise forms.ValidationError('Початкова дата має бути не пізніше кінцевої.')
        selected_types = set(data.get('source_types', []))
        allowed_types = {choice[0] for choice in assessment_type_choices(data.get('source'))}
        if self.data.get('source_types_present') and not selected_types:
            self.add_error('source_types', 'Оберіть хоча б один тип уроків / оцінок.')
        elif selected_types - allowed_types:
            self.add_error('source_types', 'Оберіть типи, що відповідають вибраному джерелу оцінок.')
        return data
