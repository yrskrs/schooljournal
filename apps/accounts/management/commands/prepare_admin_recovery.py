from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.bootstrap import setup_recovery_user
from apps.accounts.models import InitialSetupState, User


class Command(BaseCommand):
    help = 'Дозволити одноразове відновлення адміністратора через браузер без видалення даних.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)

    def handle(self, *args, **options):
        if not settings.INITIAL_SETUP_ENABLED or len(settings.INITIAL_SETUP_TOKEN) < 32:
            raise CommandError('Увімкніть INITIAL_SETUP_ENABLED та задайте новий INITIAL_SETUP_TOKEN (32+ символи).')
        with transaction.atomic():
            user = setup_recovery_user()
            if user is None or user.username != options['username']:
                raise CommandError('INITIAL_SETUP_RECOVERY_USER_ID має вказувати на вибраного суперадміністратора.')
            state = InitialSetupState.objects.select_for_update().get(pk=1)
            # An old/demo password and existing sessions cannot bypass recovery.
            User.objects.filter(pk=user.pk).update(is_active=False)
            from apps.journals.models import AuditLog
            AuditLog.objects.create(user=user, action=AuditLog.Action.UPDATE,
                target_model='User', target_id=str(user.pk),
                description='На сервері дозволено одноразове відновлення адміністратора; старий вхід тимчасово вимкнено.')
            state.completed_at = None
            state.save(update_fields=['completed_at'])
        self.stdout.write(self.style.SUCCESS('Відкрийте сторінку входу та введіть серверний код налаштування. Дані збережено.'))
