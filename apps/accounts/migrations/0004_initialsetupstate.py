from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def initialize_setup(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    State = apps.get_model('accounts', 'InitialSetupState')
    database = schema_editor.connection.alias
    existing_admin = User.objects.using(database).filter(
        Q(is_superuser=True) | Q(role='SUPERADMIN')
    ).exists()
    State.objects.using(database).create(
        pk=1, completed_at=timezone.now() if existing_admin else None
    )


class Migration(migrations.Migration):
    dependencies = [('accounts', '0003_alter_user_role')]

    operations = [
        migrations.CreateModel(
            name='InitialSetupState',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.RunPython(initialize_setup, migrations.RunPython.noop),
    ]
