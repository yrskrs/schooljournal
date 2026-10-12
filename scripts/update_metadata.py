"""Release metadata shared by the web app and its standalone updater (stdlib only)."""
import ast
import json
from pathlib import Path
import re
import urllib.request


def version_tuple(value):
    return tuple(int(p) for p in re.findall(r'\d+', str(value))) or (0,)


def read_version(root):
    root = Path(root)
    if (root / 'VERSION').exists():
        return (root / 'VERSION').read_text(encoding='utf-8').strip()
    tree = ast.parse((root / 'app/version.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            return str(ast.literal_eval(node.value))
    raise ValueError('Не знайдено встановлену версію')


def parse_changelog_markdown(text):
    releases, current = [], None
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^##\s+\[?v?(\d+\.\d+(?:\.\d+)*)\]?(.*)$', line)
        if match:
            if current:
                releases.append(current)
            version, rest = match.groups()
            rest = re.sub(r'^\s*[-–—:]\s*', '', rest).strip()
            date = re.match(r'^(\d{4}-\d{2}-\d{2})(?:\s*[-–—:]\s*)?(.*)$', rest)
            current = {'version': version, 'date': date[1] if date else '',
                       'title': (date[2] if date else rest) or f'Оновлення {version}', 'changes': []}
        elif current and line.startswith(('- ', '* ')):
            current['changes'].append(line[2:].strip())
    if current:
        releases.append(current)
    return releases


def releases_between(releases, current_ver, target_ver):
    unique = {r['version']: r for r in releases if isinstance(r, dict) and r.get('version')
              and version_tuple(current_ver) < version_tuple(r['version']) <= version_tuple(target_ver)}
    return sorted(unique.values(), key=lambda r: version_tuple(r['version']), reverse=True)


def local_release(root, version):
    root = Path(root)
    try:
        releases = json.loads((root / 'RELEASES.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        try:
            releases = parse_changelog_markdown((root / 'CHANGELOG.md').read_text(encoding='utf-8'))
        except OSError:
            releases = []
    return next((r for r in releases if r.get('version') == version),
                {'version': version, 'title': f'Версія {version}', 'changes': []})


def check_remote(root, repo, current_ver, branch='main'):
    """Read GitHub version and release notes; network failures stay failures."""
    headers = {'User-Agent': 'School-Updater/2.0', 'Cache-Control': 'no-cache'}
    def fetch(path):
        req = urllib.request.Request(f'https://raw.githubusercontent.com/{repo}/{branch}/{path}', headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.read().decode('utf-8')
    try:
        try:
            remote_ver = fetch('VERSION').strip()
        except Exception:
            text = fetch('app/version.py')
            match = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
            if not match:
                raise ValueError('GitHub не повернув номер версії')
            remote_ver = match[1]
        if not re.fullmatch(r'\d+\.\d+\.\d+', remote_ver):
            raise ValueError('Некоректна версія на GitHub')
        try:
            releases = json.loads(fetch('RELEASES.json'))
            if not isinstance(releases, list) or any(not isinstance(r, dict) or not r.get('version') for r in releases):
                raise ValueError('Некоректний RELEASES.json')
        except Exception:
            releases = parse_changelog_markdown(fetch('CHANGELOG.md'))
        target = next((r for r in releases if r.get('version') == remote_ver), None)
        if not target:
            raise ValueError(f'На GitHub немає опису версії {remote_ver}')
        has_update = version_tuple(remote_ver) > version_tuple(current_ver)
        between = releases_between(releases, current_ver, remote_ver) if has_update else []
        return {'ok': True, 'current_version': current_ver, 'latest_version': remote_ver,
                'has_update': has_update, 'releases': between, 'releases_between': between,
                'releases_count': len(between), 'total_intermediate_versions': len(between),
                'total_changes_count': sum(len(r.get('changes', [])) for r in between),
                'release': target, 'target_release': target,
                'current_release': local_release(root, current_ver)}
    except Exception as exc:
        return {'ok': False, 'current_version': current_ver, 'has_update': False,
                'releases': [], 'releases_between': [],
                'error': f'Не вдалося перевірити GitHub: {exc}'}
