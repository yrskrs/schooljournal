# Синхронізація, прокрутка та індикатори — Docker-перевірки

## Вибір уроку для пізніх результатів — 1.3.36

Стенд створює тимчасову SQLite у `/tmp`, вигаданого вчителя й учнів, уроки
та вже імпортовану оцінку. Відповіді API підмінено; робочі `.env`, бази й
томи не використовуються. Перевіряються окремий попередній перегляд,
вибір старішого уроку поза періодом завершення робіт, імпорт лише потрібної
сесії, збереження наявної оцінки й наступний автоматичний імпорт на обраний
урок. Екрани: 1440×1000 та 390×844.

Перший термінал:

```bash
mkdir -p /tmp/schooljournal-grade-placement-artifacts
GRADE_PLACEMENT_FIXTURE=/tmp/schooljournal-grade-placement-artifacts/grade-placement-browser-fixture.json \
  .venv/bin/python tests/ux/grade_placement_server.py
```

Другий термінал після повідомлення про готовність:

```bash
docker run --rm --network host --security-opt label=disable \
  -v "$PWD/tests/ux:/checks:ro" \
  -v /tmp/schooljournal-grade-placement-artifacts:/artifacts:rw \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && NODE_PATH=/tmp/pw/node_modules node /checks/grade_placement_check.cjs'
```

Очікується 28 успішних перевірок. Знімки залишаються в каталозі артефактів.
Зупиніть сервер через Ctrl+C для видалення його тимчасової бази й сесії.
Серверні регресії: `tests/test_grade_placement.py`; результати:
[GRADE_PLACEMENT_CHECKS.md](../../GRADE_PLACEMENT_CHECKS.md).

## API та сповіщення — 1.3.34

Окремий сценарій запускає тимчасовий Django-сервер на 127.0.0.1:19189. Використовуються `config.test_settings`, SQLite у `/tmp`, вигадані користувачі й підмінені відповіді джерел. Робочий `.env` та бази не читаються. Перевіряються простий вигляд API, дві адреси одного сайту, точні переходи зі сповіщень, розгортання блоків, пріоритет цілі над збереженою прокруткою, вирішення повідомлень та інструкція на великому й мобільному екранах.

У першому терміналі:

```bash
mkdir -p /tmp/schooljournal-api-workspace-artifacts
API_WORKSPACE_FIXTURE=/tmp/schooljournal-api-workspace-artifacts/api-workspace-browser-fixture.json \
  .venv/bin/python tests/ux/api_workspace_server.py
```

Після повідомлення `API workspace synthetic fixture ready` у другому терміналі:

```bash
docker run --rm --network host \
  -v "$PWD/static:/checks/static:ro" -v "$PWD/tests:/checks/tests:ro" \
  -v /tmp/schooljournal-api-workspace-artifacts:/artifacts:rw -w /checks \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && node tests/site_lifecycle_check.js && node tests/site_sync_response_check.cjs && NODE_PATH=/tmp/pw/node_modules node tests/ux/api_workspace_check.cjs'
```

Після завершення зупиніть перший термінал через Ctrl+C: тимчасова БД і сесія приберуться. Для повторного проходу запустіть сервер знову, щоб відновити початкові невирішені сповіщення. Знімки залишаються в каталозі артефактів. Серверні регресії: `tests/test_api_workspace.py`; результати: `API_WORKSPACE_CHECKS.md`.

## Кнопки оцінок і списків — 1.3.32

Окремий сценарій запускає справжні Django-сторінки й POST-маршрути на 127.0.0.1:19187. База тимчасова, налаштування `config.test_settings` не читають робочий `.env`, обидва джерела замінені вигаданими відповідями. Підтвердження списку з двома конфліктами не підміняє повідомлення про оцінки. Чотири кнопки перевіряються на ширинах 1440 і 390 пікселів.

В одному терміналі:

```bash
mkdir -p /tmp/schooljournal-grade-feedback-artifacts
GRADE_FEEDBACK_FIXTURE=/tmp/schooljournal-grade-feedback-artifacts/grade-feedback-browser-fixture.json \
  .venv/bin/python tests/ux/grade_feedback_server.py
```

В іншому терміналі:

```bash
docker run --rm --network host \
  -v "$PWD/tests/ux:/checks:ro" \
  -v /tmp/schooljournal-grade-feedback-artifacts:/artifacts:rw \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && NODE_PATH=/tmp/pw/node_modules node /checks/grade_feedback_check.cjs'
```

Після перевірки зупиніть перший термінал через Ctrl+C. Сервер прибере тимчасову БД і синтетичну сесію. Знімки містять лише вигадані дані.

## Обмін між трьома сервісами

Окремий проєкт `schooljournal-ux-checks`, PostgreSQL у tmpfs, лише вигадані дані. Робочі `.env`, БД і томи не використовуються. Потрібні локальні образи тестів 1.6.7 та завдань 4.5.6 (або явно задані CHECK_TEST_IMAGE / CHECK_WORK_IMAGE). Порти 19100/19180/19185/19186 доступні лише через 127.0.0.1.

З кореня репозиторію:

```bash
docker build -t schooljournal-web:1.3.30 --build-arg APP_VERSION=1.3.30 .
docker compose -f tests/ux/compose.yaml up -d --wait --wait-timeout 120
python tests/ux/http_check.py
docker compose -f tests/ux/compose.yaml exec -T journal python manage.py test tests school_sync.tests --noinput
docker compose -f tests/ux/compose.yaml exec -T journal python manage.py shell < tests/ux/seed_ui.py
python tests/ux/roster_check.py
```

`http_check.py` запускається на свіжих тестових БД. Він перевіряє підключення трьох незалежних сервісів, CSRF/права, повтори, ручні оцінки, помилки шкали, offline/retry, конфлікти, зміну IP, перезапуски та пагінацію. `roster_check.py` створює окремий клас і перевіряє виключення нового ігнорованого учня, деактивацію раніше переданого, відновлення зі збереженням ID та відсутність дублів в обох джерелах. Додатково перевіряються саме сторінка вчителя `/teacher/students/?class_group=…`, її заголовок і лічильник, CSV, API, список тестів та повторне застосування ігнорування звичайною синхронізацією оцінок.

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
docker build -t schooljournal-roster-check:1.3.30 --build-arg APP_VERSION=1.3.30 .
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


## Браузерна регресія ігнорування

Після `http_check.py` та `roster_check.py`:

```bash
mkdir -p /tmp/schooljournal-ux-artifacts
cp /tmp/schooljournal-ignore-fixture.json /tmp/schooljournal-ux-artifacts/ignore-fixture.json
docker run --rm --network host \
  -v "$PWD/tests/ux:/checks:ro" \
  -v /tmp/schooljournal-ux-artifacts:/artifacts:rw \
  --entrypoint bash mcr.microsoft.com/playwright:v1.56.1-noble \
  -lc 'npm install --prefix /tmp/pw playwright@1.56.1 --no-audit --no-fund --silent && NODE_PATH=/tmp/pw/node_modules node /checks/ignored_roster_browser_check.cjs'
```

Натискання ігнорування в журналі змінює видимий рядок і кількість учнів на обох сайтах. Зняття ігнорування повертає ті самі рядки. Перевіряються завершення індикатора й відсутність помилок браузера. Артефакти містять лише синтетичні дані.
