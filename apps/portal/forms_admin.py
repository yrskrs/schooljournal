from django import forms
from apps.schools.models import School


class SchoolEditForm(forms.ModelForm):
    class Meta:
        model = School
        fields = ('name', 'short_name', 'edrpou_code', 'address', 'phone', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean_edrpou_code(self):
        return self.cleaned_data.get('edrpou_code') or None
