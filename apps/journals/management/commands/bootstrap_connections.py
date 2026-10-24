"""Idempotent, database-only setup after migrations; never probes or syncs pupils."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from apps.journals.models import ServiceConnection
from apps.journals.services.network_profiles import ensure_automatic_profiles


class Command(BaseCommand):
    help = 'Підготувати автоматичні профілі адрес без передачі даних.'

    def handle(self, *args, **options):
        owners = ServiceConnection.objects.values_list('owner_id', flat=True).distinct()
        count = 0
        for owner in get_user_model().objects.filter(pk__in=owners):
            ensure_automatic_profiles(owner)
            count += 1
        self.stdout.write(f'Автоматичні профілі підготовлено для власників: {count}. Дані учнів не змінювалися.')
