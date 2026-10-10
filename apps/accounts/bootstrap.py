from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import InitialSetupState, User


def global_admin_exists():
    # An inactive administrator also closes public setup; recovery is private.
    return User.objects.filter(Q(is_superuser=True) | Q(role=User.Role.SUPERADMIN)).exists()


def initial_setup_required():
    if not settings.INITIAL_SETUP_ENABLED:
        return False
    pending = InitialSetupState.objects.filter(pk=1, completed_at__isnull=True)
    if not pending.exists():
        return False
    if global_admin_exists():
        pending.update(completed_at=timezone.now())
        return False
    return True
