import json
from collections import Counter
from django import forms

from apps.journals.services.assessment import journal_students
from apps.journals.services.connection_setup import SOURCES, source_urls, selected_students
from school_sync.journal_roster import similar


class ConnectSourceForm(forms.Form):
    connection_code = forms.CharField(label='Код підключення зі старої версії', required=False, max_length=5000,
        widget=forms.Textarea(attrs={'rows': 3, 'autocomplete': 'off', 'class': 'form-control'}))
    source = forms.ChoiceField(label='Що підключаємо', choices=list(SOURCES.items()), required=False)
    key_name = forms.CharField(label='Назва ключа / підключення', required=False, max_length=100,
        help_text='Наприклад: Мій ключ для тестів або Завдання школи.')
    site_url = forms.URLField(label='Адреса сайту тестів або завдань (IP і порт)', max_length=450, required=False,
        help_text='Вставте повну адресу, за якою відкриваєте джерело, наприклад http://192.168.10.25:8000. Вона має бути доступна серверу журналу.')
    api_key = forms.CharField(label='Ключ API', required=False, max_length=500,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    server_group = forms.CharField(label="Група сервера (об'єднання IP)", required=False, max_length=100,
        help_text='Необов’язково: назва сервера для об’єднання різних IP (наприклад: Сервер тестів).')
    location_tag = forms.CharField(label="Локація / Мережа", required=False, max_length=50,
        help_text='Наприклад: 🏠 Дім, 🏫 Кабінет інформатики, 💻 Ноутбук.')

    def clean(self):
        data = super().clean()
        key = data.get('api_key', '').strip()
        code = data.get('connection_code', '').strip() or (key if key.startswith('{') else '')
        if code:
            try:
                payload = json.loads(code)
                if not isinstance(payload, dict):
                    raise ValueError
                for key in ('source', 'site_url', 'api_key'):
                    if not isinstance(payload.get(key), str):
                        raise ValueError
                    if key != 'site_url' or not data.get('site_url'):
                        data[key] = payload[key].strip()
            except (ValueError, TypeError):
                raise forms.ValidationError('Вставте лише ключ API в поле ключа й адресу сайту в поле адреси. Старий код має містити source, site_url та api_key.')
        data['api_key'] = data.get('api_key', '').strip()
        if data.get('source') not in SOURCES:
            self.add_error('source', 'Оберіть тести або завдання.')
        if not data.get('api_key') or len(data['api_key']) > 500 or any(c in data['api_key'] for c in ('\r', '\n')):
            self.add_error('api_key', 'Потрібен чинний ключ API без переносів рядка.')
        if not data.get('site_url'):
            self.add_error('site_url', 'Вставте адресу сайту.')
        else:
            data['grades_url'], _ = source_urls(data['site_url'])
        return data


class SourceContextForm(forms.Form):
    source_class_id = forms.ChoiceField(label='Клас на сайті джерела')
    source_subject_id = forms.ChoiceField(label='Предмет на сайті джерела')
    version = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, connection, **kwargs):
        super().__init__(*args, **kwargs)
        for field, kind in [('source_class_id', 'classes'), ('source_subject_id', 'subjects')]:
            rows = connection.roster_cache.get(kind, [])
            self.fields[field].choices = [('', '— Оберіть назву —')] + [(row['id'], row['name']) for row in rows]


class ConfirmStudentsForm(forms.Form):
    confirm = forms.BooleanField(label='Я перевірив(ла) відповідність учнів і підтверджую вибрані записи.')
    version = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, connection, **kwargs):
        super().__init__(*args, **kwargs)
        local = list(journal_students(connection.journal))
        counts = Counter(student.get_full_name_ukr().casefold() for student in local)
        source = selected_students(connection)
        source_counts = Counter(row['name'].casefold() for row in source)
        mappings = {row.external_id: str(row.student_id) for row in connection.student_mappings.all()}
        choices = [('', 'Позакласний учень / перевірити пізніше')] + [(str(student.pk), student.get_full_name_ukr()) for student in local]
        choices.append(('new', 'Додати як нового учня цього класу'))
        for row in source:
            suggested = mappings.get(row['id'], '')
            if not suggested and counts[row['name'].casefold()] == 1 and source_counts[row['name'].casefold()] == 1:
                suggested = next(str(student.pk) for student in local if student.get_full_name_ukr().casefold() == row['name'].casefold())
            self.fields['pupil_' + row['id']] = forms.ChoiceField(label=row['name'], choices=choices, required=False, initial=suggested)
            if not suggested:
                names = [s.get_full_name_ukr() for s in local if similar(row['name'], s.get_full_name_ukr())]
                if names:
                    self.fields['pupil_' + row['id']].help_text = 'Можлива помилка або скорочення: ' + ', '.join(names[:5]) + '. Оберіть учня лише після перевірки.'


class EditConnectionForm(forms.Form):
    name = forms.CharField(label='Назва підключення', max_length=100, required=True,
        widget=forms.TextInput(attrs={'class': 'form-control'}))
    source = forms.ChoiceField(label='Тип сервісу джерела', choices=list(SOURCES.items()), required=False,
        widget=forms.Select(attrs={'class': 'form-control'}))
    site_url = forms.URLField(label='Адреса сайту (IP і порт)', max_length=450, required=True,
        widget=forms.URLInput(attrs={'class': 'form-control'}),
        help_text='Повна адреса, наприклад http://192.168.1.13:8080')
    api_key = forms.CharField(label='Новий ключ API (залиште порожнім, щоб не змінювати)',
        required=False, max_length=500,
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password'}))
    enabled = forms.BooleanField(label='Підключення активне (використовувати для синхронізації)', required=False)
    source_class_id = forms.ChoiceField(label='Клас на сайті джерела', required=False,
        widget=forms.Select(attrs={'class': 'form-control'}))
    source_subject_id = forms.ChoiceField(label='Предмет на сайті джерела', required=False,
        widget=forms.Select(attrs={'class': 'form-control'}))
    server_group = forms.CharField(label="Група сервера (об'єднання IP)", max_length=100, required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Наприклад: Сервер тестування'}),
        help_text="Вкажіть однакову назву для підключень до одного сервера з різних IP (дім / школа).")
    location_tag = forms.CharField(label="Локація / Мережа", max_length=50, required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '🏠 Дім, 🏫 Кабінет інформатики, 💻 Ноутбук'}),
        help_text="Зручна мітка: де саме діє ця адреса.")
    is_default_for_group = forms.BooleanField(label="Основна (пріоритетна) адреса в цій групі", required=False)

    def __init__(self, *args, connection, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection = connection
        classes = connection.roster_cache.get('classes', []) if isinstance(connection.roster_cache, dict) else []
        subjects = connection.roster_cache.get('subjects', []) if isinstance(connection.roster_cache, dict) else []
        self.fields['source'].initial = connection.source
        self.fields['source_class_id'].choices = [('', '— Не вибрано —')] + [(str(c['id']), c['name']) for c in classes]
        self.fields['source_subject_id'].choices = [('', '— Не вибрано —')] + [(str(s['id']), s['name']) for s in subjects]
        self.fields['server_group'].initial = connection.server_group
        self.fields['location_tag'].initial = connection.location_tag
        self.fields['is_default_for_group'].initial = connection.is_default_for_group

    def clean_site_url(self):
        url = self.cleaned_data['site_url']
        try:
            grades_url, _ = source_urls(url)
            self.cleaned_data['grades_url'] = grades_url
        except Exception as e:
            raise forms.ValidationError(str(e))
        return url

    def clean(self):
        data = super().clean()
        key = data.get('api_key', '').strip()
        if key and any(c in key for c in ('\r', '\n')):
            self.add_error('api_key', 'Ключ не може містити переносів рядка.')
        return data

