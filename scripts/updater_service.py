#!/usr/bin/env python3
"""Local updater service: executes updates, reports progress, and performs automatic rollbacks."""
import http.server
import json
import os
from pathlib import Path
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / 'backups' / 'update_status.json'
LOCK_FILE = ROOT / '.updater.lock'

CURRENT_STATE = {
    'state': 'idle',  # 'idle', 'running', 'success', 'error'
    'step': 0,
    'step_name': 'Готово до роботи',
    'progress': 0,
    'logs': [],
    'error': None,
    'rolled_back': False,
    'target_version': None,
    'current_version': None,
    'releases': [],
    'backup_file': None,
    'started_at': None,
    'updated_at': None,
}

STATE_LOCK = threading.Lock()


def get_current_version():
    version_file = ROOT / 'VERSION'
    if version_file.exists():
        return version_file.read_text().strip()
    return '1.0.0'


def save_status():
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(CURRENT_STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to write status file: {e}")


def update_state(**kwargs):
    with STATE_LOCK:
        CURRENT_STATE.update(kwargs)
        CURRENT_STATE['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save_status()


def append_log(line):
    clean_line = line.strip()
    if not clean_line:
        return
    with STATE_LOCK:
        CURRENT_STATE['logs'].append(clean_line)
        if len(CURRENT_STATE['logs']) > 300:
            CURRENT_STATE['logs'] = CURRENT_STATE['logs'][-300:]
        CURRENT_STATE['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save_status()


def parse_changelog_markdown(text):
    """Parse standard markdown CHANGELOG into structured release entries."""
    releases = []
    current_entry = None
    if not text:
        return releases

    for raw_line in text.splitlines():
        line = raw_line.strip()
        header_match = re.match(r'^##\s+v?(\d+[\.\d\w\-\+]*)(?:\s*—\s*|\s*-\s*|\s+)?(.*)$', line)
        if header_match:
            if current_entry:
                releases.append(current_entry)
            version = header_match.group(1).strip()
            rest = header_match.group(2).strip()
            is_date = bool(re.match(r'^\d{4}-\d{2}-\d{2}', rest))
            current_entry = {
                'version': version,
                'date': rest if is_date else '',
                'title': f'Оновлення {version}' if (is_date or not rest) else rest,
                'changes': []
            }
        elif current_entry and line.startswith(('-', '*')):
            bullet = line.lstrip('-*').strip()
            if bullet:
                current_entry['changes'].append(bullet)

    if current_entry:
        releases.append(current_entry)
    return releases


def _version_tuple(v_str):
    try:
        parts = [int(p) for p in re.findall(r'\d+', str(v_str))]
        return tuple(parts) if parts else (0,)
    except Exception:
        return (0,)


def get_releases_between(all_releases, current_ver, target_ver):
    """Return all releases strictly newer than current_ver up to target_ver (sorted newest first)."""
    curr_t = _version_tuple(current_ver)
    targ_t = _version_tuple(target_ver)
    matched = []
    seen = set()

    for item in all_releases or []:
        v = str(item.get('version', '')).strip()
        if not v or v in seen:
            continue
        v_t = _version_tuple(v)
        if curr_t < v_t <= targ_t:
            seen.add(v)
            matched.append(item)

    matched.sort(key=lambda r: _version_tuple(r.get('version', '')), reverse=True)
    return matched


def check_github_updates():
    current_ver = get_current_version()
    repo_url = os.environ.get('GITHUB_REPO', 'yrskrs/schooljournal')
    version_url = f"https://raw.githubusercontent.com/{repo_url}/main/VERSION"
    releases_url = f"https://raw.githubusercontent.com/{repo_url}/main/RELEASES.json"
    changelog_url = f"https://raw.githubusercontent.com/{repo_url}/main/CHANGELOG.md"

    headers = {'User-Agent': 'SchoolJournal-Updater/1.0'}
    try:
        req = urllib.request.Request(version_url, headers=headers)
        with urllib.request.urlopen(req, timeout=7) as resp:
            remote_ver = resp.read().decode('utf-8').strip()

        all_releases = []
        try:
            req_rel = urllib.request.Request(releases_url, headers=headers)
            with urllib.request.urlopen(req_rel, timeout=7) as resp_rel:
                all_releases = json.loads(resp_rel.read().decode('utf-8'))
        except Exception:
            all_releases = []

        if not all_releases:
            try:
                req_ch = urllib.request.Request(changelog_url, headers=headers)
                with urllib.request.urlopen(req_ch, timeout=7) as resp_ch:
                    all_releases = parse_changelog_markdown(resp_ch.read().decode('utf-8'))
            except Exception:
                local_changelog = ROOT / 'CHANGELOG.md'
                if local_changelog.exists():
                    all_releases = parse_changelog_markdown(local_changelog.read_text(encoding='utf-8'))

        has_update = _version_tuple(remote_ver) > _version_tuple(current_ver)
        releases_to_apply = get_releases_between(all_releases, current_ver, remote_ver)

        if has_update and not releases_to_apply:
            releases_to_apply = [{
                'version': remote_ver,
                'title': f'Оновлення {remote_ver}',
                'date': time.strftime('%Y-%m-%d'),
                'changes': ['Планове оновлення системи']
            }]

        target_release = releases_to_apply[0] if releases_to_apply else {
            'version': remote_ver,
            'title': f'Оновлення {remote_ver}',
            'date': time.strftime('%Y-%m-%d'),
            'changes': ['Планове оновлення системи']
        }

        return {
            'ok': True,
            'current_version': current_ver,
            'latest_version': remote_ver,
            'has_update': has_update,
            'releases': releases_to_apply,
            'releases_count': len(releases_to_apply),
            'total_changes_count': sum(len(r.get('changes', [])) for r in releases_to_apply),
            'release': target_release
        }
    except Exception as exc:
        return {
            'ok': False,
            'current_version': current_ver,
            'latest_version': current_ver,
            'has_update': False,
            'releases': [],
            'releases_count': 0,
            'error': f"Не вдалося зв'язатися з GitHub: {str(exc)}"
        }


def execute_update_job(target_ref='origin/main', target_ver=None):
    current_ver = get_current_version()
    check_info = {}
    try:
        check_info = check_github_updates()
    except Exception:
        pass

    target_display_ver = target_ver or check_info.get('latest_version') or 'останньої версії'
    releases_to_apply = check_info.get('releases') or []

    update_state(
        state='running',
        step=1,
        step_name='Отримання коду з GitHub',
        progress=10,
        logs=[],
        error=None,
        rolled_back=False,
        target_version=target_display_ver,
        current_version=current_ver,
        releases=releases_to_apply,
        started_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    )
    append_log(f"Розпочато оновлення з {current_ver} до {target_display_ver}")
    if releases_to_apply:
        version_list_str = ', '.join(r.get('version', '') for r in reversed(releases_to_apply))
        append_log(f"Зміни включають версії: {version_list_str} (усього: {len(releases_to_apply)})")

    backup_path = None
    try:
        # Ensure git safe directory
        subprocess.run(['git', 'config', '--global', '--add', 'safe.directory', '*'], check=False)

        # Step 1: fetch updates
        append_log("Крок 1/5: Завантаження коду з GitHub...")
        fetch_proc = subprocess.run(
            ['python3', str(ROOT / 'scripts' / 'fetch_update.py')],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60
        )
        if fetch_proc.returncode != 0:
            err_msg = fetch_proc.stderr or fetch_proc.stdout or 'Помилка git fetch'
            raise RuntimeError(f"Не вдалося завантажити оновлення з репозиторію: {err_msg}")
        append_log("Код успішно завантажено з GitHub.")

        update_state(step=2, step_name='Перевірка цільової ревізії та збірка образу', progress=30)
        append_log(f"Крок 2/5: Визначення цільової ревізії {target_ref}...")

        target_commit = subprocess.check_output(
            ['git', 'rev-parse', '--verify', f"{target_ref}^{{commit}}"],
            cwd=str(ROOT),
            text=True
        ).strip()
        current_commit = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(ROOT),
            text=True
        ).strip()
        append_log(f"Поточний коміт: {current_commit[:10]}, Цільовий коміт: {target_commit[:10]}")

        # Build candidate in separate worktree if not up to date
        candidate_image = f"schooljournal-candidate:{target_commit[:12]}"
        build_workdir = Path(f"/tmp/schooljournal-build.{target_commit[:8]}")
        if build_workdir.exists():
            subprocess.run(['rm', '-rf', str(build_workdir)], check=False)
        subprocess.run(['git', 'worktree', 'add', '--detach', str(build_workdir), target_commit], cwd=str(ROOT), check=True)

        try:
            append_log(f"Збірка Docker-образу кандидата {candidate_image}...")
            build_proc = subprocess.run(
                ['docker', 'build', '--build-arg', f'VCS_REF={target_commit}', '-t', candidate_image, str(build_workdir)],
                capture_output=True,
                text=True,
                timeout=600
            )
            if build_proc.returncode != 0:
                raise RuntimeError(f"Помилка збірки Docker-образу: {build_proc.stderr or build_proc.stdout}")
            append_log("Docker-образ кандидата успішно зібрано.")
        finally:
            subprocess.run(['git', 'worktree', 'remove', '--force', str(build_workdir)], cwd=str(ROOT), check=False)

        # Step 3: Quiesced backup
        update_state(step=3, step_name='Створення резервної копії перед оновленням', progress=55)
        append_log("Крок 3/5: Створення повного знімка бази даних, файлів та образу...")
        backup_proc = subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'backup.sh'), '--quiesced'],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180
        )
        if backup_proc.returncode != 0:
            raise RuntimeError(f"Помилка створення резервної копії: {backup_proc.stderr or backup_proc.stdout}")
        backup_path = backup_proc.stdout.strip().splitlines()[-1]
        append_log(f"Резервну копію створено: {Path(backup_path).name}")
        update_state(backup_file=backup_path)

        # Step 4: Merge, update release image, and restart
        update_state(step=4, step_name='Застосування міграцій та запуск оновленого сервісу', progress=75)
        append_log("Крок 4/5: Оновлення ревізії Git та конфігурації образу...")
        merge_proc = subprocess.run(
            ['git', 'merge', '--ff-only', target_commit],
            cwd=str(ROOT),
            capture_output=True,
            text=True
        )
        if merge_proc.returncode != 0:
            raise RuntimeError(f"Не вдалося виконати швидке злиття (fast-forward merge): {merge_proc.stderr}")

        # Update .release-image.yaml
        release_yaml = ROOT / '.release-image.yaml'
        release_yaml.write_text(f"services:\n  web:\n    image: {candidate_image}\n    pull_policy: never\n")

        # Update .env if present
        env_file = ROOT / '.env'
        if env_file.exists():
            new_ver = (ROOT / 'VERSION').read_text().strip()
            content = env_file.read_text()
            if 'JOURNAL_IMAGE=' in content:
                content = re.sub(r'JOURNAL_IMAGE=[^\n]*', f'JOURNAL_IMAGE={candidate_image}', content)
                env_file.write_text(content)

        append_log("Збір статичних файлів (collectstatic)...")
        subprocess.run(
            ['bash', '-c', 'source scripts/common.sh && dc run --rm --no-deps -T -e SKIP_INIT=1 web python manage.py collectstatic --noinput --clear'],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True
        )

        append_log("Запуск оновлених контейнерів...")
        subprocess.run(
            ['bash', '-c', 'source scripts/common.sh && dc up -d --wait --wait-timeout 180'],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True
        )
        # Nginx restart to refresh upstream IP
        subprocess.run(
            ['bash', '-c', 'source scripts/common.sh && dc restart nginx'],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False
        )

        # Step 5: Healthcheck
        update_state(step=5, step_name='Перевірка працездатності системи (/healthz)', progress=90)
        append_log("Крок 5/5: Перевірка працездатності оновленої системи...")

        healthy = False
        new_version = (ROOT / 'VERSION').read_text().strip()
        for attempt in range(15):
            time.sleep(2)
            try:
                with urllib.request.urlopen('http://127.0.0.1:8085/healthz/', timeout=4) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    if data.get('status') == 'ok':
                        healthy = True
                        break
            except Exception:
                pass

        if not healthy:
            raise RuntimeError("Сервіс не пройшов перевірку справності (/healthz/) після перезапуску.")

        append_log(f"Оновлення успішно завершено! Нова версія: {new_version}")
        update_state(
            state='success',
            step=5,
            step_name='Оновлення успішно завершено',
            progress=100,
            target_version=new_version,
            current_version=new_version
        )

    except Exception as exc:
        err_msg = str(exc)
        append_log(f"ПОМИЛКА ОНОВЛЕННЯ: {err_msg}")
        update_state(
            state='error',
            error=err_msg,
            step_name='Помилка під час оновлення'
        )

        if backup_path and Path(backup_path).exists():
            append_log(f"АВТОМАТИЧНЕ ВІДНОВЛЕННЯ: Повернення до резервної копії {Path(backup_path).name}...")
            try:
                restore_proc = subprocess.run(
                    ['bash', str(ROOT / 'scripts' / 'restore.sh'), backup_path, '--yes', '--no-safety-backup'],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if restore_proc.returncode == 0:
                    append_log("Систему успішно відновлено до попередньої стабільної версії з резервної копії.")
                    update_state(rolled_back=True)
                else:
                    append_log(f"Помилка автоматичного відновлення: {restore_proc.stderr}")
            except Exception as r_exc:
                append_log(f"Критична помилка при відновленні: {r_exc}")


class UpdaterHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/health', '/healthz'):
            self._send_json(200, {'status': 'ok', 'service': 'updater'})
        elif self.path in ('/status', '/api/status'):
            with STATE_LOCK:
                st = dict(CURRENT_STATE)
            self._send_json(200, st)
        elif self.path in ('/check', '/api/check'):
            result = check_github_updates()
            self._send_json(200, result)
        else:
            self._send_json(404, {'error': 'Not found'})

    def do_POST(self):
        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len).decode('utf-8') if content_len > 0 else '{}'
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if self.path in ('/check', '/api/check'):
            result = check_github_updates()
            self._send_json(200, result)

        elif self.path in ('/update', '/api/update'):
            with STATE_LOCK:
                if CURRENT_STATE.get('state') == 'running':
                    self._send_json(409, {'error': 'Оновлення вже виконується'})
                    return
            target_ref = data.get('ref', 'origin/main')
            target_ver = data.get('version')
            thread = threading.Thread(target=execute_update_job, args=(target_ref, target_ver), daemon=True)
            thread.start()
            self._send_json(200, {'ok': True, 'message': 'Оновлення розпочато', 'target_version': target_ver})

        elif self.path in ('/rollback', '/api/rollback'):
            backup = data.get('backup_file') or CURRENT_STATE.get('backup_file')
            if not backup or not Path(backup).exists():
                self._send_json(400, {'error': 'Файл резервної копії не знайдено'})
                return
            def run_rollback():
                append_log(f"Ручний запуск відновлення з {backup}...")
                proc = subprocess.run(
                    ['bash', str(ROOT / 'scripts' / 'restore.sh'), backup, '--yes', '--no-safety-backup'],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True
                )
                if proc.returncode == 0:
                    append_log("Відновлення успішно завершено.")
                    update_state(state='idle', rolled_back=True, error=None)
                else:
                    append_log(f"Помилка відновлення: {proc.stderr}")
            threading.Thread(target=run_rollback, daemon=True).start()
            self._send_json(200, {'ok': True, 'message': 'Відновлення запущено'})
        else:
            self._send_json(404, {'error': 'Not found'})

    def _send_json(self, status, payload):
        text = json.dumps(payload, ensure_ascii=False)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))

    def log_message(self, format, *args):
        # Silent or redirect to stdout
        pass


def run_server(port=8001):
    try:
        subprocess.run(['git', 'config', '--global', '--add', 'safe.directory', '*'], check=False)
    except Exception:
        pass
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(('0.0.0.0', port), UpdaterHTTPHandler) as httpd:
        print(f"Updater service listening on 0.0.0.0:{port}...")
        save_status()
        httpd.serve_forever()


if __name__ == '__main__':
    port = int(os.environ.get('UPDATER_PORT', 8001))
    run_server(port)
