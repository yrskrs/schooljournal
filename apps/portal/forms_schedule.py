from django import forms
from django.db.models import Q
from apps.curriculum.models import LessonTime, WeeklyScheduleEntry


class LessonTimeForm(forms.ModelForm):
    class Meta:
        model = LessonTime
        fields = ['number', 'start_time', 'end_time', 'is_active']
        widgets = {
            'start_time': forms.TimeInput(format='%H:%M', attrs={'type': 'time'}),
            'end_time': forms.TimeInput(format='%H:%M', attrs={'type': 'time'}),
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-control'


class ScheduleJournalChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, journal):
        return f'{journal.student_class.name} · {journal.subject.name}'


class WeeklyScheduleForm(forms.ModelForm):
    journal = ScheduleJournalChoiceField(queryset=None, label='Клас і предмет')
    start_date = forms.DateField(label='Розклад діє з', required=False,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    end_date = forms.DateField(label='Розклад діє до', required=False,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    class Meta:
        model = WeeklyScheduleEntry
        fields = ['journal', 'weekday', 'lesson_time', 'start_date', 'end_date', 'room']

    def __init__(self, *args, journals, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['journal'].queryset = journals
        self.fields['lesson_time'].queryset = LessonTime.objects.filter(school=school, is_active=True)
        if self.instance.pk:
            self.fields['lesson_time'].queryset = LessonTime.objects.filter(school=school).filter(
                Q(is_active=True) | Q(pk=self.instance.lesson_time_id))
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        data = super().clean()
        journal = data.get('journal')
        if journal:
            data['start_date'] = data.get('start_date') or journal.academic_year.start_date
            data['end_date'] = data.get('end_date') or journal.academic_year.end_date
        return data
