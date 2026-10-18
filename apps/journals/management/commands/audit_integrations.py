import json

from django.core.management.base import BaseCommand

from apps.journals.models import Journal
from apps.journals.services.integration_diagnostics import build_diagnostic_report


class Command(BaseCommand):
    help = 'Read-only anonymized audit of every local class, pupil and integration; no network requests.'

    def handle(self, *args, **options):
        report = build_diagnostic_report(Journal.objects.select_related('student_class', 'subject').order_by('pk'), all_records=True)
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
