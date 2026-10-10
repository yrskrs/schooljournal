#!/bin/sh
set -eu
if [ "${SKIP_INIT:-0}" != 1 ]; then
    if [ -n "${POSTGRES_HOST:-}" ]; then
        attempts=0
        until nc -z "$POSTGRES_HOST" "${POSTGRES_PORT:-5432}" 2>/dev/null; do
            attempts=$((attempts + 1))
            [ "$attempts" -lt 60 ] || { echo "PostgreSQL unavailable after 60 seconds" >&2; exit 1; }
            sleep 1
        done
    fi
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    if [ "${SEED_DEMO_DATA:-False}" = true ] || [ "${SEED_DEMO_DATA:-False}" = 1 ]; then
        python manage.py seed_demo_data
    fi
fi
exec "$@"
