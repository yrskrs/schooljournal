import re
from django import forms
from django.contrib.auth import authenticate
from apps.journals.models import GradeAPIConnection, Lesson
from apps.journals.services.scheduling import SUMMARY_TYPES
from apps.journals.services.external_grades import validate_api_url


STUDENT_FIELDS = [
    ('id', 'Локальний ID учня'), ('nz_student_id', 'ID учня nz.ua'), ('internal_id', 'Внутрішній ID'),
    ('name', 'ПІБ учня'), ('class', 'Назва класу'), ('status', 'Статус навчання'),
]


class StudentAPIForm(forms.Form):
    enabled = forms.BooleanField(label='Дозволити API списку учнів', required=False)
    fields = forms.MultipleChoiceField(label='Що надавати', choices=STUDENT_FIELDS,
        widget=forms.CheckboxSelectMultiple, required=True)
    expires_at = forms.DateTimeField(label='Ключ діє до (необов’язково)', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'))


class GradeAPIConnectionForm(forms.ModelForm):
    clear_key = forms.BooleanField(label='Прибрати збережений ключ API', required=False)

    class Meta:
        model = GradeAPIConnection
        fields = ['name', 'source', 'contract_version', 'source_class_id', 'source_subject_id', 'url', 'enabled', 'api_key', 'student_identifier', 'records_key', 'id_key',
            'student_key', 'date_key', 'value_key', 'ai_key', 'approved_key', 'lesson_key']
        widgets = {'api_key': forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved_key = self.instance.api_key if self.instance.pk else ''
        self.fields['api_key'].help_text = 'Порожнє поле зберігає наявний ключ. Надсилається як Authorization: Bearer.'
        self.fields['url'].help_text = 'Повна адреса маршруту JSON, наприклад http://192.168.1.13:8080/api/grades.'
        self.fields['records_key'].help_text = 'Для вкладених полів використовуйте крапку: data.grades. Для кореневого списку залиште порожнім.'
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-control'
        self.fields['enabled'].widget.attrs['id'] = 'connection_enabled'

    def clean_url(self):
        value = self.cleaned_data['url']
        validate_api_url(value)
        return value

    def clean(self):
        data = super().clean()
        if data.get('contract_version') == 2:
            if data.get('source') not in ('schooltest', 'schoolwork'):
                self.add_error('source', 'Оберіть сайт тестування або завдань для API v2.')
            for key in ('source_class_id', 'source_subject_id'):
                if not data.get(key):
                    self.add_error(key, 'Для API v2 потрібне явне зіставлення класу та предмета.')
        for key in ('records_key', 'id_key', 'student_key', 'date_key', 'value_key', 'ai_key', 'approved_key', 'lesson_key'):
            value = data.get(key, '')
            if value and not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', value):
                self.add_error(key, 'Вкажіть назву поля JSON або шлях через крапку.')
        api_key = data.get('api_key', '')
        if any(character in api_key for character in ('\r', '\n')):
            self.add_error('api_key', 'Ключ не може містити переносів рядка.')
        data['api_key'] = '' if data.get('clear_key') else api_key or self.saved_key
        return data


class PullGradesForm(forms.Form):
    date_from = forms.DateField(label='З дати', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    date_to = forms.DateField(label='До дати', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

    def clean(self):
        data = super().clean()
        if data.get('date_from') and data.get('date_to') and data['date_from'] > data['date_to']:
            raise forms.ValidationError('Кінець періоду має бути не раніше початку.')
        return data


class LessonChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, lesson):
        return f'{lesson.date:%d.%m.%Y} · урок №{lesson.lesson_number} · {lesson.topic or "Без теми"}'


class GradeLessonForm(forms.Form):
    lesson = LessonChoiceField(queryset=Lesson.objects.none(), required=False,
        label='За який урок виставити нові оцінки', empty_label='Автоматично за сесією та датою',
        widget=forms.Select(attrs={'class': 'form-control'}))

    def __init__(self, *args, journal, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lesson'].queryset = journal.lessons.exclude(lesson_type__in=SUMMARY_TYPES).filter(
            date__gte=journal.academic_year.start_date, date__lte=journal.academic_year.end_date).order_by('-date', 'lesson_number', 'pk')


class LocalProfileLinkForm(forms.Form):
    username = forms.CharField(label='Логін додаткового локального профілю', max_length=150)
    password = forms.CharField(label='Пароль локального профілю', widget=forms.PasswordInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.local_user = None
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        data = super().clean()
        self.local_user = authenticate(request=self.request, username=data.get('username'), password=data.get('password'))
        if not self.local_user or self.local_user.is_nz_personal or not self.local_user.is_teacher_user or self.local_user.is_superadmin_user:
            raise forms.ValidationError('Підтвердьте логін і пароль активного локального профілю вчителя.')
        return data
