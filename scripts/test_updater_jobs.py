"""Regression tests for concurrency, streaming, timeouts and persistent updater state."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import scripts.updater_service as updater
from scripts.update_metadata import check_remote, parse_changelog_markdown


class UpdaterJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root_patch = patch.object(updater, 'ROOT', self.root)
        self.status_patch = patch.object(updater, 'STATUS_FILE', self.root / 'backups/status.json')
        self.lock_patch = patch.object(updater, 'LOCK_FILE', self.root / '.maintenance.lock')
        self.root_patch.start(); self.status_patch.start(); self.lock_patch.start()
        updater.CURRENT_STATE.clear()
        updater.CURRENT_STATE.update(state='idle', progress=0, logs=[])

    def tearDown(self):
        self.root_patch.stop(); self.status_patch.stop(); self.lock_patch.stop()
        self.directory.cleanup()

    def test_image_selection_preserves_environment_owner_and_permissions(self):
        environment = self.root / '.env'
        environment.write_text('DUMMY=keep-this-value\n')
        environment.chmod(0o600)
        owner = environment.stat()
        deployment = updater.Deployment.__new__(updater.Deployment)
        deployment.config = {'services': {service: {} for service in updater.WRITERS}}
        deployment.path = self.root / 'compose.json'
        deployment.select_image('synthetic/app:9.2.0')
        updated = environment.stat()
        self.assertEqual((updated.st_uid, updated.st_gid), (owner.st_uid, owner.st_gid))
        self.assertEqual(updated.st_mode & 0o777, 0o600)
        self.assertIn('DUMMY=keep-this-value', environment.read_text())
        self.assertIn('synthetic/app:9.2.0', environment.read_text())
        self.assertFalse(environment.with_suffix('.image-update').exists())

    def test_refresh_adopts_new_services_without_losing_rollback_config(self):
        from unittest.mock import Mock
        import copy
        deployment = updater.Deployment.__new__(updater.Deployment)
        old = {'services': {'app': {'environment': {'OLD': '1'}}}, 'volumes': {'real': {}}}
        deployment.config = copy.deepcopy(old)
        deployment.original_config = copy.deepcopy(old)
        deployment.path = self.root / 'compose.json'
        replacement = Mock()
        replacement.config = {'services': {'app': {'environment': {'NEW': '1'}}, 'code_runner': {}},
                              'volumes': {'real': {}}}
        with patch.object(updater, 'Deployment', return_value=replacement):
            deployment.refresh()
        self.assertIn('code_runner', deployment.config['services'])
        self.assertEqual(deployment.config['services']['app']['environment'], {'NEW': '1'})
        self.assertEqual(json.loads(deployment.path.read_text()), replacement.config)
        replacement.close.assert_called_once()
        self.assertEqual(deployment.original_config, old)
        deployment.restore_config()
        self.assertEqual(deployment.config, old)
        self.assertEqual(json.loads(deployment.path.read_text()), old)

    def test_output_is_visible_before_command_finishes(self):
        finished = threading.Event()
        def run():
            updater.run_command([sys.executable, '-u', '-c', "import time; print('snapshot started'); time.sleep(.5); print('snapshot done')"])
            finished.set()
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not updater.CURRENT_STATE['logs'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(finished.is_set())
        self.assertIn('snapshot started', updater.CURRENT_STATE['logs'][0])
        thread.join(3)
        self.assertTrue(finished.is_set())
        self.assertIn('snapshot done', updater.CURRENT_STATE['logs'][-1])

    def test_timeout_stops_silent_command(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            updater.run_command([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=.1)
        self.assertLess(time.monotonic() - started, 3)

    def test_failed_command_retains_actual_error(self):
        with self.assertRaisesRegex(RuntimeError, 'specific failure'):
            updater.run_command([sys.executable, '-c', "import sys; print('specific failure',file=sys.stderr); sys.exit(7)"])
        self.assertIn('specific failure', updater.CURRENT_STATE['logs'][-1])

    def test_start_reserves_state_before_response_and_rejects_duplicate(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def job():
            entered.set(); release.wait(3); updater.update_state(state='success', progress=100); completed.set()
        first = updater.start_job(job, ())
        self.assertTrue(first)
        self.assertEqual(updater.CURRENT_STATE['state'], 'running')
        self.assertEqual(updater.CURRENT_STATE['progress'], 5)
        self.assertEqual(json.loads(updater.STATUS_FILE.read_text())['job_id'], first)
        self.assertIsNone(updater.start_job(job, ()))
        release.set(); self.assertTrue(completed.wait(3))

    def test_exception_always_reaches_terminal_error(self):
        completed = threading.Event()
        def fail():
            completed.set(); raise RuntimeError('job exploded')
        updater.start_job(fail, ())
        self.assertTrue(completed.wait(3))
        deadline = time.monotonic() + 3
        while updater.CURRENT_STATE['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(updater.CURRENT_STATE['state'], 'error')
        self.assertEqual(updater.CURRENT_STATE['error'], 'job exploded')

    def test_finished_status_survives_restart(self):
        updater.update_state(state='success', progress=100, logs=['done'])
        updater.CURRENT_STATE.update(state='idle', progress=0, logs=[])
        updater.load_status()
        self.assertEqual(updater.CURRENT_STATE['state'], 'success')
        self.assertEqual(updater.CURRENT_STATE['logs'], ['done'])

    def test_interrupted_job_is_reported_as_error(self):
        updater.update_state(state='running', progress=75)
        updater.load_status()
        self.assertEqual(updater.CURRENT_STATE['state'], 'error')
        self.assertIn('перезапущено', updater.CURRENT_STATE['error'])

    def test_github_outage_is_not_reported_as_up_to_date(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            result = check_remote(self.root, 'example/repo', '1.2.3')
        self.assertFalse(result['ok'])
        self.assertIn('offline', result['error'])

    def test_changelog_supports_all_project_formats(self):
        releases = parse_changelog_markdown('## [4.4.6] - 2026-10-12 - Title\n* Work\n## 1.5.8 — 2026-10-12\n- Test')
        self.assertEqual(releases[0]['date'], '2026-10-12')
        self.assertEqual(releases[0]['changes'], ['Work'])
        self.assertEqual(releases[1]['changes'], ['Test'])


if __name__ == '__main__':
    unittest.main()
