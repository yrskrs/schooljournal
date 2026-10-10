from django.db import migrations
from django.db.models import F, Q


def confirm_sync_status(apps, schema_editor):
    # Only confirmed online snapshots can be shown with an online marker.
    Grade = apps.get_model('journals', 'Grade')
    Attendance = apps.get_model('journals', 'Attendance')
    Lesson = apps.get_model('journals', 'Lesson')
    db = schema_editor.connection.alias
    Grade.objects.using(db).filter(is_synced_to_nz=True).filter(
        Q(lesson__nz_schedule_id='') | ~Q(value=F('nz_last_synced_value'))
    ).update(is_synced_to_nz=False)
    Attendance.objects.using(db).filter(is_synced_to_nz=True).filter(
        Q(lesson__nz_schedule_id='') | ~Q(status=F('nz_last_synced_status'))
    ).update(is_synced_to_nz=False)
    Lesson.objects.using(db).filter(is_synced_to_nz=True).filter(
        Q(nz_schedule_id='') | ~Q(topic=F('nz_last_synced_topic')) | ~Q(homework=F('nz_last_synced_homework'))
    ).update(is_synced_to_nz=False)


class Migration(migrations.Migration):
    dependencies = [('journals', '0003_lesson_homework_due_lesson_and_more')]
    operations = [migrations.RunPython(confirm_sync_status, migrations.RunPython.noop)]
