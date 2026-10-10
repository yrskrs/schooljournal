from django.db import migrations


def preserve_existing_accounts(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    Journal = apps.get_model('journals', 'Journal')
    NZAccount = apps.get_model('journals', 'NZAccount')
    database = schema_editor.connection.alias
    for account in NZAccount.objects.using(database).all().iterator():
        Journal.objects.using(database).filter(teacher_id=account.user_id, nz_owner__isnull=True).exclude(
            nz_journal_id='').update(nz_owner_id=account.pk)
        user = User.objects.using(database).select_related('school').get(pk=account.user_id)
        if user.school and user.school.name == 'Особистий простір журналів nz.ua' and user.password.startswith('!'):
            User.objects.using(database).filter(pk=user.pk).update(is_nz_personal=True)


class Migration(migrations.Migration):
    dependencies = [('journals', '0007_grade_is_approved_journal_local_profile_and_more'),
        ('accounts', '0002_user_is_nz_personal')]
    operations = [migrations.RunPython(preserve_existing_accounts, migrations.RunPython.noop)]
