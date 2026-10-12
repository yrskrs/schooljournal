#!/usr/bin/env bash
set -Eeuo pipefail
# --check не потребує Docker чи .env.
if [[ "${1:-}" == --check && $# -eq 2 ]]; then
    exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/archive.py" check "$2"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
main() {
    local archive commit image_tag safety keep_code=0 no_safety=0
    restore_database="journal_restore_$(date +%s)_$$"
    previous_database="journal_previous_$(date +%s)_$$"
    [[ $# -ge 1 && -f "$1" ]] || { echo "Usage: ./restore.sh ARCHIVE --yes [--no-safety-backup]; or ./restore.sh --check ARCHIVE" >&2; return 2; }
    archive=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1")
    shift
    [[ "${1:-}" == --yes ]] || { echo "Restore replaces database, media and service code. Add --yes after reviewing the archive." >&2; return 2; }
    shift
    for option in "$@"; do
        case "$option" in
            --no-safety-backup) no_safety=1;;
            --keep-code) keep_code=1;;
            *) return 2;;
        esac
    done
    lock_maintenance
    work=$(mktemp -d "$JOURNAL_ROOT/backups/.restore.XXXXXX")
    cleanup_restore() { status=$?; trap - EXIT; rm -rf -- "$work"; if [[ $status -ne 0 ]]; then echo "Restore failed. Web remains stopped if restoration began. Use the safety backup to recover." >&2; fi; exit "$status"; }
    trap cleanup_restore EXIT
    python3 "$JOURNAL_ROOT/scripts/archive.py" extract "$archive" "$work"
    dc exec -T db pg_restore --list < "$work/database.dump" > /dev/null
    gzip -dc "$work/web-image.tar.gz" | docker image load >&2
    if [[ $no_safety -eq 0 ]]; then
        safety=$("$JOURNAL_ROOT/scripts/backup.sh")
        echo "Safety backup: $safety" >&2
    fi
    dc stop nginx web
    # Порожня тимчасова база також прибирає таблиці, яких немає у старому знімку.
    dc exec -T -e RESTORE_DATABASE="$restore_database" db sh -c 'exec createdb -U "$POSTGRES_USER" --template=template0 "$RESTORE_DATABASE"'
    dc exec -T -e RESTORE_DATABASE="$restore_database" db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$RESTORE_DATABASE" --exit-on-error --single-transaction --no-owner --no-acl' < "$work/database.dump"
    dc exec -T -e RESTORE_DATABASE="$restore_database" -e PREVIOUS_DATABASE="$previous_database" db sh -c 'exec psql -U "$POSTGRES_USER" -d postgres --single-transaction -v ON_ERROR_STOP=1 -v original="$POSTGRES_DB" -v replacement="$RESTORE_DATABASE" -v previous="$PREVIOUS_DATABASE"' <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :'original' AND pid <> pg_backend_pid();
ALTER DATABASE :"original" RENAME TO :"previous";
ALTER DATABASE :"replacement" RENAME TO :"original";
SQL
    # Точна заміна media; вміст архіву перевірено до зупинки сервісу.
    dc run --rm --no-deps -T --user 0 --entrypoint sh web -c 'find /app/media -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; exec tar -xzf - -C /app/media' < "$work/media.tar.gz"
    commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$work/manifest.json")
    if [[ $keep_code -eq 0 && "$commit" =~ ^[0-9a-f]{40}$ ]] && git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git cat-file -e "$commit^{commit}" 2>/dev/null; then
        git reset --hard "$commit"
    fi
    if [[ $keep_code -eq 0 ]]; then python3 "$JOURNAL_ROOT/scripts/archive.py" install-source "$work/source.tar.gz" "$JOURNAL_ROOT"; fi
    image_tag=$(python3 "$JOURNAL_ROOT/scripts/archive.py" tag "$work/manifest.json")
    docker image tag "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$work/manifest.json")" "$image_tag"
    printf 'services:\n  web:\n    image: %s\n    pull_policy: never\n' "$image_tag" > "$JOURNAL_ROOT/.release-image.yaml"
    # Static volume може містити новішу версію — перебудовуємо з відновленого образу.
    dc run --rm --no-deps -T -e SKIP_INIT=1 web python manage.py collectstatic --noinput --clear
    healthy
    dc exec -T -e PREVIOUS_DATABASE="$previous_database" db sh -c 'exec dropdb -U "$POSTGRES_USER" "$PREVIOUS_DATABASE"'
    echo "Restored successfully. Version: $(cat "$JOURNAL_ROOT/VERSION")"
}
mkdir -p "$JOURNAL_ROOT/backups"
main "$@"
