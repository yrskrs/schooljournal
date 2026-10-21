import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('snapshot_archives', Path(__file__).resolve().parents[1] / 'scripts/archive.py')
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-snapshot-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.work = Path(self.tmp.name) / 'work'
        self.root.mkdir()
        self.work.mkdir()
        (self.root / 'VERSION').write_text('1.0.0\n')
        (self.root / 'manage.py').write_text('# Synthetic source only\n')
        for name in ('.env', 'db.sqlite3'):
            (self.root / name).write_text('synthetic-private-content')
        for directory in ('backups', 'media', 'nz.uz', 'samples'):
            (self.root / directory).mkdir()
            (self.root / directory / 'private.txt').write_text('synthetic-private-content')
        (self.work / 'database.dump').write_bytes(b'synthetic-dump')
        (self.work / 'web-image.tar.gz').write_bytes(b'synthetic-image')
        with tarfile.open(self.work / 'media.tar.gz', 'w:gz') as archive:
            item = tarfile.TarInfo('synthetic.txt')
            item.size = 5
            archive.addfile(item, io.BytesIO(b'media'))
        snapshot.create(self.work, self.root, 'sha256:' + 'a' * 64)

    def pack(self):
        target = Path(self.tmp.name) / 'backup.tar.gz'
        with tarfile.open(target, 'w:gz') as archive:
            for name in snapshot.MEMBERS:
                archive.add(self.work / name, arcname=name)
        return target

    def test_source_excludes_credentials_databases_and_private_folders(self):
        with tarfile.open(self.work / 'source.tar.gz') as archive:
            self.assertEqual(set(archive.getnames()), {'VERSION', 'manage.py'})
        destination = Path(self.tmp.name) / 'verified'
        destination.mkdir()
        snapshot.extract(self.pack(), destination)

    def test_corruption_is_rejected(self):
        (self.work / 'database.dump').write_bytes(b'corrupted')
        destination = Path(self.tmp.name) / 'rejected'
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            snapshot.extract(self.pack(), destination)

    def test_traversal_and_symlinks_are_rejected(self):
        for name, link in (('../escape', False), ('link', True)):
            archive_path = Path(self.tmp.name) / 'unsafe.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/etc/passwd'
                archive.addfile(item)
            destination = Path(self.tmp.name) / ('rejected-' + str(link))
            destination.mkdir()
            with self.assertRaises(ValueError):
                snapshot.extract(archive_path, destination)
        self.assertFalse((Path(self.tmp.name) / 'escape').exists())

    def test_missing_payload_is_rejected(self):
        archive_path = Path(self.tmp.name) / 'incomplete.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as archive:
            archive.add(self.work / 'manifest.json', arcname='manifest.json')
        destination = Path(self.tmp.name) / 'incomplete'
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            snapshot.extract(archive_path, destination)

    def test_source_restore_preserves_env_and_removes_obsolete_source(self):
        destination = Path(self.tmp.name) / 'installed'
        destination.mkdir()
        (destination / '.env').write_text('synthetic-retained-secret')
        (destination / 'apps').mkdir()
        (destination / 'apps' / 'obsolete.py').write_text('# obsolete')
        (destination / 'school_sync').mkdir()
        (destination / 'school_sync' / 'core.py').write_text('# package recovered from Git for a legacy snapshot')
        snapshot.install_source(self.work / 'source.tar.gz', destination)
        self.assertEqual((destination / '.env').read_text(), 'synthetic-retained-secret')
        self.assertEqual((destination / 'VERSION').read_text(), '1.0.0\n')
        self.assertFalse((destination / 'apps' / 'obsolete.py').exists())
        self.assertTrue((destination / 'school_sync' / 'core.py').exists())

    def test_sync_package_is_archived_verified_and_restored(self):
        package = self.root / 'school_sync'
        package.mkdir()
        (package / '__init__.py').write_text('# synthetic package')
        (package / 'core.py').write_text('# synthetic sync implementation')
        (package / '__pycache__').mkdir()
        (package / '__pycache__' / 'core.pyc').write_bytes(b'private-cache')
        snapshot.create(self.work, self.root, 'sha256:' + 'a' * 64)
        verified = Path(self.tmp.name) / 'verified-sync'
        verified.mkdir()
        snapshot.extract(self.pack(), verified)
        destination = Path(self.tmp.name) / 'installed-sync'
        (destination / 'school_sync').mkdir(parents=True)
        (destination / 'school_sync' / 'obsolete.py').write_text('# obsolete')
        snapshot.install_source(verified / 'source.tar.gz', destination)
        self.assertEqual((destination / 'school_sync' / 'core.py').read_text(), '# synthetic sync implementation')
        self.assertTrue((destination / 'school_sync' / '__init__.py').exists())
        self.assertFalse((destination / 'school_sync' / '__pycache__').exists())
        self.assertFalse((destination / 'school_sync' / 'obsolete.py').exists())

    def test_unwritable_source_is_rejected_before_any_file_is_removed(self):
        destination = Path(self.tmp.name) / 'blocked-source'
        blocked = destination / 'apps' / 'journals' / 'management'
        blocked.mkdir(parents=True)
        marker = destination / 'apps' / 'keep.py'
        marker.write_text('# keep this source')
        with patch.object(snapshot.os, 'access', side_effect=lambda path, mode: Path(path) != blocked):
            with self.assertRaises(PermissionError):
                snapshot.install_source(self.work / 'source.tar.gz', destination)
        self.assertEqual(marker.read_text(), '# keep this source')
        self.assertTrue(blocked.is_dir())

    import sys
    @unittest.skipIf(sys.platform == 'win32', 'Symlinks require special privileges on Windows')
    def test_source_symlink_is_rejected_before_another_directory_is_removed(self):
        destination = Path(self.tmp.name) / 'linked-source'
        (destination / 'apps').mkdir(parents=True)
        marker = destination / 'apps' / 'keep.py'
        marker.write_text('# keep this source')
        (destination / 'config').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            snapshot.install_source(self.work / 'source.tar.gz', destination)
        self.assertTrue(marker.exists())
