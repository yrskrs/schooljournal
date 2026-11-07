"""Regression tests for concurrency, streaming, timeouts and persistent updater state."""
import json
import copy
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

import scripts.updater_service as updater
from scripts import image_retention as retention
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



# Scoped image history and post-verification cleanup regressions.
PROJECT = next(name for name, data in retention.PROJECTS.items()
               if data[0] == updater.REPO and name != 'school_test')
PREFIX = retention.PROJECTS[PROJECT][1][0]


def image(number, tags=None, source=None):
    return {'Id': 'sha256:' + f'{number:064x}', 'Created': f'2026-11-{number:02d}T12:00:00Z',
            'RepoTags': [f'{PREFIX}:v{number}'] if tags is None else tags,
            'Config': {'Labels': {'org.opencontainers.image.source': source or 'https://github.com/' + updater.REPO,
                                  'org.opencontainers.image.version': f'1.0.{number}'}}}


class FakeDocker:
    def __init__(self, images, referenced=()):
        self.inventory = copy.deepcopy(images)
        self.used = set(referenced)
        self.commands = []
        self.moved = {}
        self.fail_remove = False

    def images(self):
        return self.inventory

    def referenced_images(self):
        return set(self.used)

    def resolve(self, ref):
        if ref in self.moved:
            return self.moved[ref]
        for entry in self.inventory:
            if entry['Id'] == ref or ref in entry.get('RepoTags', []):
                return entry['Id']
        raise ValueError('missing reference')

    def run(self, *args):
        self.commands.append(args)
        if self.fail_remove:
            raise subprocess.CalledProcessError(1, ['docker', *args])
        return ''


class ImageRetentionTests(unittest.TestCase):
    def test_eight_versions_keep_latest_five_and_dry_run_never_deletes(self):
        images = [image(n) for n in range(1, 9)]
        docker = FakeDocker(images)
        result = retention.cleanup(PROJECT, docker=docker, log=lambda _: None)
        self.assertEqual(set(result['keep']), {image(n)['Id'] for n in range(4, 9)})
        self.assertEqual(len(result['remove']), 3)
        self.assertEqual(docker.commands, [])

    def test_current_and_old_rollback_count_inside_five(self):
        images = [image(n) for n in range(1, 9)]
        result = retention.plan(images, PROJECT, [image(1)['Id'], image(8)['Id']])
        self.assertEqual(set(result['keep']), {image(n)['Id'] for n in (1, 5, 6, 7, 8)})

    def test_aliases_are_one_version_and_foreign_alias_is_not_removed(self):
        old = image(1, [f'{PREFIX}:old', f'{PREFIX}:rollback', 'unrelated/shared:keep'])
        images = [old] + [image(n) for n in range(2, 8)]
        docker = FakeDocker(images)
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        removed = [args[-1] for args in docker.commands]
        self.assertIn(f'{PREFIX}:old', removed)
        self.assertIn(f'{PREFIX}:rollback', removed)
        self.assertNotIn('unrelated/shared:keep', removed)
        self.assertEqual(result['total'], 7)
        self.assertTrue(all(args[:3] == ('image', 'rm', '--no-prune') for args in docker.commands))

    def test_other_projects_and_unlabelled_dangling_layers_are_untouched(self):
        images = [image(n) for n in range(1, 8)]
        foreign = image(10, ['other-app:1'], 'https://github.com/other/repo')
        base = image(11, [], 'https://github.com/other/repo')
        runner = image(12, ['schoolwork2-python-runner:1'])
        docker = FakeDocker(images + [foreign, base, runner])
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertEqual(result['total'], 7)
        self.assertTrue(all(args[-1].startswith(PREFIX + ':') for args in docker.commands))

    def test_identifiable_dangling_old_app_build_can_be_removed(self):
        old = image(1, [])
        docker = FakeDocker([old] + [image(n) for n in range(2, 8)])
        retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertIn(('image', 'rm', '--no-prune', old['Id']), docker.commands)

    def test_images_used_by_running_or_stopped_containers_are_protected(self):
        docker = FakeDocker([image(n) for n in range(1, 8)], [image(1)['Id'], image(2)['Id']])
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertTrue({image(1)['Id'], image(2)['Id']}.issubset(result['keep']))
        self.assertNotIn(f'{PREFIX}:v1', [c[-1] for c in docker.commands])

    def test_more_than_five_required_images_never_forces_deletion(self):
        protected = {image(n)['Id'] for n in range(1, 7)}
        result = retention.plan([image(n) for n in range(1, 9)], PROJECT, protected)
        self.assertTrue(result['protected_overflow'])
        self.assertEqual(set(result['keep']), protected)

    def test_inventory_failure_deletes_nothing(self):
        docker = FakeDocker([])
        docker.images = Mock(side_effect=RuntimeError('daemon unavailable'))
        with self.assertRaises(RuntimeError):
            retention.cleanup(PROJECT, apply=True, docker=docker)
        self.assertEqual(docker.commands, [])

    def test_missing_rollback_reference_fails_closed(self):
        docker = FakeDocker([image(n) for n in range(1, 9)])
        with self.assertRaises(ValueError):
            retention.cleanup(PROJECT, protect=['missing:rollback'], apply=True, docker=docker)
        self.assertEqual(docker.commands, [])

    def test_stale_missing_record_does_not_block_cleanup_but_existing_rollback_is_kept(self):
        docker = FakeDocker([image(n) for n in range(1, 8)])
        result = retention.cleanup(PROJECT, rollback=['missing:old-record', image(1)['Id']],
                                   apply=True, docker=docker, log=lambda _: None)
        self.assertIn(image(1)['Id'], result['keep'])
        self.assertEqual(len(result['keep']), 5)
        self.assertNotIn(f'{PREFIX}:v1', [c[-1] for c in docker.commands])

    def test_tag_moved_after_inventory_is_not_removed(self):
        docker = FakeDocker([image(n) for n in range(1, 8)])
        docker.moved[f'{PREFIX}:v1'] = image(7)['Id']
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertIn(f'{PREFIX}:v1', result['skipped'])
        self.assertNotIn(f'{PREFIX}:v1', [c[-1] for c in docker.commands])

    def test_new_container_started_after_plan_is_not_removed(self):
        docker = FakeDocker([image(n) for n in range(1, 8)])
        docker.referenced_images = Mock(side_effect=[set(), {image(1)['Id']}, set()])
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertIn(image(1)['Id'], result['skipped'])
        self.assertNotIn(f'{PREFIX}:v1', [c[-1] for c in docker.commands])

    def test_delete_failure_does_not_escalate_to_force_or_prune(self):
        docker = FakeDocker([image(n) for n in range(1, 8)])
        docker.fail_remove = True
        result = retention.cleanup(PROJECT, apply=True, docker=docker, log=lambda _: None)
        self.assertEqual(len(result['errors']), 2)
        self.assertTrue(all(c[:3] == ('image', 'rm', '--no-prune') for c in docker.commands))

    def test_cleanup_failure_does_not_fail_a_verified_web_update(self):
        with patch.object(updater, 'run_command', side_effect=RuntimeError('Docker busy')), \
                patch.object(updater, 'append_log') as log:
            updater.cleanup_old_images(Mock(env={}), 'old-image', 'new-image')
        self.assertIn('очищення', log.call_args.args[0])

    def test_full_web_job_cleans_only_after_verified_success(self):
        for fail_verification in (False, True):
            with self.subTest(fail_verification=fail_verification), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'backups').mkdir()
                backup = root / 'fixture-backup'; backup.mkdir()
                for name in ('VERSION', 'RELEASES.json', 'CHANGELOG.md'):
                    (root / name).write_text('synthetic')
                deployment = Mock(env={}, config={'services': {}})
                deployment.query.return_value = 'synthetic-container'
                old_commit = 'a' * 40
                old_image = 'sha256:' + 'b' * 64
                events = []

                def capture(args, *rest, **kwargs):
                    if args[:3] == ['git', 'status', '--porcelain']:
                        return ''
                    if args[:2] == ['git', 'rev-parse']:
                        return old_commit
                    if args[:2] == ['git', 'cat-file']:
                        return ''
                    if len(args) == 2 and str(args[1]).endswith('/scripts/source_fingerprint.py'):
                        return 'd' * 64
                    if args[:2] == ['docker', 'inspect']:
                        return old_commit if 'org.opencontainers.image.revision' in args[3] else old_image
                    if args[:3] == ['docker', 'image', 'inspect']:
                        return old_image
                    raise AssertionError(args)

                def run(args, *rest, **kwargs):
                    if any(str(arg).endswith('/scripts/backup.sh') for arg in args):
                        return 'Резервна копія перевірена: ' + str(backup) if PROJECT == 'schoolwork2' else str(backup)
                    return ''

                def verify(*args):
                    events.append('verified')
                    if fail_verification:
                        raise RuntimeError('candidate health check failed')

                def state(**kwargs):
                    updater.CURRENT_STATE.update(kwargs)

                with patch.dict(updater.CURRENT_STATE, {'state': 'running', 'job_id': 'c' * 32, 'progress': 0, 'logs': []}, clear=True), \
                     patch.object(updater, 'ROOT', root), \
                     patch.object(updater, 'Deployment', return_value=deployment), \
                     patch.object(updater, 'capture', side_effect=capture), \
                     patch.object(updater, 'installed_health', return_value='9.0.0'), \
                     patch.object(updater, 'read_version', return_value='9.0.1'), \
                     patch.object(updater, 'run_command', side_effect=run), \
                     patch.object(updater, 'verify_deployment', side_effect=verify), \
                     patch.object(updater, 'cleanup_old_images', side_effect=lambda *a: events.append('cleanup')) as clean, \
                     patch.object(updater, 'perform_rollback'), \
                     patch.object(updater, 'append_log'), \
                     patch.object(updater, 'update_state', side_effect=state):
                    updater.execute_update_job('HEAD')
                    if fail_verification:
                        clean.assert_not_called()
                        self.assertEqual(updater.CURRENT_STATE['state'], 'error')
                    else:
                        self.assertEqual(events, ['verified', 'cleanup'], updater.CURRENT_STATE.get('error'))
                        self.assertEqual(updater.CURRENT_STATE['state'], 'success')

    def test_cli_reads_immediate_web_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / 'backups').mkdir()
            (root / 'backups/update_status.json').write_text('{"rollback":{"image":"old:1"}}')
            self.assertEqual(retention.rollback_references(root), ['old:1'])


if __name__ == '__main__':
    unittest.main()
