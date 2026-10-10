#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ ! -e .env ]] || { echo ".env already exists; it was not overwritten."; exit 0; }
python3 - <<'PY'
from pathlib import Path
import secrets
text = Path('.env.example').read_text()
text = text.replace('SECRET_KEY=\n', 'SECRET_KEY=' + secrets.token_hex(48) + '\n')
text = text.replace('POSTGRES_PASSWORD=\n', 'POSTGRES_PASSWORD=' + secrets.token_hex(32) + '\n')
with open('.env', 'x') as stream:
    stream.write(text)
Path('.env').chmod(0o600)
PY
echo ".env created with unique secrets. Set your host, URL and HTTPS options before starting Docker."
