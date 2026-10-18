"""Cross-process CLI/updater exclusion with read-only, synthetic lock files."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import updater_service as updater


class MaintenanceLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='synthetic-maintenance-lock-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lock = self.root / '.maintenance.lock'
        self.patch = patch.object(updater, 'LOCK_FILE', self.lock)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_existing_readonly_file_is_not_truncated_or_replaced(self):
        self.lock.write_text('synthetic marker')
        self.lock.chmod(0o444)
        inode = self.lock.stat().st_ino
        with updater.open_maintenance_lock() as lock:
            self.assertFalse(lock.writable())
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.lock.stat().st_ino, inode)
            self.assertEqual(self.lock.read_text(), 'synthetic marker')

    def test_new_lock_is_shared_even_with_private_umask(self):
        previous = os.umask(0o077)
        try:
            with updater.open_maintenance_lock():
                self.assertEqual(self.lock.stat().st_mode & 0o777, 0o644)
        finally:
            os.umask(previous)

    def test_cli_and_updater_use_the_same_readonly_lock(self):
        scripts = Path(__file__).resolve().parent
        helper = scripts / ('maintenance.sh' if (scripts / 'maintenance.sh').exists() else 'common.sh')
        target = self.root / 'scripts' / helper.name
        target.parent.mkdir()
        shutil.copy2(helper, target)
        (self.root / '.env').write_text('SYNTHETIC=1\n')
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        environment = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                       'PROJECT_ROOT': str(self.root), 'JOURNAL_ROOT': str(self.root),
                       'MAINTENANCE_LOCK_HELD': '0', 'ENV_FILE': str(self.root / '.env')}
        command = ['bash', '-c', 'source "$1"; if declare -F lock_maintenance >/dev/null; then lock_maintenance; fi; printf acquired', 'test', str(target)]
        self.lock.write_text('synthetic marker')
        self.lock.chmod(0o444)
        with updater.open_maintenance_lock() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertNotIn('acquired', blocked.stdout)
        allowed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn('acquired', allowed.stdout)
        self.assertEqual(self.lock.read_text(), 'synthetic marker')


if __name__ == '__main__':
    unittest.main()
