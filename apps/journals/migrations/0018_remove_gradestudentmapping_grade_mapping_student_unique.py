from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('journals', '0017_gradeapiconnection_server_group_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='gradestudentmapping',
            name='grade_mapping_student_unique',
        ),
    ]
