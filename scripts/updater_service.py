#!/usr/bin/env python3
"""Independent Docker updater with streamed logs, bounded jobs and verified rollback."""
import copy
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request
import uuid

try:
    from scripts.update_metadata import (read_version, version_tuple, parse_changelog_markdown,
                                         releases_between, check_remote)
except ModuleNotFoundError:
    from update_metadata import (read_version, version_tuple, parse_changelog_markdown,
                                 releases_between, check_remote)

SERVICE_CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

PROJECT = 'schooljournal'
REPO = 'yrskrs/schooljournal'
APP_SERVICE = 'web'
WRITERS = ['web']
HEALTH_URL = 'http://nginx/healthz/'
ROOT = Path(os.environ.get('PROJECT_ROOT', Path(__file__).resolve().parent.parent))
STATUS_FILE = ROOT / 'backups/update_status.json'
LOCK_FILE = ROOT / '.maintenance.lock'
STATE_LOCK = threading.RLock()
CURRENT_STATE = {'state': 'idle', 'step': 0, 'step_name': 'Готово до роботи', 'progress': 0,
                 'logs': [], 'error': None, 'rolled_back': False, 'rollback_error': None,
                 'target_version': None, 'current_version': None, 'releases': [],
                 'backup_file': None, 'backup_dir': None, 'job_id': None}
_version_tuple = parse_version_tuple = version_tuple
filter_releases_between = filter_intermediate_releases = get_releases_between = releases_between
parse_changelog_all_releases = parse_changelog_markdown


def get_current_version():
    return read_version(ROOT)


def make_release_metadata_readable(root):
    paths = [Path(root) / name for name in ('VERSION', 'RELEASES.json', 'CHANGELOG.md')]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError('Release metadata must be regular files.')
    for path in paths:
        path.chmod(0o644)


def save_status():
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(CURRENT_STATE, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o644)
    os.replace(temporary, STATUS_FILE)


def update_state(**kwargs):
    with STATE_LOCK:
        CURRENT_STATE.update(kwargs)
        CURRENT_STATE['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save_status()


def append_log(line):
    line = str(line).strip()
    if not line:
        return
    entry = f'[{time.strftime("%H:%M:%S")}] {line}'
    print(entry, flush=True)
    with STATE_LOCK:
        logs = CURRENT_STATE['logs']
        logs.append(entry)
        CURRENT_STATE['logs'] = logs[-1500:]
        update_state()


def load_status():
    if STATUS_FILE.exists():
        try:
            saved = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            CURRENT_STATE.update(saved)
            if saved.get('state') == 'running':
                CURRENT_STATE.update(state='error', error='Службу оновлення перезапущено під час операції. Перевірте журнал і резервну копію перед повторним запуском.',
                                     step_name='Операцію перервано', progress=min(saved.get('progress', 0), 99))
        except (ValueError, OSError):
            pass
    try:
        CURRENT_STATE['current_version'] = get_current_version()
    except Exception:
        pass
    save_status()


def check_github_updates(current_ver=None):
    return check_remote(ROOT, os.environ.get('GITHUB_REPO', REPO), current_ver or get_current_version(),
                        os.environ.get('GITHUB_BRANCH', 'main'))


def capture(command, env=None):
    proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise RuntimeError(f'{command[0]}: {proc.stderr.strip() or proc.stdout.strip()}')
    return proc.stdout.strip()


def run_command(command, env=None, timeout=600):
    """Drain both output streams while running; terminate the process group on timeout."""
    output, pending = [], b''
    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline, heartbeat = time.monotonic() + timeout, time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise TimeoutError(f'Перевищено час виконання ({timeout} с): {command[0]}')
                for key, _ in selector.select(1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    pending += chunk.replace(b'\r', b'\n')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        text = line.decode('utf-8', errors='replace').strip()
                        if text:
                            output.append(text)
                            append_log(text)
                if time.monotonic() - heartbeat >= 10:
                    append_log('Команда працює; очікуємо завершення…')
                    heartbeat = time.monotonic()
            if pending:
                text = pending.decode('utf-8', errors='replace')
                output.append(text)
                append_log(text)
            returncode = process.wait(timeout=max(1, deadline - time.monotonic()))
            if returncode:
                raise RuntimeError(f'Команда {command[0]} завершилася з кодом {returncode}:\n' + '\n'.join(output[-25:]))
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            selector.close()
    return '\n'.join(output)


class Deployment:
    """Freeze Compose config and translate bind paths for the host Docker daemon."""
    def __init__(self):
        self.env = dict(os.environ, COMPOSE_PROJECT_NAME=os.environ.get('COMPOSE_PROJECT_NAME', PROJECT),
                        MAINTENANCE_LOCK_HELD='1', PYTHONUNBUFFERED='1')
        files = [ROOT / 'compose.yaml', ROOT / '.release-image.yaml', ROOT / 'compose.override.yaml']
        command = ['docker', 'compose', '--env-file', str(ROOT / '.env')]
        for file in files:
            if file.exists():
                command += ['-f', str(file)]
        self.config = json.loads(capture(command + ['config', '--format', 'json'], self.env))
        host_root = str(ROOT)
        if Path('/.dockerenv').exists():
            mounts = json.loads(capture(['docker', 'inspect', '--format', '{{json .Mounts}}', os.uname().nodename]))
            host_root = next((m['Source'] for m in mounts if m['Destination'] == str(ROOT)), '')
            if not host_root:
                raise RuntimeError('Не знайдено шлях проєкту на Docker-хості')
        for service in self.config['services'].values():
            for volume in service.get('volumes', []):
                if volume.get('type') == 'bind' and volume['source'].startswith(str(ROOT) + '/'):
                    volume['source'] = host_root + volume['source'][len(str(ROOT)):]
                elif volume.get('type') == 'bind' and volume['source'] == str(ROOT):
                    volume['source'] = host_root
        fd, path = tempfile.mkstemp(prefix='.compose-update-', suffix='.json', dir=ROOT / 'backups')
        os.close(fd)
        self.path = Path(path)
        self.env.update(COMPOSE_FILE=path, UPDATER_COMPOSE_FILE=path)
        self.write()
        self.original_config = copy.deepcopy(self.config)

    def refresh(self):
        """Load the checked-out release's services and environment after merge."""
        replacement = Deployment()
        try:
            self.config = replacement.config
            self.write()
        finally:
            replacement.close()

    def restore_config(self):
        """Rollback must keep the original volume/network/env configuration."""
        self.config = copy.deepcopy(self.original_config)
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def select_image(self, image, persist=True):
        for service in WRITERS:
            self.config['services'][service]['image'] = image
            self.config['services'][service]['pull_policy'] = 'never'
        self.write()
        if not persist:
            return
        services = ''.join(f'  {service}:\n    image: {image}\n    pull_policy: never\n' for service in WRITERS)
        (ROOT / '.release-image.yaml').write_text('services:\n' + services, encoding='utf-8')
        env_file = ROOT / '.env'
        content = env_file.read_text(encoding='utf-8')
        entry = 'JOURNAL_IMAGE=' + image
        if re.search(r'^JOURNAL_IMAGE=', content, re.MULTILINE):
            content = re.sub(r'^JOURNAL_IMAGE=[^\n]*', entry, content, flags=re.MULTILINE)
        else:
            content = content.rstrip() + '\n' + entry + '\n'
        temporary = env_file.with_suffix('.image-update')
        temporary.write_text(content, encoding='utf-8')
        temporary.chmod(0o600)
        owner = env_file.stat()
        os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, env_file)


    def dc(self, *args, timeout=600):
        return run_command(['docker', 'compose', *args], self.env, timeout)

    def query(self, *args):
        return capture(['docker', 'compose', *args], self.env)

    def close(self):
        self.path.unlink(missing_ok=True)


def installed_health(expected=None, timeout=180):
    deadline = time.monotonic() + timeout
    error = ''
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(os.environ.get('APP_URL', HEALTH_URL), headers={'Host': 'localhost'})
            with urllib.request.urlopen(request, timeout=4) as response:
                data = json.load(response)
            if data.get('status') != 'ok' or not data.get('version'):
                raise ValueError('Некоректний healthcheck')
            if expected and data['version'] != expected:
                raise ValueError(f'Запущено {data["version"]}, очікується {expected}')
            return data['version']
        except Exception as exc:
            error = str(exc)
            time.sleep(2)
    raise RuntimeError(f'Перевірка готовності не пройдена: {error}')


def verify_deployment(deployment, image, version):
    ids = deployment.query('ps', '-aq', *WRITERS).splitlines()
    if len(ids) != len(WRITERS):
        raise RuntimeError('Запущено не всі служби сайту')
    expected_id = capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    for container in ids:
        actual = capture(['docker', 'inspect', '--format', '{{.Image}}', container])
        if actual != expected_id:
            raise RuntimeError('Контейнер запущено зі старого або іншого образу')
    installed_health(version)


def perform_rollback(deployment, backup, old_image, old_commit, old_version):
    update_state(state='running', step_name='Автоматичний відкат до попередньої версії', progress=95)
    append_log('Відновлюємо попередній Docker-образ, БД, файли та ревізію коду…')
    deployment.restore_config()
    deployment.select_image(old_image)
    deployment.env['EXPECTED_VERSION'] = old_version
    flags = ['--yes']
    if PROJECT == 'schooljournal':
        flags += ['--no-safety-backup', '--keep-code']
    elif PROJECT == 'schoolwork2':
        flags += ['--no-safety-backup']
    run_command(['bash', str(ROOT / 'scripts/restore.sh'), str(backup), *flags], deployment.env, 600)
    run_command(['python3', str(ROOT / 'scripts/configure_integration_network.py')], deployment.env, 90)
    run_command(['git', 'reset', '--hard', old_commit], deployment.env, 60)
    make_release_metadata_readable(ROOT)
    deployment.select_image(old_image)
    verify_deployment(deployment, old_image, old_version)
    update_state(rolled_back=True, current_version=old_version)
    append_log(f'✅ Відкат перевірено: попередня версія {old_version}, БД та файли відновлені.')


def execute_update_job(target_ref='origin/main', target_ver=None, releases_list=None):
    deployment, backup, worktree = None, None, None
    backup_complete = deploy_started = writers_stopped = False
    try:
        if target_ref not in ('HEAD', 'origin/main'):
            raise ValueError('Дозволено оновлення origin/main або перезбірку HEAD')
        if capture(['git', 'status', '--porcelain']):
            raise RuntimeError('Є незакомічені зміни. Закомітьте або збережіть їх перед оновленням.')
        old_commit = capture(['git', 'rev-parse', 'HEAD'])
        deployment = Deployment()
        old_version = installed_health(timeout=10)
        container = deployment.query('ps', '-aq', APP_SERVICE)
        if not container:
            raise RuntimeError('Відсутній контейнер застосунку')
        old_image = capture(['docker', 'inspect', '--format', '{{.Image}}', container])
        # Capture labels from the container even if a rebuild replaced its image tag.
        revision = capture(['docker', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', container])
        if len(revision) == 40:
            capture(['git', 'cat-file', '-e', revision + '^{commit}'])
            old_commit = revision
        elif read_version(ROOT) != old_version:
            # Older installations built with VCS_REF=local: use the nearest matching release revision.
            for candidate in capture(['git', 'log', '-50', '--format=%H']).splitlines():
                try:
                    source_version = capture(['git', 'show', candidate + ':VERSION'])
                except RuntimeError:
                    continue
                if source_version == old_version:
                    old_commit = candidate
                    break
            else:
                raise RuntimeError('Не знайдено ревізію попередньої версії; оновлення скасовано')
        try:
            capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', old_image])
        except RuntimeError:
            append_log('Попередній образ видалено зі сховища; зберігаємо код чинного контейнера…')
            # Volumes are excluded by Docker commit. Clear runtime env defaults so secrets
            # are not baked into the snapshot; Compose supplies them again at restore.
            environment = json.loads(capture(['docker', 'inspect', '--format', '{{json .Config.Env}}', container]))
            changes = []
            for entry in environment:
                key = entry.split('=', 1)[0]
                if key not in ('PATH', 'LANG', 'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'DEBIAN_FRONTEND'):
                    changes += ['--change', f'ENV {key}=""']
            snapshot = f'{PROJECT}-rollback:container-{CURRENT_STATE["job_id"][:12]}'
            try:
                run_command(['docker', 'commit', *changes, container, snapshot], deployment.env, 300)
            except RuntimeError:
                append_log('Метадані старого образу пошкоджено; експортуємо файлову систему чинного контейнера…')
                config = json.loads(capture(['docker', 'inspect', '--format', '{{json .Config}}', container]))
                fd, exported_path = tempfile.mkstemp(prefix='.rollback-rootfs-', suffix='.tar', dir=ROOT / 'backups')
                os.close(fd)
                try:
                    run_command(['docker', 'export', '--output', exported_path, container], deployment.env, 600)
                    import_changes = []
                    for key in ('Cmd', 'Entrypoint'):
                        if config.get(key):
                            import_changes += ['--change', key.upper() + ' ' + json.dumps(config[key])]
                    for key in ('User', 'WorkingDir'):
                        if config.get(key):
                            import_changes += ['--change', ('WORKDIR' if key == 'WorkingDir' else 'USER') + ' ' + config[key]]
                    for entry in environment:
                        if entry.split('=', 1)[0] in ('PATH', 'LANG', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED'):
                            import_changes += ['--change', 'ENV ' + entry]
                    for key, value in (config.get('Labels') or {}).items():
                        if key.startswith('org.opencontainers.'):
                            import_changes += ['--change', f'LABEL {key}=' + json.dumps(value)]
                    run_command(['docker', 'import', *import_changes, exported_path, snapshot], deployment.env, 600)
                finally:
                    Path(exported_path).unlink(missing_ok=True)
            old_image = capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', snapshot])
        deployment.select_image(old_image, persist=False)
        deployment.env.update(BACKUP_IMAGE=old_image, BACKUP_SOURCE_COMMIT=old_commit)
        update_state(current_version=old_version, step=1, progress=10, step_name='Перевірка ревізії коду')
        append_log('Крок 1/5: Перевірка коду та джерела оновлення…')
        if target_ref != 'HEAD':
            run_command(['python3', str(ROOT / 'scripts/fetch_update.py'), '--branch', 'main'], deployment.env, 90)
            capture(['git', 'merge-base', '--is-ancestor', 'HEAD', target_ref])
        commit = capture(['git', 'rev-parse', '--verify', target_ref + '^{commit}'])
        worktree = Path(tempfile.mkdtemp(prefix=f'.build-{PROJECT}-', dir=ROOT / 'backups'))
        run_command(['git', 'worktree', 'add', '--detach', str(worktree), commit], deployment.env, 60)
        version = read_version(worktree)
        if target_ver and version != target_ver:
            raise RuntimeError(f'Цільова ревізія має версію {version}; перевірено {target_ver}. Перевірте оновлення знову.')
        from_release = check_remote(worktree, os.environ.get('GITHUB_REPO', REPO), old_version) if target_ref != 'HEAD' else {}
        update_state(target_version=version, releases=from_release.get('releases', []), step=2,
                     progress=30, step_name='Збірка Docker-образу')
        append_log(f'Крок 2/5: Збираємо версію {version}, коміт {commit[:12]}…')
        image = f'{PROJECT}-candidate:{commit[:12]}-{CURRENT_STATE["job_id"][:8]}'
        run_command(['docker', 'build', '--progress=plain', '--build-arg', f'VCS_REF={commit}',
                     '--build-arg', f'APP_VERSION={version}', '-t', image, str(worktree)], deployment.env, 1200)
        if PROJECT != 'schooltest5':
            append_log('Перевіряємо імпорт і конфігурацію кандидата без робочої БД…')
            settings = 'config.test_settings' if PROJECT == 'schooljournal' else 'schoolnet.test_settings'
            run_command(['docker', 'run', '--rm', '--network', 'none', '-e', f'DJANGO_SETTINGS_MODULE={settings}',
                         '--entrypoint', 'python', image, 'manage.py', 'check'], deployment.env, 120)
        run_command(['git', 'worktree', 'remove', '--force', str(worktree)], deployment.env, 60)
        worktree = None
        if PROJECT == 'schooljournal':
            run_command(['python3', str(ROOT / 'scripts/archive.py'), 'check-target', str(ROOT)], deployment.env, 60)
        update_state(step=3, progress=55, step_name='Зупинка запису та створення резервної копії')
        append_log('Крок 3/5: Зупиняємо запис і створюємо перевірений знімок…')
        writers_stopped = True
        deployment.dc('stop', *WRITERS, timeout=120)
        if PROJECT == 'schooltest5':
            backup = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=ROOT / 'backups'))
            run_command(['bash', str(ROOT / 'scripts/backup.sh'), '--output', str(backup), '--keep-stopped'], deployment.env, 600)
        elif PROJECT == 'schooljournal':
            output = run_command(['bash', str(ROOT / 'scripts/backup.sh'), '--quiesced'], deployment.env, 1200)
            backup = Path(output.splitlines()[-1])
        else:
            deployment.env['SCHOOLNET_BACKUP_KEEP_STOPPED'] = '1'
            output = run_command(['bash', str(ROOT / 'scripts/backup.sh')], deployment.env, 600)
            backup = Path(output.split('Резервна копія перевірена:', 1)[1].strip().splitlines()[0])
        if not backup.exists():
            raise RuntimeError('Резервну копію не знайдено після backup.sh')
        backup_complete = True
        update_state(backup_file=str(backup), backup_dir=str(backup),
                     rollback={'backup': str(backup), 'image': old_image, 'commit': old_commit, 'version': old_version})
        rollback_tag = f'{PROJECT}-rollback:{CURRENT_STATE["job_id"][:12]}'
        run_command(['docker', 'image', 'tag', old_image, rollback_tag], deployment.env, 60)
        update_state(step=4, progress=75, step_name='Міграції та запуск зібраного образу')
        append_log('Крок 4/5: Застосовуємо ревізію і запускаємо зібраний образ…')
        deploy_started = True
        run_command(['git', 'merge', '--ff-only', commit], deployment.env, 60)
        make_release_metadata_readable(ROOT)
        deployment.refresh()
        deployment.select_image(image)
        if PROJECT != 'schooltest5':
            deployment.dc('run', '--rm', '--no-deps', '-T', '-e', 'SKIP_INIT=1', APP_SERVICE,
                          'python', 'manage.py', 'check')
            deployment.dc('run', '--rm', '--no-deps', '-T', '-e', 'SKIP_INIT=1', APP_SERVICE,
                          'python', 'manage.py', 'migrate', '--noinput')
            deployment.dc('run', '--rm', '--no-deps', '-T', '-e', 'SKIP_INIT=1', APP_SERVICE,
                          'python', 'manage.py', 'collectstatic', '--noinput')
        deployment.dc('up', '-d', '--no-build', '--no-deps', '--wait', '--wait-timeout', '180', *WRITERS, timeout=240)
        run_command(['python3', str(ROOT / 'scripts/configure_integration_network.py')], deployment.env, 90)
        if PROJECT == 'schooljournal':
            deployment.dc('up', '-d', '--no-deps', 'nginx', timeout=120)
            deployment.dc('restart', 'nginx', timeout=120)
        update_state(step=5, progress=90, step_name='Перевірка БД, версії та запущеного образу')
        append_log('Крок 5/5: Перевіряємо готовність і точний образ кожної служби…')
        verify_deployment(deployment, image, version)
        append_log(f'✅ Успішно завершено. Встановлено {version}; коміт {commit[:12]}.')
        update_state(state='success', progress=100, step_name='Успішно завершено', current_version=version, error=None)
    except Exception as exc:
        error = str(exc)
        append_log(f'❌ Помилка: {error}')
        rollback_error = None
        if deploy_started and backup_complete:
            try:
                perform_rollback(deployment, backup, old_image, old_commit, old_version)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
                append_log(f'❌ Помилка відкату: {rollback_error}')
        elif deployment and writers_stopped:
            try:
                deployment.dc('start', *WRITERS, timeout=120)
                installed_health(old_version, timeout=90)
                append_log('Попередню версію збережено; її готовність перевірено.')
            except Exception as resume_exc:
                rollback_error = str(resume_exc)
        update_state(state='error', error=error, rollback_error=rollback_error,
                     progress=min(CURRENT_STATE.get('progress', 0), 99),
                     step_name='Оновлення не вдалося; попередню версію відновлено' if CURRENT_STATE.get('rolled_back') else 'Оновлення не вдалося')
    finally:
        if worktree:
            try:
                run_command(['git', 'worktree', 'remove', '--force', str(worktree)], timeout=60)
            except Exception as exc:
                append_log(f'Очищення робочого дерева: {exc}')
        if deployment:
            deployment.close()


def execute_rollback_job(backup=None):
    deployment = None
    try:
        info = CURRENT_STATE.get('rollback')
        if not info or (backup and str(backup) != info['backup']):
            raise ValueError('Немає перевіреної копії з відповідним образом для цього відкату')
        if not Path(info['backup']).exists():
            raise ValueError('Резервну копію не знайдено')
        if capture(['git', 'status', '--porcelain']):
            raise ValueError('Відкат вимагає чистого Git checkout')
        deployment = Deployment()
        perform_rollback(deployment, info['backup'], info['image'], info['commit'], info['version'])
        update_state(state='success', progress=100, target_version=info['version'], step_name='Відкат успішно завершено')
    except Exception as exc:
        append_log(f'Помилка відкату: {exc}')
        update_state(state='error', error=str(exc), progress=99)
    finally:
        if deployment:
            deployment.close()


def open_maintenance_lock():
    """Share flock with host CLI even when the file belongs to a container UID."""
    try:
        descriptor = os.open(LOCK_FILE, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        descriptor = os.open(LOCK_FILE, os.O_RDONLY)
    else:
        # Lock files contain no secrets; keep new files readable across UIDs,
        # independently of the updater's umask. Never replace a locked inode.
        os.fchmod(descriptor, 0o644)
    return os.fdopen(descriptor, 'r')


def start_job(function, args):
    """Reserve state and the cross-process maintenance lock before returning HTTP 202."""
    with STATE_LOCK:
        if CURRENT_STATE.get('state') == 'running':
            return None
        lock = open_maintenance_lock()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return None
        job_id = uuid.uuid4().hex
        update_state(state='running', job_id=job_id, step=1, progress=5, step_name='Підготовка операції',
                     logs=[], error=None, rollback_error=None, rolled_back=False,
                     started_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        def worker():
            try:
                function(*args)
            except Exception as exc:
                update_state(state='error', error=str(exc), step_name='Помилка служби оновлення', progress=0)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            if (CURRENT_STATE.get('state') == 'success' and not CURRENT_STATE.get('rolled_back') and Path('/.dockerenv').exists()
                    and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SERVICE_CODE_HASH):
                append_log('Перезапускаємо оновлений код служби updater; фінальний статус збережено.')
                subprocess.Popen(['docker', 'restart', os.uname().nodename],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        threading.Thread(target=worker, daemon=True).start()
        return job_id


class UpdaterHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ('/', '/health', '/healthz'):
            self.send_json(200, {'status': 'ok', 'service': PROJECT + '-updater'})
        elif parsed.path in ('/status', '/api/status'):
            with STATE_LOCK:
                status = copy.deepcopy(CURRENT_STATE)
            try:
                status['current_version'] = get_current_version()
            except Exception:
                pass
            self.send_json(200, status)
        elif parsed.path in ('/check', '/api/check'):
            current = parse_qs(parsed.query).get('current_version', [None])[0]
            self.send_json(200, check_github_updates(current))
        else:
            self.send_json(404, {'ok': False, 'error': 'Маршрут не знайдено'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length < 0 or length > 65536:
                raise ValueError('Некоректний розмір запиту')
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Очікується JSON object')
            if self.path in ('/check', '/api/check'):
                self.send_json(200, check_github_updates(data.get('current_version')))
                return
            if self.path in ('/update', '/api/update'):
                ref = data.get('ref', 'origin/main')
                if ref not in ('HEAD', 'origin/main'):
                    raise ValueError('Дозволено лише origin/main або HEAD')
                job_id = start_job(execute_update_job, (ref, data.get('version')))
            elif self.path in ('/rollback', '/api/rollback'):
                job_id = start_job(execute_rollback_job, (data.get('backup_file') or data.get('backup_dir'),))
            else:
                self.send_json(404, {'ok': False, 'error': 'Маршрут не знайдено'})
                return
            self.send_json(202 if job_id else 409, {'ok': bool(job_id), 'job_id': job_id,
                           'error': None if job_id else 'Інша операція обслуговування вже виконується'})
        except (ValueError, TypeError) as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def run_server(port=8001):
    # Trust only the mounted checkout, not arbitrary repositories.
    subprocess.run(['git', 'config', '--global', '--add', 'safe.directory', str(ROOT)], check=True)
    load_status()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(('0.0.0.0', port), UpdaterHTTPHandler) as server:
        print(f'{PROJECT} updater on :{port}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    run_server(int(os.environ.get('UPDATER_PORT', 8001)))
