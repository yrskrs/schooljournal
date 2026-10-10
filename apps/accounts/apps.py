from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    verbose_name = 'Користувачі та безпека'

    def ready(self):
        from . import signals  # noqa: F401
