#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
main() {
    local ref="${1:-origin/main}" old_commit target
    backup=''
    update_started=0
    [[ $# -le 1 && "$ref" != -* ]] || { echo "Usage: ./update.sh [origin/main|origin/release/v1.0.0|TAG]" >&2; return 2; }
    lock_maintenance
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Deploy from a Git clone; this folder has no Git metadata." >&2; return 1; }
    [[ -z "$(git status --porcelain --untracked-files=normal)" ]] || { echo "Commit or stash local source changes before updating." >&2; return 1; }
    old_commit=$(git rev-parse HEAD)
    git fetch --prune origin
    target=$(git rev-parse --verify "$ref^{commit}")
    git merge-base --is-ancestor "$old_commit" "$target" || { echo "Target is not a forward update. Use restore.sh for rollback." >&2; return 1; }
    [[ "$old_commit" != "$target" ]] || { echo "Already at requested revision."; return 0; }
    # Збірка в окремому worktree: робочий сервіс ще використовує попередній код.
    workdir=$(mktemp -d /tmp/schooljournal-build.XXXXXX)
    git worktree add --detach "$workdir" "$target"
    cleanup_update() {
        status=$?
        trap - EXIT
        git worktree remove --force "$workdir" >&2 || status=1
        if [[ $status -ne 0 && $update_started -eq 1 ]]; then
            if [[ -n "$backup" ]]; then
                echo "Update failed; restoring the pre-update snapshot." >&2
                if ! "$JOURNAL_ROOT/scripts/restore.sh" "$backup" --yes --no-safety-backup; then
                    echo "Automatic recovery failed. Web remains stopped; recover with: ./restore.sh $backup --yes" >&2
                fi
            fi
        fi
        exit "$status"
    }
    trap cleanup_update EXIT
    local candidate="schooljournal-candidate:${target:0:12}"
    docker build --build-arg VCS_REF="$target" -t "$candidate" "$workdir"
    docker run --rm --env-file "$ENV_FILE" -e SKIP_INIT=1 --entrypoint python "$candidate" manage.py check
    # Закриваємо запис перед узгодженою резервною копією.
    dc stop nginx web
    if ! backup=$("$JOURNAL_ROOT/scripts/backup.sh" --quiesced); then
        dc start --wait --wait-timeout 180 web nginx
        echo "Backup failed; source and database were not updated." >&2
        return 1
    fi
    update_started=1
    echo "Pre-update backup: $backup"
    git merge --ff-only "$target"
    rm -f -- "$JOURNAL_ROOT/.release-image.yaml"
    printf 'services:\n  web:\n    image: %s\n    pull_policy: never\n' "$candidate" > "$JOURNAL_ROOT/.release-image.yaml"
    dc run --rm --no-deps -T -e SKIP_INIT=1 web python manage.py collectstatic --noinput --clear
    # Міграції виконує entrypoint. За будь-якої помилки автоматично повертаємо повний знімок.
    if ! healthy; then
        echo "Deployment unhealthy." >&2
        return 1
    fi
    update_started=0
    echo "Updated successfully. Version: $(cat "$JOURNAL_ROOT/VERSION")"
}
main "$@"
