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
    site_url = forms.URLField(label='Адреса сайту тестів або завдань (IP і порт)', max_length=450, required=False,
        help_text='Вставте повну адресу, за якою відкриваєте джерело, наприклад http://192.168.10.25:8000. Вона має бути доступна серверу журналу.')
    api_key = forms.CharField(label='Ключ API', required=False, max_length=500,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

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

    def clean(self):
        data = super().clean()
        selected = [value for key, value in data.items() if key.startswith('pupil_') and value and value != 'new']
        if len(selected) != len(set(selected)):
            raise forms.ValidationError('Різних учнів джерела не можна пов’язати з одним учнем журналу.')
        return data
