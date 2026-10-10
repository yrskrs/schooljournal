#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 - "$@" <<'PY'
import argparse
from pathlib import Path
import os
import re
import secrets
import tempfile
import shlex
from urllib.parse import urlsplit
parser = argparse.ArgumentParser(description='Prepare journal configuration without changing existing secrets.')
parser.add_argument('--public-url', help='Browser URL, e.g. http://192.168.1.13:8085')
args = parser.parse_args()
public_host = public_origin = None
if args.public_url:
    try:
        url = urlsplit(args.public_url)
        port = url.port
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('Expected an HTTP(S) origin without credentials, path, query or fragment.')
        public_host = '[' + url.hostname + ']' if ':' in url.hostname else url.hostname
        public_origin = url.scheme + '://' + public_host + (':' + str(port) if port else '')
    except ValueError as exc:
        parser.error(str(exc))
env = Path('.env')
if not env.exists():
    text = Path('.env.example').read_text()
    for name, size in [('SECRET_KEY', 48), ('POSTGRES_PASSWORD', 32), ('INITIAL_SETUP_TOKEN', 32)]:
        text = text.replace(name + '=\n', name + '=' + secrets.token_hex(size) + '\n')
else:
    text = env.read_text()
    assignments = list(re.finditer(r'(?m)^[ \t]*(?:export[ \t]+)?INITIAL_SETUP_TOKEN[ \t]*=[^\n]*', text))
    if assignments:
        assignment = assignments[-1]
        value = assignment.group().split('=', 1)[1].strip()
        if value and not value.startswith('#') and not re.fullmatch(r"(?:\"\"|'')[ \t]*(?:#.*)?", value):
            pass
        else:
            replacement = 'INITIAL_SETUP_TOKEN=' + secrets.token_hex(32)
            text = text[:assignment.start()] + replacement + text[assignment.end():]
    else:
        text = text.rstrip('\n') + '\nINITIAL_SETUP_TOKEN=' + secrets.token_hex(32) + '\n'
if public_host:
    for name, entry in [('ALLOWED_HOSTS', public_host), ('CSRF_TRUSTED_ORIGINS', public_origin)]:
        assignments = list(re.finditer(r'(?m)^[ \t]*(?:export[ \t]+)?' + name + r'[ \t]*=[^\n]*', text))
        assignment = assignments[-1] if assignments else None
        raw = assignment.group().split('=', 1)[1].strip() if assignment else ''
        parsed = shlex.split(raw, comments=True)
        entries = [item.strip() for item in ','.join(parsed).split(',') if item.strip()]
        if entry not in entries:
            entries.append(entry)
        replacement = name + '=' + ','.join(entries)
        text = text[:assignment.start()] + replacement + text[assignment.end():] if assignment else text.rstrip('\n') + '\n' + replacement + '\n'
temporary = None
try:
    with tempfile.NamedTemporaryFile(mode='w', dir=env.parent, prefix='.env.setup-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(text)
    temporary.chmod(0o600)
    os.replace(temporary, env)
finally:
    if temporary and temporary.exists():
        temporary.unlink()
print('.env prepared; existing secrets and settings were preserved. Recreate web to apply changes.')
PY
