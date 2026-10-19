"""Temporary browser fixture; no working database, secrets, or remote APIs."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
import django
django.setup()
from django.conf import settings
temporary = tempfile.TemporaryDirectory(prefix='schooljournal-api-workspace-')
settings.DATABASES['default']['NAME'] = str(Path(temporary.name) / 'synthetic.sqlite3')
settings.DEBUG = True
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from apps.journals.models import GradePullLog
from tests.test_api_workspace import APIWorkspaceTests
call_command('migrate', verbosity=0)
fixture = APIWorkspaceTests()
fixture.client = Client()
fixture.setUp()
first = fixture.connection(mappings=True)
first.name = 'Тести школи'
first.is_default_for_group = True
first.roster_sent_at = timezone.now()
first.roster_report = {'counts': {'conflicts': 1, 'needs_review': 0}}
first.save()
backup = fixture.connection(mappings=True)
backup.url = 'http://10.44.1.20:8000/api/v2/journal/grades/'
backup.location_tag = 'Школа'
backup.save()
fixture.log(first, 'connection', errors=1)
fixture_path = Path(os.environ.get('API_WORKSPACE_FIXTURE', '/tmp/api-workspace-browser-fixture.json'))
fixture_path.touch(mode=0o600)
fixture_path.write_text(json.dumps({'url': 'http://127.0.0.1:19189', 'journal': fixture.journal.pk,
    'cookies': [{'name': settings.SESSION_COOKIE_NAME,
                 'value': fixture.client.cookies[settings.SESSION_COOKIE_NAME].value,
                 'url': 'http://127.0.0.1:19189'}]}))


def send_roster(connection, user, resolutions=None):
    report = {'counts': {'created': 0, 'updated': 0, 'ignored': 0, 'needs_review': 0, 'conflicts': 0}}
    connection.roster_report = report
    connection.roster_sent_at = timezone.now()
    connection.save(update_fields=['roster_report', 'roster_sent_at'])
    GradePullLog.objects.create(connection=connection, user=user, counts={'operation': 'roster', 'errors': 0})
    return report


from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from wsgiref.simple_server import make_server, WSGIRequestHandler


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


with patch('apps.portal.views_connection_setup.fetch_source_roster', return_value=first.roster_cache), \
        patch('apps.portal.views_connection_setup.send_journal_roster', side_effect=send_roster):
    with make_server('127.0.0.1', 19189, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet) as server:
        print('API workspace synthetic fixture ready on 127.0.0.1:19189', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            fixture_path.unlink(missing_ok=True)
            temporary.cleanup()
