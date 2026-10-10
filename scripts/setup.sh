#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 - <<'PY'
from pathlib import Path
import os
import re
import secrets
import tempfile
env = Path('.env')
if not env.exists():
    text = Path('.env.example').read_text()
    for name, size in [('SECRET_KEY', 48), ('POSTGRES_PASSWORD', 32), ('INITIAL_SETUP_TOKEN', 32)]:
        text = text.replace(name + '=\n', name + '=' + secrets.token_hex(size) + '\n')
    with env.open('x') as stream:
        stream.write(text)
    env.chmod(0o600)
    print('.env created with unique secrets. Set your host, URL and HTTPS options before starting Docker.')
else:
    text = env.read_text()
    assignments = list(re.finditer(r'(?m)^[ \t]*(?:export[ \t]+)?INITIAL_SETUP_TOKEN[ \t]*=[^\n]*', text))
    if assignments:
        assignment = assignments[-1]
        value = assignment.group().split('=', 1)[1].strip()
        if value and not value.startswith('#') and not re.fullmatch(r"(?:\"\"|'')[ \t]*(?:#.*)?", value):
            print('.env already exists; its settings and setup code were preserved.')
            raise SystemExit(0)
        replacement = 'INITIAL_SETUP_TOKEN=' + secrets.token_hex(32)
        text = text[:assignment.start()] + replacement + text[assignment.end():]
    else:
        text = text.rstrip('\n') + '\nINITIAL_SETUP_TOKEN=' + secrets.token_hex(32) + '\n'
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
    print('INITIAL_SETUP_TOKEN added to the existing .env; other settings were preserved.')
PY
