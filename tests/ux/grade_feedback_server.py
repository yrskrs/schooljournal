"""Local browser fixture with an isolated temporary database and mocked sources."""
import os, sys, json, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
import django
django.setup()
from django.conf import settings
temporary = tempfile.TemporaryDirectory(prefix='schooljournal-grade-feedback-')
settings.DATABASES['default']['NAME'] = str(Path(temporary.name) / 'synthetic.sqlite3')
settings.DEBUG = True
from django.core.management import call_command
call_command('migrate', verbosity=0)
from tests.test_grade_sync_feedback import GradeSyncFeedbackTests
from django.test import Client
fixture = GradeSyncFeedbackTests()
fixture.client = Client()
fixture.setUp()
fixture.connection(mappings=True)
work = fixture.connection(mappings=True)
work.source = 'schoolwork'
work.name = 'Синтетичні завдання'
work.roster_cache = {**work.roster_cache, 'source': 'schoolwork'}
work.save()
fixture_path = Path(os.environ.get('GRADE_FEEDBACK_FIXTURE', '/tmp/grade-feedback-browser-fixture.json'))
fixture_path.touch(mode=0o600)
fixture_path.write_text(json.dumps({'url':f'http://127.0.0.1:19187/journal/{fixture.journal.pk}/','cookies':[{'name':settings.SESSION_COOKIE_NAME,'value':fixture.client.cookies[settings.SESSION_COOKIE_NAME].value,'url':'http://127.0.0.1:19187'}]}))
from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from wsgiref.simple_server import make_server, WSGIRequestHandler
class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass
report = {'counts':dict(created=0, updated=0, ignored=0, needs_review=1, conflicts=2)}
with patch('apps.portal.views_integrations.send_journal_roster', return_value=report), patch('apps.portal.views_integrations.fetch_grade_payload', return_value=[]), patch('apps.portal.views_connection_setup.send_journal_roster', return_value=report):
    with make_server('127.0.0.1', 19187, StaticFilesHandler(get_wsgi_application()), handler_class=Quiet) as server:
        print('Synthetic browser fixture ready', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            fixture_path.unlink(missing_ok=True)
            temporary.cleanup()
