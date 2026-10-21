"""Synthetic browser fixture; no working .env, databases or external APIs."""
import datetime as dt
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
from django.core.management import call_command
from django.test import Client
from apps.curriculum.models import Student, ClassEnrollment
from apps.journals.models import Grade, ExternalGrade, APIInboxResult, GradeLessonAssignment, GradeStudentMapping
from tests.test_grade_placement import GradePlacementTests
from apps.portal.views_grade_placement import PREVIEW_SESSION_KEY

work = tempfile.TemporaryDirectory(prefix='schooljournal-placement-browser-')
settings.DATABASES['default']['NAME'] = str(Path(work.name) / 'synthetic.sqlite3')
settings.DEBUG = True
call_command('migrate', verbosity=0)
fixture = GradePlacementTests()
fixture.client = Client()
fixture.setUp()
fixture.second_mapping()
third = Student.objects.create(school=fixture.school, first_name='Третій', last_name='Вигаданий')
ClassEnrollment.objects.create(student=third, student_class=fixture.group)
GradeStudentMapping.objects.create(connection=fixture.connection, external_id='43', student=third)
original = fixture.session_row(date=fixture.l1.date.isoformat(), title='Тест минулого тижня')
late = fixture.session_row(student_id=42, title='Тест минулого тижня', value='11')
other = fixture.session_row(student_id=42, session=71, title='Інша робота', work_id=99)
future = fixture.session_row(student_id=43, title='Тест минулого тижня', date='2026-10-16', value='10')
fixture_path = Path(os.environ.get('GRADE_PLACEMENT_FIXTURE', '/tmp/grade-placement-browser-fixture.json'))
fixture_path.touch(mode=0o600)
fixture_path.write_text(json.dumps({'url': 'http://127.0.0.1:19190', 'journal': fixture.journal.pk,
    'lesson': fixture.l1.pk, 'cookies': [{'name': settings.SESSION_COOKIE_NAME,
    'value': fixture.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': 'http://127.0.0.1:19190'}]}))
fetches = {'preview': 0, 'automatic': 0}


def reset():
    Grade.objects.all().delete()
    ExternalGrade.objects.all().delete()
    APIInboxResult.objects.all().delete()
    GradeLessonAssignment.objects.all().delete()
    fixture.pull(original)
    session = fixture.client.session
    session.pop(PREVIEW_SESSION_KEY, None)
    session.save()
    fetches.update(preview=0, automatic=0)


def fetch_preview(connection, start, end):
    fetches['preview'] += 1
    return [row for row in [original, late, other] if start <= dt.date.fromisoformat(row['date']) <= end]


def fetch_automatic(connection, start, end):
    fetches['automatic'] += 1
    return [future]


def state():
    return {'grades': list(Grade.objects.order_by('student_id').values('student_id', 'lesson__date', 'value')),
            'assignments': GradeLessonAssignment.objects.count(), 'fetches': fetches,
            'records': list(ExternalGrade.objects.values_list('external_id', flat=True))}


from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from wsgiref.simple_server import make_server, WSGIRequestHandler
app = StaticFilesHandler(get_wsgi_application())


def application(environ, start_response):
    path = environ['PATH_INFO']
    if path in ('/__synthetic__/reset', '/__synthetic__/state'):
        if path.endswith('reset'):
            reset()
        payload = json.dumps(state(), default=str).encode()
        start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(payload)))])
        return [payload]
    return app(environ, start_response)


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


reset()
with patch('apps.portal.views_grade_placement.fetch_grade_payload', side_effect=fetch_preview), \
        patch('apps.portal.views_integrations.fetch_grade_payload', side_effect=fetch_automatic):
    with make_server('127.0.0.1', 19190, application, handler_class=Quiet) as server:
        print('Synthetic lesson placement fixture ready on 127.0.0.1:19190', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            fixture_path.unlink(missing_ok=True)
            work.cleanup()
