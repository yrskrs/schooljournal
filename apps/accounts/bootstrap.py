from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import InitialSetupState, User


def global_admin_exists():
    # An inactive administrator also closes public setup; recovery is private.
    return User.objects.filter(Q(is_superuser=True) | Q(role=User.Role.SUPERADMIN)).exists()


def setup_recovery_user():
    user_id = getattr(settings, 'INITIAL_SETUP_RECOVERY_USER_ID', 0)
    if not user_id:
        return None
    return User.objects.filter(pk=user_id).filter(
        Q(is_superuser=True) | Q(role=User.Role.SUPERADMIN)
    ).first()


def initial_setup_required():
    if not settings.INITIAL_SETUP_ENABLED:
        return False
    pending = InitialSetupState.objects.filter(pk=1, completed_at__isnull=True)
    if not pending.exists():
        return False
    if global_admin_exists():
        if setup_recovery_user() is not None:
            return True
        pending.update(completed_at=timezone.now())
        return False
    return True
