#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
main() {
    lock_maintenance
    quiesced=0
    local archive web_id image_id
    [[ "${1:-}" != --quiesced ]] || quiesced=1
    [[ $# -eq 0 || ( $# -eq 1 && $quiesced -eq 1 ) ]] || { echo "Usage: ./backup.sh [--quiesced]" >&2; return 2; }
    mkdir -p "$JOURNAL_ROOT/backups"
    work=$(mktemp -d "$JOURNAL_ROOT/backups/.snapshot.XXXXXX")
    archive="$JOURNAL_ROOT/backups/schooljournal-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
    cleanup_backup() {
        status=$?
        trap - EXIT
        rm -rf -- "$work"
        if [[ $quiesced -eq 0 ]]; then
            dc start --wait --wait-timeout 180 web nginx >&2 || status=1
        fi
        exit "$status"
    }
    trap cleanup_backup EXIT
    web_id=$(dc ps -aq web)
    [[ -n "$web_id" ]] || { echo "No deployed web container; start the service first." >&2; return 1; }
    image_id=${BACKUP_IMAGE:-$(docker inspect --format '{{.Image}}' "$web_id")}
    if [[ $quiesced -eq 0 ]]; then dc stop nginx web >&2; fi
    echo "Backing up PostgreSQL, media, source and the deployed image..." >&2
    dc exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl' > "$work/database.dump"
    dc exec -T db pg_restore --list < "$work/database.dump" > /dev/null
    dc run --rm --no-deps -T --user 0 --entrypoint tar web -C /app/media -czf - . > "$work/media.tar.gz"
    docker image save "$image_id" | gzip > "$work/web-image.tar.gz"
    python3 "$JOURNAL_ROOT/scripts/archive.py" create "$work" "$JOURNAL_ROOT" "$image_id"
    tar -czf "$archive.partial" -C "$work" database.dump media.tar.gz source.tar.gz web-image.tar.gz manifest.json SHA256SUMS
    python3 "$JOURNAL_ROOT/scripts/archive.py" check "$archive.partial"
    mv -- "$archive.partial" "$archive"
    echo "$archive"
}
main "$@"
