"""Disposable notification UI; synthetic SQLite, users and mocked sources only."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
import django
django.setup()
from django.conf import settings
work = tempfile.TemporaryDirectory(prefix='notification-browser-')
settings.DATABASES['default']['NAME'] = str(Path(work.name) / 'checks.sqlite3')
settings.DEBUG = True
settings.ALLOWED_HOSTS = ['journal', 'localhost', '127.0.0.1']
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import GradePullLog
from tests.test_api_workspace import APIWorkspaceTests

call_command('migrate', verbosity=0)
fixture = APIWorkspaceTests(); fixture.client = Client(); fixture.setUp()
connection = fixture.connection(mappings=True)
target = Student.objects.create(school=fixture.school, first_name='Учень', last_name='Перевірка')
ClassEnrollment.objects.create(student=target, student_class=fixture.group, start_date=fixture.year.start_date)
connection.roster_sent_at = timezone.now()
connection.roster_report = {'schema_version': 2, 'source': connection.source,
    'instance_id': connection.roster_cache['instance_id'], 'class_id': connection.source_class_id,
    'counts': {'needs_review': 1, 'conflicts': 0},
    'needs_review': [{'journal_id': 'synthetic-person', 'name': target.get_full_name_ukr(),
                      'candidates': [{'id': 99, 'name': 'Кандидат джерела'}]}]}
connection.save()
grade = fixture.log(connection, errors=1)
grade.errors = [{'row': 27, 'message': 'Синтетична причина оцінки. synthetic-key'}]; grade.save()
roster = fixture.log(connection, 'roster', errors=1)
roster.errors = [{'message': 'Синтетична причина передачі списку.'}]; roster.save()
for _ in range(31): fixture.log(connection, 'connection')
artifact = Path('/artifacts/journal-fixture.json')
artifact.touch(mode=0o600)
artifact.write_text(json.dumps({'base': 'http://journal:8000', 'journal': fixture.journal.pk,
    'connection': connection.pk, 'grade_log': grade.pk, 'roster_log': roster.pk, 'target': target.pk,
    'cookie': {'name': settings.SESSION_COOKIE_NAME,
               'value': fixture.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': 'http://journal:8000'}}))


def send_roster(connection, user, resolutions=None):
    connection.roster_report = {'counts': {'created': 0, 'updated': 0, 'needs_review': 0, 'conflicts': 0}}
    connection.roster_sent_at = timezone.now()
    connection.save(update_fields=['roster_report', 'roster_sent_at'])
    GradePullLog.objects.create(connection=connection, user=user, counts={'operation': 'roster', 'errors': 0})
    return connection.roster_report


from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from wsgiref.simple_server import make_server, WSGIRequestHandler


class Quiet(WSGIRequestHandler):
    def log_message(self, *args): pass


with patch('apps.portal.views_connection_setup.send_journal_roster', side_effect=send_roster):
    with make_server('0.0.0.0', 8000, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet) as server:
        print('Synthetic notification UI ready', flush=True)
        server.serve_forever()
