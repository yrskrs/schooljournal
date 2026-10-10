from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('journals', '0010_rosterreplica_rosterstate')]
    operations = [
        migrations.AddField(model_name='gradeapiconnection', name='roster_cache', field=models.JSONField(default=dict, db_default={}, blank=True)),
        migrations.AddField(model_name='gradeapiconnection', name='roster_checked_at', field=models.DateTimeField(null=True, blank=True)),
    ]
