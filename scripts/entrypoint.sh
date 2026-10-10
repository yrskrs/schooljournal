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
    if [ -n "${FIRST_SUPERADMIN_USERNAME:-}" ] && [ -n "${FIRST_SUPERADMIN_PASSWORD:-}" ]; then
        python - <<'PY'
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.contrib.auth.password_validation import validate_password
from apps.accounts.models import User
username = os.environ['FIRST_SUPERADMIN_USERNAME']
if not User.objects.filter(username=username).exists():
    user = User(username=username, email=os.environ.get('FIRST_SUPERADMIN_EMAIL', ''), role=User.Role.SUPERADMIN)
    validate_password(os.environ['FIRST_SUPERADMIN_PASSWORD'], user)
    User.objects.create_superuser(username=username, email=user.email,
        password=os.environ['FIRST_SUPERADMIN_PASSWORD'], role=User.Role.SUPERADMIN)
    print('Initial superadministrator created.')
PY
    fi
fi
exec "$@"
