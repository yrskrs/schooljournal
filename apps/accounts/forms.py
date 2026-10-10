from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from apps.schools.models import School
from .models import User

class CustomLoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Логін користувача',
        }),
        label="Логін"
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Пароль',
        }),
        label="Пароль"
    )


class SchoolRegistrationForm(UserCreationForm):
    school_name = forms.CharField(label='Повна назва школи', max_length=255)
    school_short_name = forms.CharField(label='Коротка назва', max_length=100, required=False)
    edrpou_code = forms.RegexField(label='ЄДРПОУ (за наявності)', regex=r'^\d{8}$', max_length=8, required=False,
                                 error_messages={'invalid': 'Код ЄДРПОУ має містити 8 цифр.'})
    email = forms.EmailField(label='Email адміністратора')
    first_name = forms.CharField(label="Ім’я адміністратора", max_length=150)
    last_name = forms.CharField(label='Прізвище адміністратора', max_length=150)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('school_name', 'school_short_name', 'edrpou_code', 'last_name', 'first_name', 'email', 'username')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'
            field.widget.attrs.pop('autofocus', None)

    def clean_edrpou_code(self):
        code = self.cleaned_data.get('edrpou_code') or None
        if code and School.objects.filter(edrpou_code=code).exists():
            raise forms.ValidationError('Школу з таким ЄДРПОУ вже зареєстровано. Зверніться до її адміністратора.')
        return code
