# Синхронізація, прокрутка та індикатори — Docker-перевірки

Окремий проєкт `schooljournal-ux-checks`, PostgreSQL у tmpfs, лише вигадані дані. Робочі `.env`, БД і томи не використовуються. Потрібні локальні образи тестів 1.5.6 та завдань 4.4.3 (або явно задані CHECK_TEST_IMAGE / CHECK_WORK_IMAGE). Порти 19100/19180/19185/19186 доступні лише через 127.0.0.1.

З кореня репозиторію:

```bash
docker build -t schooljournal-ux-check:1.3.12 --build-arg APP_VERSION=1.3.12 .
docker compose -f tests/ux/compose.yaml up -d --wait --wait-timeout 120
python tests/ux/http_check.py
docker compose -f tests/ux/compose.yaml exec -T journal python manage.py test tests school_sync.tests --noinput
docker compose -f tests/ux/compose.yaml exec -T journal python manage.py shell < tests/ux/seed_ui.py
python tests/ux/roster_check.py
```

`http_check.py` запускається на свіжих тестових БД. Він перевіряє підключення трьох незалежних сервісів, CSRF/права, повтори, ручні оцінки, помилки шкали, offline/retry, конфлікти, зміну IP, перезапуски та пагінацію. `roster_check.py` створює окремий клас і перевіряє виключення нового ігнорованого учня, деактивацію раніше переданого, відновлення зі збереженням ID та відсутність дублів в обох джерелах.

`seed_ui.py` виводить ID окремого журналу; у наведеному порядку це 2. Для іншого порядку передайте фактичний ID через UX_JOURNAL_ID. Браузерні тести працюють у Chromium Docker:

```bash
mkdir -p /tmp/schooljournal-ux-artifacts
docker run --rm --network host \
  -v "$PWD/tests/ux:/checks:ro" \
  -v /tmp/schooljournal-ux-artifacts:/artifacts:rw \
  -e UX_JOURNAL_ID=2 \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && NODE_PATH=/tmp/pw/node_modules node /checks/browser_check.cjs'
```

Браузер перевіряє реальні HTTP-збереження, видалення, HTMX та ігнорування; прокрутку сторінки/таблиці, пошук, мобільне/настільне меню, скасування попереднього перегляду й відмову мережі. Запит до nz.ua замінено синтетичною відповіддю; серверний контракт nz.ua окремо перевіряється Django-тестами. Знімки 390×844 та 1440×900 зберігаються в `/tmp/schooljournal-ux-artifacts/`.

Перевірки клієнтського життєвого циклу в Node Docker:

```bash
docker run --rm -v "$PWD/static:/checks/static:ro" -v "$PWD/tests:/checks/tests:ro" -w /checks node:22 node tests/site_lifecycle_check.js
```

Після завершення:

```bash
docker compose -f tests/ux/compose.yaml down
```

Ця команда прибирає лише тестовий проєкт. Робочі інсталяції журналу, тестів та завдань залишаються окремими.

## Порожній «Ваш клас» та особистий профіль nz.ua

Окрема перевірка відтворює звичайні серверні налаштування без фонового обміну. Виконуйте на свіжих тестових БД після завершення попереднього проєкту:

```bash
docker build -t schooljournal-roster-check:1.3.12 --build-arg APP_VERSION=1.3.12 .
docker compose -f tests/ux/compose.yaml -f tests/ux/roster_page.compose.yaml up -d --wait journal nginx
docker compose -f tests/ux/compose.yaml -f tests/ux/roster_page.compose.yaml exec -T journal python manage.py shell < tests/ux/seed_roster_page.py
docker run --rm --network host \
  -v "$PWD/tests/ux:/checks:ro" \
  -v /tmp/schooljournal-ux-artifacts:/artifacts:rw \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && NODE_PATH=/tmp/pw/node_modules node /checks/roster_page_check.cjs'
docker compose -f tests/ux/compose.yaml -f tests/ux/roster_page.compose.yaml down
```

Перевіряються два доступні класи локального вчителя, відсутність чужого класу, вибір предмета, перехід до підключення, кількість ігнорованих, мобільна розмітка та власний клас особистого nz.ua-профілю без локальної школи. Знімки — `roster-desktop.png` та `roster-mobile.png` у каталозі артефактів.
