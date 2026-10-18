from scripts.update_metadata import check_remote, local_release
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
    current_ver = get_current_local_version()
    payload = json.dumps({'current_version': current_ver}).encode('utf-8')
    try:
        req = urllib.request.Request(f"{UPDATER_URL}/api/check", data=payload,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if isinstance(data, dict) and 'ok' in data:
            return data
    except Exception as exc:
        logger.debug("Updater check unavailable: %s", exc)
    return check_remote(Path(settings.BASE_DIR), os.environ.get('GITHUB_REPO', 'yrskrs/schooljournal'), current_ver)


def get_current_release():
    return local_release(Path(settings.BASE_DIR), get_current_local_version())


def get_update_status():
    """Retrieve current update progress from updater service or local status file."""
    local_ver = get_current_local_version()
    # Try updater daemon
    try:
        req = urllib.request.Request(f"{UPDATER_URL}/api/status")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if isinstance(data, dict):
                data['current_version'] = local_ver
                return data
    except Exception:
        pass

    # Fallback to status file in shared backups volume
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                data['current_version'] = local_ver
                return data
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
        'current_version': local_ver
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
