"""Check Internet-fetch routing and safe image selection using disposable files."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import updater_service

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fetch_update', SCRIPTS / 'fetch_update.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class UpdateHelpers(unittest.TestCase):
    def test_github_ssh_uses_same_repository_via_https(self):
        for remote in ('git@github.com:owner/school_test.git', 'git@github.com:owner/school_test', 'ssh://git@github.com/owner/school_test.git'):
            self.assertEqual(fetch.github_https(remote), 'https://github.com/owner/school_test.git')
        for remote in ('https://github.com/owner/other.git', 'git@other.example:owner/other.git', '/tmp/synthetic.git'):
            self.assertEqual(fetch.github_https(remote), remote)

    def test_fetch_updates_origin_tracking_without_rewriting_origin(self):
        with patch.object(sys, 'argv', ['fetch_update', '--branch', 'main']), patch.object(fetch.subprocess, 'check_output', return_value='git@github.com:owner/school_test.git\n'), patch.object(fetch.subprocess, 'run') as run:
            fetch.main()
        command = run.call_args_list[-1].args[0]
        self.assertEqual(command, ['git', 'fetch', '--no-tags', 'https://github.com/owner/school_test.git', 'refs/heads/main:refs/remotes/origin/main'])
        self.assertEqual(run.call_args_list[-1].kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertNotIn('--force', command)

    def test_fetch_failure_stops_update(self):
        with patch.object(sys, 'argv', ['fetch_update']), patch.object(fetch.subprocess, 'check_output', return_value='https://github.com/owner/test.git'), patch.object(fetch.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['git', 'fetch'])):
            with self.assertRaisesRegex(SystemExit, 'робочі дані не змінено'):
                fetch.main()

    @unittest.skipUnless((SCRIPTS / 'select_release_image.py').exists(), 'Journal uses candidate images instead of .env image tags')
    def test_image_update_preserves_config_and_never_evaluates_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app').mkdir()
            (root / 'app/version.py').write_text('__version__ = "9.8.7"\n')
            environment = 'DUMMY=$(touch forbidden)\nSCHOOLTEST_IMAGE="registry.example:5000/custom/app:1.0.0"\n'
            (root / '.env').write_text(environment)
            result = subprocess.run([sys.executable, str(SCRIPTS / 'select_release_image.py'), 'schooltest5'], cwd=root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'registry.example:5000/custom/app:9.8.7')
            self.assertIn('DUMMY=$(touch forbidden)', (root / '.env').read_text())
            self.assertEqual((root / '.env.before-image-update').read_text(), environment)
            self.assertFalse((root / 'forbidden').exists())
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)
            pinned = 'SCHOOLTEST_IMAGE=custom/app@sha256:synthetic\n'
            (root / '.env').write_text(pinned)
            result = subprocess.run([sys.executable, str(SCRIPTS / 'select_release_image.py'), 'schooltest5'], cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / '.env').read_text(), pinned)


class ReleaseMetadataPermissions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='synthetic-release-permissions-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = [self.root / name for name in ('VERSION', 'RELEASES.json', 'CHANGELOG.md')]
        for path in self.paths:
            path.write_text('synthetic-public-metadata')
            path.chmod(0o600)
        self.secret = self.root / '.env'
        self.secret.write_text('SYNTHETIC_SECRET=keep-private\n')
        self.secret.chmod(0o600)
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        self.environment = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                            'JOURNAL_ROOT': str(self.root), 'ENV_FILE': str(self.secret)}

    def cli(self):
        return subprocess.run(['bash', '-c', 'source "$1"; make_release_metadata_readable',
                               'test', str(SCRIPTS / 'common.sh')],
                              env=self.environment, capture_output=True, text=True, timeout=5)

    def test_cli_and_web_updaters_make_only_public_metadata_readable(self):
        for action in (lambda: updater_service.make_release_metadata_readable(self.root), self.cli):
            for path in self.paths:
                path.chmod(0o600)
            result = action()
            if result is not None:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o644 for path in self.paths))
            self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.secret.read_text(), 'SYNTHETIC_SECRET=keep-private\n')

    def test_symlink_to_private_file_is_rejected_without_changing_any_permissions(self):
        self.paths[-1].unlink()
        self.paths[-1].symlink_to(self.secret)
        with self.assertRaises(ValueError):
            updater_service.make_release_metadata_readable(self.root)
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths[0].stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
