"""Central settings only; journal pages never request a source address or key."""
import json
from urllib.parse import urlsplit, urlunsplit

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from apps.journals.models import NetworkProfile, ServiceEndpoint
from apps.journals.connection_models import SOURCE_CHOICES
from apps.journals.services.connection_setup import source_urls


class StyledForm:
    def style_fields(self):
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.CheckboxInput, forms.HiddenInput)):
                field.widget.attrs['class'] = 'form-control'


class ServiceSetupForm(StyledForm, forms.Form):
    source = forms.ChoiceField(label='Який сайт підключаємо', choices=SOURCE_CHOICES)
    profile = forms.ModelChoiceField(label='Вже налаштована мережа', queryset=NetworkProfile.objects.none(),
        required=False, empty_label='Вказати нову адресу сервера')
    address = forms.CharField(label='Адреса, IP або hostname сервера', required=False, max_length=450,
        help_text='Наприклад, 192.168.1.13 або schoolserver.local. Повну адресу можна скопіювати на сайті джерела.')
    api_key = forms.CharField(label='Ключ API', max_length=500,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='На сайті джерела відкрийте «Підключення до журналу» й створіть ключ. Вставте сюди лише ключ.')
    name = forms.CharField(label='Власна назва підключення', required=False, max_length=100)
    profile_name = forms.CharField(label='Назва нової мережі', required=False, max_length=100,
        help_text='Дім, Школа або власна назва. Для другого сервісу достатньо обрати цю мережу.')

    def __init__(self, *args, owner, journal_host=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner, self.journal_host = owner, journal_host
        self.fields['profile'].queryset = NetworkProfile.objects.filter(owner=owner, enabled=True)
        self.style_fields()

    def clean(self):
        data = super().clean()
        source, key, address = data.get('source'), data.get('api_key', '').strip(), data.get('address', '').strip()
        if key.startswith('{'):
            try:
                old = json.loads(key)
                source, key = old['source'], old['api_key'].strip()
                address = address or old['site_url'].strip()
                if source not in dict(SOURCE_CHOICES) or not isinstance(key, str): raise ValueError
                data['source'] = source
            except (ValueError, TypeError, KeyError, AttributeError):
                self.add_error('api_key', 'Вставте ключ API з сайту джерела, без додаткового тексту.')
                return data
        if key.lower().startswith('bearer '): key = key[7:].strip()
        if not key or any(c in key for c in '\r\n'):
            self.add_error('api_key', 'Потрібен ключ API без переносів рядка.')
        if source not in dict(SOURCE_CHOICES): return data
        profile = data.get('profile')
        port = getattr(profile, source + '_port') if profile else getattr(settings, 'LOCAL_' + source.upper() + '_PORT')
        scheme = profile.scheme if profile else 'http'
        if profile and not address:
            host = self.journal_host if profile.use_journal_host else profile.host
            host = f'[{host}]' if host and ':' in host else host
            address = f'{scheme}://{host}:{port}'
        if not address:
            self.add_error('address', 'Вкажіть хост сервера або оберіть налаштовану мережу.')
            return data
        try:
            bare_host = '://' not in address
            if bare_host: address = scheme + '://' + address
            parsed = urlsplit(address)
            if parsed.port is None and bare_host:
                host = f'[{parsed.hostname}]' if ':' in (parsed.hostname or '') else parsed.hostname
                parsed = parsed._replace(netloc=f'{host}:{port}')
                address = urlunsplit(parsed)
            grades_url, _ = source_urls(address)
            parsed = urlsplit(grades_url)
            expected_host = self.journal_host if profile and profile.use_journal_host else profile.host if profile else None
            if expected_host and parsed.hostname.casefold() != expected_host.casefold():
                self.add_error('profile', 'Це інший хост. Оберіть нову мережу, щоб зберегти попередню адресу.')
            data.update(site_url=grades_url[:-len('/api/v2/journal/grades/')],
                grades_url=grades_url, parsed_address=parsed, api_key=key)
        except (ValidationError, ValueError, AttributeError):
            self.add_error('address', 'Вкажіть мережеву адресу сайту без ключа, параметрів і службових адрес.')
        if key and key in data.get('name', ''):
            self.add_error('name', 'Ключ вводиться лише в поле «Ключ API». Тут достатньо короткої назви сайту.')
        return data


class NetworkProfileForm(StyledForm, forms.ModelForm):
    class Meta:
        model = NetworkProfile
        fields = ['name', 'host', 'scheme', 'schooltest_port', 'schoolwork_port', 'enabled']
        labels = {'name': 'Назва мережі', 'host': 'Хост сервера', 'scheme': 'Протокол',
            'schooltest_port': 'Порт SchoolTest', 'schoolwork_port': 'Порт SchoolNet', 'enabled': 'Мережа увімкнена'}
        help_texts = {'host': 'Один IP або hostname для обох сервісів. Порти налаштовуються в «Додатково».'}

    def __init__(self, *args, owner, **kwargs):
        kwargs.setdefault('instance', NetworkProfile(owner=owner,
            schooltest_port=settings.LOCAL_SCHOOLTEST_PORT, schoolwork_port=settings.LOCAL_SCHOOLWORK_PORT))
        super().__init__(*args, **kwargs)
        self.owner = owner
        if self.instance.use_journal_host:
            self.fields['host'].disabled = True
            self.fields['host'].help_text = 'Хост береться з поточної адреси, за якою ви відкрили журнал.'
        self.style_fields()

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if NetworkProfile.objects.filter(owner=self.owner, name=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Така назва мережі вже є. Оберіть іншу.')
        return name


class ServiceEditForm(StyledForm, forms.Form):
    name = forms.CharField(label='Назва підключення', max_length=100)
    api_key = forms.CharField(label='Новий ключ API', max_length=500, required=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='Залиште порожнім, щоб зберегти чинний ключ. Новий ключ перевіряється перед заміною.')
    enabled = forms.BooleanField(label='Підключення увімкнене', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.style_fields()

    def clean_api_key(self):
        key = self.cleaned_data['api_key'].strip()
        if any(c in key for c in '\r\n'): raise forms.ValidationError('Ключ не може містити переносів рядка.')
        return key


class EndpointForm(StyledForm, forms.ModelForm):
    class Meta:
        model = ServiceEndpoint
        fields = ['profile', 'port_override', 'path_prefix', 'enabled']
        labels = {'profile': 'Мережа', 'port_override': 'Окремий порт цього сервісу',
            'path_prefix': 'Префікс шляху сайту', 'enabled': 'Адреса увімкнена'}

    def __init__(self, *args, service, **kwargs):
        kwargs.setdefault('instance', ServiceEndpoint(connection=service))
        super().__init__(*args, **kwargs)
        self.fields['profile'].queryset = NetworkProfile.objects.filter(owner_id=service.owner_id)
        self.style_fields()
