from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import InitialSetupState, User


@receiver(post_save, sender=User, dispatch_uid='accounts.close_initial_setup')
def close_initial_setup(sender, instance, using, raw=False, **kwargs):
    if not raw and (instance.is_superuser or instance.role == User.Role.SUPERADMIN):
        InitialSetupState.objects.using(using).filter(
            pk=1, completed_at__isnull=True
        ).update(completed_at=timezone.now())
