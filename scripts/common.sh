#!/usr/bin/env bash
# Спільні функції. .env читає Docker Compose, ніколи не shell source.
set -Eeuo pipefail
umask 077
JOURNAL_ROOT="${JOURNAL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$JOURNAL_ROOT"
export ENV_FILE="${ENV_FILE:-$JOURNAL_ROOT/.env}"
command -v docker >/dev/null || { echo "Install Docker and Docker Compose v2." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Install python3." >&2; exit 1; }
docker compose version >/dev/null
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE. Run ./setup.sh." >&2; exit 1; }

dc() {
    local -a options=(--env-file "$ENV_FILE" -f "$JOURNAL_ROOT/compose.yaml")
    [[ ! -f "$JOURNAL_ROOT/.release-image.yaml" ]] || options+=(-f "$JOURNAL_ROOT/.release-image.yaml")
    docker compose "${options[@]}" "$@"
}
lock_maintenance() {
    if [[ "${MAINTENANCE_LOCK_HELD:-0}" != 1 ]]; then
        command -v flock >/dev/null || { echo "Install util-linux (flock)." >&2; exit 1; }
        exec 9>"$JOURNAL_ROOT/.maintenance.lock"
        flock -n 9 || { echo "Another maintenance operation is running." >&2; exit 1; }
        export MAINTENANCE_LOCK_HELD=1
    fi
}
healthy() {
    dc up -d --wait --wait-timeout "${HEALTH_WAIT_TIMEOUT:-180}" || return
    dc exec -T -e EXPECTED_VERSION="$(cat "$JOURNAL_ROOT/VERSION")" web python -c 'import json, os, urllib.request; from pathlib import Path; d=json.load(urllib.request.urlopen("http://localhost:8000/healthz/")); assert d["status"] == "ok" and d["version"] == Path("/app/VERSION").read_text().strip() == os.environ["EXPECTED_VERSION"]'
}
