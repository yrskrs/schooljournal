# =========================================================================
# Робочий Dockerfile для вебзастосунку «Електронний шкільний журнал»
# =========================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Системні пакети та шрифти для формування звітів
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-traditional \
    libpq-dev \
    gcc \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Встановлення фіксованих залежностей Python
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копіюємо лише код; робочі бази, імпортовані документи й секрети залишаються локально.
COPY manage.py VERSION RELEASES.json LICENSE setup.sh .env.example /app/
COPY school_sync/ /app/school_sync/
COPY gunicorn.conf.py /app/
COPY config/ /app/config/
COPY apps/ /app/apps/
COPY static/ /app/static/
COPY templates/ /app/templates/
COPY tests/ /app/tests/
COPY scripts/ /app/scripts/

# Створення директорій для статики та медіа
RUN mkdir -p /app/staticfiles /app/media /app/backups /app/samples

# Безпечний непривілейований користувач
RUN useradd -u 1001 -m -s /bin/bash journaluser && \
    chown -R journaluser:journaluser /app && \
    chmod +x /app/scripts/*.sh

ARG VCS_REF=local
LABEL org.opencontainers.image.source="https://github.com/yrskrs/schooljournal" \
      org.opencontainers.image.revision=$VCS_REF

USER journaluser

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--config", "/app/gunicorn.conf.py", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", "--timeout", "120"]
ARG APP_VERSION=1.3.22
LABEL org.opencontainers.image.version=$APP_VERSION
