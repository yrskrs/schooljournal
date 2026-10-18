from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('journals', '0016_extrastudent_standalone')]

    operations = [
        migrations.AddField(
            model_name='gradeapiconnection',
            name='server_group',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Вкажіть однакову назву для підключень до одного сервера з різних IP (дім / школа).',
                max_length=100,
                verbose_name='Група серверів / Об’єднання IP'
            ),
        ),
        migrations.AddField(
            model_name='gradeapiconnection',
            name='location_tag',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Наприклад: 🏠 Дім, 🏫 Кабінет інформатики, 💻 Ноутбук',
                max_length=50,
                verbose_name='Локація / Мережа'
            ),
        ),
        migrations.AddField(
            model_name='gradeapiconnection',
            name='is_default_for_group',
            field=models.BooleanField(
                default=False,
                verbose_name='Основна адреса в групі'
            ),
        ),
    ]
