"""Versioned, checksummed snapshots. Never include credentials or live personal data in source."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

DIRECTORIES = ('apps', 'config', 'templates', 'static', 'scripts', 'tests', 'nginx', '.github')
FILES = ('manage.py', 'requirements.txt', 'Dockerfile', 'compose.yaml', 'compose.dev.yaml',
         '.env.example', '.gitignore', '.dockerignore', 'VERSION', 'RELEASES.json', 'LICENSE',
         'setup.sh', 'update.sh', 'backup.sh', 'restore.sh', 'service.sh', 'README.md', 'EXTERNAL_API.md',
         'DEPLOYMENT.md', 'CHANGELOG.md', 'IMPLEMENTATION_PLAN.md', 'WORK_PLAN.md', 'RELEASE_CHECKLIST.md')
PAYLOADS = ('database.dump', 'media.tar.gz', 'source.tar.gz', 'web-image.tar.gz', 'manifest.json')
MEMBERS = set(PAYLOADS) | {'SHA256SUMS'}


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def source_allowed(name):
    parts = PurePosixPath(name).parts
    if not parts:
        return False
    if any(part in ('__pycache__', '.git', '.aws', '.ssh', 'media', 'backups', 'nz.uz', 'samples') for part in parts) or name.endswith(('.pyc', '.pyo', '.dump', '.sqlite', '.sqlite3', '.db', '.sql')):
        return False
    if any(part == '.env' or (part.startswith('.env.') and part != '.env.example') for part in parts):
        return False
    return parts[0] in DIRECTORIES or name in FILES


def checked_members(archive, source=False):
    seen = set()
    for member in archive.getmembers():
        name = member.name
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or name in seen:
            raise ValueError('Unsafe or duplicated archive path')
        if not (member.isfile() or member.isdir()):
            raise ValueError('Links and special files are not accepted')
        normalized = str(path)
        if source and normalized != '.' and not source_allowed(normalized):
            raise ValueError('Unexpected source file: ' + normalized)
        seen.add(name)
    return archive.getmembers()


def create(work, root, image):
    with tarfile.open(work / 'source.tar.gz', 'w:gz') as archive:
        paths = [root / name for name in FILES if (root / name).is_file()]
        for directory in DIRECTORIES:
            if (root / directory).exists():
                paths.extend((root / directory).rglob('*'))
        for path in sorted(paths):
            name = path.relative_to(root).as_posix()
            if path.is_file() and not path.is_symlink() and source_allowed(name):
                archive.add(path, arcname=name, recursive=False)
    commit = ''
    if shutil.which('git'):
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        commit = result.stdout.strip() if result.returncode == 0 else ''
    manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'version': (root / 'VERSION').read_text().strip(), 'commit': commit, 'image': image}
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (work / 'SHA256SUMS').write_text(''.join(digest(work / name) + '  ' + name + '\n' for name in PAYLOADS))


def extract(archive_path, destination):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = checked_members(archive)
        if {m.name for m in members} != MEMBERS or any(not m.isfile() for m in members):
            raise ValueError('Incomplete or unsupported backup format')
        archive.extractall(destination, members=members, filter='data')
    lines = (destination / 'SHA256SUMS').read_text().splitlines()
    expected = {}
    for line in lines:
        checksum, name = line.split('  ', 1)
        if name not in PAYLOADS or name in expected or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError('Invalid checksum manifest')
        expected[name] = checksum
    if set(expected) != set(PAYLOADS):
        raise ValueError('Missing checksums')
    for name, checksum in expected.items():
        if digest(destination / name) != checksum:
            raise ValueError('Checksum mismatch: ' + name)
    metadata = json.loads((destination / 'manifest.json').read_text())
    if metadata.get('format') != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', metadata.get('image', '')):
        raise ValueError('Invalid snapshot metadata')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', metadata.get('version', '')):
        raise ValueError('Invalid version')
    if metadata.get('commit') and not re.fullmatch(r'[0-9a-f]{40}', metadata['commit']):
        raise ValueError('Invalid Git commit')
    for name in ('source.tar.gz', 'media.tar.gz'):
        with tarfile.open(destination / name, 'r:gz') as nested:
            checked_members(nested, source=(name == 'source.tar.gz'))
            if name == 'source.tar.gz':
                version = nested.extractfile('VERSION').read().decode().strip()
                if version != metadata['version']:
                    raise ValueError('Source version mismatch')


def install_source(archive_path, root):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = checked_members(archive, source=True)
        if 'VERSION' not in {m.name for m in members}:
            raise ValueError('Source missing VERSION')
        for directory in DIRECTORIES:
            target = root / directory
            if target.is_symlink():
                raise ValueError('Source directory must not be a symlink')
            if target.exists():
                shutil.rmtree(target)
        for name in FILES:
            target = root / name
            if target.is_symlink() or target.is_file():
                target.unlink()
        archive.extractall(root, members=members, filter='data')


def main():
    command, *args = sys.argv[1:]
    if command == 'create':
        create(Path(args[0]), Path(args[1]), args[2])
    elif command == 'check':
        with tempfile.TemporaryDirectory(prefix='schooljournal-check-') as tmp:
            extract(Path(args[0]), Path(tmp))
        print('Snapshot checksums, metadata and paths verified.', file=sys.stderr)
    elif command == 'extract':
        extract(Path(args[0]), Path(args[1]))
    elif command == 'install-source':
        install_source(Path(args[0]), Path(args[1]))
    elif command == 'tag':
        metadata = json.loads(Path(args[0]).read_text())
        print('schooljournal-restored:' + metadata['image'].split(':')[1][:16])
    else:
        raise ValueError('Unsupported command')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        print('Snapshot error: ' + str(error), file=sys.stderr)
        sys.exit(1)
