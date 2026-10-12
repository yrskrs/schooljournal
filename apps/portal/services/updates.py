import json
import logging
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from django.conf import settings

logger = logging.getLogger(__name__)

UPDATER_HOST = os.environ.get('UPDATER_HOST', 'updater')
UPDATER_PORT = int(os.environ.get('UPDATER_PORT', 8001))
UPDATER_URL = f"http://{UPDATER_HOST}:{UPDATER_PORT}"
STATUS_FILE = Path(settings.BASE_DIR) / 'backups' / 'update_status.json'


def get_current_local_version():
    version_file = Path(settings.BASE_DIR) / 'VERSION'
    if version_file.exists():
        return version_file.read_text().strip()
    return '1.0.0'


def parse_version_tuple(version_str):
    try:
        parts = [int(p) for p in re.findall(r'\d+', str(version_str))]
        return tuple(parts) if parts else (0,)
    except Exception:
        return (0,)


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


def get_releases_between(all_releases, current_ver, target_ver):
    """Return all releases strictly newer than current_ver up to target_ver (sorted newest first)."""
    curr_t = parse_version_tuple(current_ver)
    targ_t = parse_version_tuple(target_ver)
    matched = []
    seen = set()

    for item in all_releases or []:
        v = str(item.get('version', '')).strip()
        if not v or v in seen:
            continue
        v_t = parse_version_tuple(v)
        if curr_t < v_t <= targ_t:
            seen.add(v)
            matched.append(item)

    matched.sort(key=lambda r: parse_version_tuple(r.get('version', '')), reverse=True)
    return matched


def check_updates_available():
    """Check for new versions on GitHub either via updater service or directly."""
    current_ver = get_current_local_version()

    # 1. Try updater service first
    try:
        req = urllib.request.Request(f"{UPDATER_URL}/api/check", data=b'{}', headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('ok'):
                # Ensure releases list is populated if has_update
                if data.get('has_update') and not data.get('releases') and data.get('release'):
                    data['releases'] = [data['release']]
                    data['releases_count'] = 1
                return data
    except Exception as exc:
        logger.debug(f"Updater service check unreachable, checking GitHub directly: {exc}")

    # 2. Fallback: query GitHub directly
    repo_url = os.environ.get('GITHUB_REPO', 'yrskrs/schooljournal')
    version_url = f"https://raw.githubusercontent.com/{repo_url}/main/VERSION"
    releases_url = f"https://raw.githubusercontent.com/{repo_url}/main/RELEASES.json"
    changelog_url = f"https://raw.githubusercontent.com/{repo_url}/main/CHANGELOG.md"
    headers = {'User-Agent': 'SchoolJournal-Django/1.0'}

    try:
        req_v = urllib.request.Request(version_url, headers=headers)
        with urllib.request.urlopen(req_v, timeout=6) as resp:
            remote_ver = resp.read().decode('utf-8').strip()

        all_releases = []
        # Try RELEASES.json
        try:
            req_r = urllib.request.Request(releases_url, headers=headers)
            with urllib.request.urlopen(req_r, timeout=6) as resp:
                all_releases = json.loads(resp.read().decode('utf-8'))
        except Exception:
            all_releases = []

        # If RELEASES.json is missing or incomplete, try CHANGELOG.md from GitHub or locally
        if not all_releases:
            try:
                req_c = urllib.request.Request(changelog_url, headers=headers)
                with urllib.request.urlopen(req_c, timeout=6) as resp:
                    all_releases = parse_changelog_markdown(resp.read().decode('utf-8'))
            except Exception:
                local_changelog = Path(settings.BASE_DIR) / 'CHANGELOG.md'
                if local_changelog.exists():
                    all_releases = parse_changelog_markdown(local_changelog.read_text(encoding='utf-8'))

        has_update = parse_version_tuple(remote_ver) > parse_version_tuple(current_ver)
        releases_to_apply = get_releases_between(all_releases, current_ver, remote_ver)

        if has_update and not releases_to_apply:
            releases_to_apply = [{
                'version': remote_ver,
                'title': f'Оновлення {remote_ver}',
                'date': '',
                'changes': ['Планове оновлення системи']
            }]

        target_release = releases_to_apply[0] if releases_to_apply else {
            'version': remote_ver,
            'title': f'Версія {remote_ver}',
            'date': '',
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
            'error': f"Не вдалося перевірити оновлення на GitHub: {str(exc)}"
        }


def get_update_status():
    """Retrieve current update progress from updater service or local status file."""
    # Try updater daemon
    try:
        req = urllib.request.Request(f"{UPDATER_URL}/api/status")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        pass

    # Fallback to status file in shared backups volume
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass

    return {
        'state': 'idle',
        'step': 0,
        'step_name': 'Готово до роботи',
        'progress': 0,
        'logs': [],
        'error': None,
        'rolled_back': False,
        'current_version': get_current_local_version()
    }


def start_update(target_ref='origin/main', target_ver=None):
    """Trigger the update process in the updater service."""
    payload = json.dumps({'ref': target_ref, 'version': target_ver}).encode('utf-8')
    req = urllib.request.Request(f"{UPDATER_URL}/api/update", data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as he:
        body = he.read().decode('utf-8')
        try:
            return json.loads(body)
        except Exception:
            return {'error': f"HTTP {he.code}: {body}"}
    except Exception as exc:
        return {'error': f"Сервіс оновлення недоступний: {str(exc)}"}


def start_rollback(backup_file=None):
    """Trigger rollback to previous backup."""
    payload = json.dumps({'backup_file': backup_file}).encode('utf-8')
    req = urllib.request.Request(f"{UPDATER_URL}/api/rollback", data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        return {'error': f"Сервіс оновлення недоступний: {str(exc)}"}
