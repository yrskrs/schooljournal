from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('journals', '0015_extrastudent_separate_journal_and_more')]
    operations = [
        migrations.AddField(model_name='extrastudent', name='is_separate', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='extrastudent', name='alias_of', field=models.ForeignKey(
            to='journals.extrastudent', on_delete=django.db.models.deletion.PROTECT,
            null=True, blank=True, related_name='aliases')),
    ]
