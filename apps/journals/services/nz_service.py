import datetime
import logging
import re
import json
import time
import hashlib
from collections import defaultdict
from datetime import datetime, date, timedelta
from bs4 import BeautifulSoup
import requests
from django.utils import timezone
from django.db import transaction
from django.db.models import Max

from apps.curriculum.models import (
    AcademicYear, Semester, Student, StudentClass, Subject, ClassEnrollment,
    LessonTime, WeeklyScheduleEntry
)
from apps.journals.models import Journal, Lesson, Grade, Attendance, NZAccount, NZSyncLog, LessonType, AuditLog
from .journal_lifecycle import clear_journal_content

logger = logging.getLogger(__name__)


class NZServiceError(Exception):
    """Базовий клас для помилок взаємодії з nz.ua."""
    pass


class NZAuthError(NZServiceError):
    """Помилка автентифікації або прострочена сесія на nz.ua."""
    pass


class NZService:
    """
    Сервіс для безпечної інтеграції з порталом nz.ua:
    - Автентифікація через логін/пароль або через сесійну куку PHPSESSID.
    - Отримання списку журналів вчителя з nz.ua.
    - Імпорт/синхронізація класів, учнів, уроків та оцінок із nz.ua.
    - Відстеження локальних змін (дельта).
    - Безпечне вивантаження лише змінених записів (grades/attendances/topics) з rate-limiting.
    - Формування детального звіту синхронізації.
    """

    BASE_URL = "https://nz.ua"
    REQUEST_DELAY_SECONDS = 0.4  # Затримка між запитами для запобігання перевантаженню nz.ua

    # Мапінг локальних оцінок/відвідувань у mark_id на nz.ua
    # За даними аналізу JS window.journal.markValues на nz.ua:
    # 1..12 -> 6..17 (значення + 5)
    # Н (неповажна відсутність) -> 1
    # ХВ (хвороба / поважна) -> 23
    # ЗАР (зараховано) -> 3
    # Н/А (не атестовано) -> 2; Н/З (не зараховано) -> 33
    # ЗВ (звільнений) -> 4
    # ID 30 is «п/п», not lateness. Local ЗП has no confirmed NZ equivalent.
    # 0 -> очищення оцінки / присутній
    MARK_MAP_TO_NZ = {
        '1': 6, '2': 7, '3': 8, '4': 9, '5': 10,
        '6': 11, '7': 12, '8': 13, '9': 14, '10': 15,
        '11': 16, '12': 17,
        'Н': 1,
        'ХВ': 23,
        'ЗАР': 3,
        'НЕЗАР': 33, 'Н/З': 33, 'Н/А': 2,
        'ЗВ': 4,
        'ВИВЧ': 5, 'П': 24, 'С': 25, 'Д': 26, 'В': 27,
        'Н/О': 28, 'ЗАУВ': 29, 'П/П': 30, 'К': 32,
        '': 0,
    }

    # Зворотний мапінг із nz.ua у локальні оцінки / відвідування
    MARK_MAP_FROM_NZ = {
        1: ('attendance', Attendance.Status.ABSENT_UNEXCUSED, 'Н'),
        23: ('attendance', Attendance.Status.ABSENT_EXCUSED, 'ХВ'),
        2: ('grade', 'Н/А'),
        33: ('grade', 'НЕЗАР'),
        3: ('grade', 'ЗАР'),
        4: ('grade', 'ЗВ'),
        5: ('grade', 'ВИВЧ'),
        24: ('grade', 'П'), 25: ('grade', 'С'), 26: ('grade', 'Д'),
        27: ('grade', 'В'), 28: ('grade', 'Н/О'), 29: ('grade', 'ЗАУВ'),
        30: ('grade', 'П/П'), 32: ('grade', 'К'),
        6: ('grade', '1'),
        7: ('grade', '2'),
        8: ('grade', '3'),
        9: ('grade', '4'),
        10: ('grade', '5'),
        11: ('grade', '6'),
        12: ('grade', '7'),
        13: ('grade', '8'),
        14: ('grade', '9'),
        15: ('grade', '10'),
        16: ('grade', '11'),
        17: ('grade', '12'),
    }

    DEFAULT_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    def __init__(self, phpsessid=None, cookies_dict=None):
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self.csrf_token = None

        if phpsessid:
            self.session.cookies.set('PHPSESSID', phpsessid, domain='nz.ua')
        if cookies_dict and isinstance(cookies_dict, dict):
            for k, v in cookies_dict.items():
                self.session.cookies.set(k, v, domain='nz.ua')

    @classmethod
    def from_account(cls, account: NZAccount):
        """Створити екземпляр сервісу з облікового запису користувача."""
        cookies = account.cookies or {}
        sessid = account.phpsessid or cookies.get('PHPSESSID')
        return cls(phpsessid=sessid, cookies_dict=cookies)

    def extract_csrf(self, html_text: str) -> str:
        """Витягти CSRF токен з мета-тегу або прихованого поля сторінки."""
        soup = BeautifulSoup(html_text, 'html.parser')
        meta = soup.find('meta', attrs={'name': 'csrf-token'})
        if meta and meta.get('content'):
            self.csrf_token = meta['content']
            return self.csrf_token

        inp = soup.find('input', attrs={'name': '_csrf'})
        if inp and inp.get('value'):
            self.csrf_token = inp['value']
            return self.csrf_token

        return self.csrf_token or ""

    def verify_session(self) -> tuple[bool, str]:
        """
        Перевірити чи дійсна сесія на nz.ua завантаживши /journal/list.
        Повертає (is_valid, message_or_username).
        """
        try:
            resp = self.session.get(f"{self.BASE_URL}/journal/list", timeout=12)
            if resp.status_code == 200:
                if "/login" in resp.url or "LoginForm" in resp.text:
                    return False, "Сесія недійсна або вичерпана (перенаправлено на вхід)."

                # Оновлюємо CSRF
                self.extract_csrf(resp.text)

                # Шукаємо ім'я користувача на сторінці
                soup = BeautifulSoup(resp.text, 'html.parser')
                if not soup.find('a', href=re.compile(r'^(?:https://nz\.ua)?/logout(?:\?|$)')):
                    return False, 'Не вдалося підтвердити авторизовану сторінку nz.ua.'
                user_elem = soup.select_one('.user-menu-box, .profile-name, .username, .user-name, header .dropdown-toggle')
                user_info = user_elem.get_text(strip=True) if user_elem else "Авторизовано"
                return True, user_info
            return False, f"Сервер nz.ua повернув код {resp.status_code}"
        except requests.RequestException as e:
            return False, f"Помилка з'єднання з nz.ua: {str(e)}"

    def login(self, login_str: str, password_str: str) -> tuple[bool, str]:
        """
        Вхід на nz.ua через форму /login.
        """
        try:
            get_resp = self.session.get(f"{self.BASE_URL}/login", timeout=12)
            csrf = self.extract_csrf(get_resp.text)
            if not csrf:
                return False, "Не вдалося отримати CSRF-токен зі сторінки входу nz.ua."

            payload = {
                '_csrf': csrf,
                'LoginForm[login]': login_str,
                'LoginForm[password]': password_str,
                'LoginForm[rememberMe]': '1',
            }

            post_resp = self.session.post(
                f"{self.BASE_URL}/login",
                data=payload,
                allow_redirects=True,
                timeout=15
            )

            if post_resp.status_code in [200, 302]:
                if "/login" in post_resp.url and "LoginForm" in post_resp.text:
                    soup = BeautifulSoup(post_resp.text, 'html.parser')
                    err = soup.select_one('.help-block-error, .alert-danger')
                    err_msg = err.get_text(strip=True) if err else "Невірний логін або пароль."
                    return False, err_msg

                self.extract_csrf(post_resp.text)
                return True, "Успішний вхід на nz.ua"
            return False, f"Помилка відповіді nz.ua: код {post_resp.status_code}"
        except requests.RequestException as e:
            return False, f"Помилка з'єднання: {str(e)}"

    def get_cookies_dict(self) -> dict:
        """Повернути збережені кукі сесії у вигляді словника."""
        return self.session.cookies.get_dict()

    def fetch_journal_list(self) -> list[dict]:
        """
        Отримати перелік журналів вчителя з https://nz.ua/journal/list.
        Повертає список словників:
        [
            {
                'nz_journal_id': '12345',
                'nz_subgroup_id': '0',
                'class_name': '5-А',
                'subject_name': 'Математика',
                'full_title': '5-А - Математика',
                'url': 'https://nz.ua/journal/index?journal=12345'
            },
            ...
        ]
        """
        resp = self.session.get(f"{self.BASE_URL}/journal/list", timeout=15)
        if resp.status_code != 200 or "/login" in resp.url or 'LoginForm' in resp.text:
            raise NZAuthError("Необхідно авторизуватись на nz.ua.")

        self.extract_csrf(resp.text)
        soup = BeautifulSoup(resp.text, 'html.parser')

        journals = []
        links = soup.find_all('a', href=re.compile(r'/journal/index\?journal=\d+'))

        seen_keys = set()
        for a in links:
            href = a.get('href', '')
            match_j = re.search(r'journal=(\d+)', href)
            if not match_j:
                continue

            j_id = match_j.group(1)
            match_sg = re.search(r'subgroup=(\d+)', href)
            sg_id = match_sg.group(1) if match_sg else ""

            key = (j_id, sg_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            text = a.get_text(separator=' ', strip=True)
            # Часто посилання містить "5-А Клас Математика" або схожий текст
            # Спробуємо розпарсити клас та предмет
            class_match = re.search(r'(\d+[\s\-]?[А-Яа-яA-Za-z]?)\s*(?:клас)?', text, re.IGNORECASE)
            class_name = class_match.group(1).replace(' ', '') if class_match else ""

            # Шукаємо предмет поруч у рядку таблиці, якщо є
            parent_tr = a.find_parent('tr')
            subject_name = ""
            if parent_tr:
                tds = parent_tr.find_all('td')
                for td in tds:
                    td_txt = td.get_text(strip=True)
                    if td_txt and td_txt != text and not re.search(r'^\d+$', td_txt):
                        subject_name = td_txt
                        break

            if not subject_name:
                # Вилучаємо клас із тексту для визначення предмета
                sub_txt = text
                if class_name:
                    sub_txt = re.sub(re.escape(class_name), '', sub_txt, flags=re.IGNORECASE)
                sub_txt = re.sub(r'клас', '', sub_txt, flags=re.IGNORECASE).strip(' -—:,')
                subject_name = sub_txt or "Предмет"

            journals.append({
                'nz_journal_id': j_id,
                'nz_subgroup_id': sg_id,
                'class_name': class_name or "Клас",
                'subject_name': subject_name,
                'full_title': text or f"{class_name} — {subject_name}",
                'url': f"{self.BASE_URL}{href}" if href.startswith('/') else href,
            })

        return journals

    @staticmethod
    def _parse_buzzers(html_or_soup) -> dict:
        """
        Розпарсити час дзвінків / розклад уроків школи з селектора OsvitaScheduleReal[buzzer_id].
        Повертає словник:
        {
            buzzer_id: {
                'buzzer_id': str,
                'number': int,
                'start_time': datetime.time,
                'end_time': datetime.time,
                'label': str,
                'is_selected': bool
            }
        }
        """
        if not html_or_soup:
            return {}
        if isinstance(html_or_soup, str):
            soup = BeautifulSoup(html_or_soup, 'html.parser')
        else:
            soup = html_or_soup

        buzzers = {}
        selects = soup.select('select[id*="buzzer_id"], select[name*="buzzer_id"]')
        default_num = 1
        for sel in selects:
            for opt in sel.find_all('option'):
                val = (opt.get('value') or '').strip()
                if not val or not val.isdigit():
                    continue
                txt = opt.get_text(' ', strip=True)
                m = re.search(r'(?:(\d+)\s*(?:урок|[.)])\s*)?[^(]*\(\s*(\d{1,2}:\d{2})(?:\s*[-–—]\s*(\d{1,2}:\d{2}))?\s*\)', txt, re.I)
                if m:
                    num = int(m.group(1)) if m.group(1) else default_num
                    default_num = max(default_num, num) + 1
                    try:
                        s_time = datetime.strptime(m.group(2), '%H:%M').time()
                        if m.group(3):
                            e_time = datetime.strptime(m.group(3), '%H:%M').time()
                        else:
                            s_dt = datetime.combine(datetime.today(), s_time)
                            e_time = (s_dt + timedelta(minutes=45)).time()
                    except ValueError:
                        continue
                    is_sel = opt.has_attr('selected') or ('selected' in opt.attrs)
                    buzzers[val] = {
                        'buzzer_id': val,
                        'number': num,
                        'start_time': s_time,
                        'end_time': e_time,
                        'label': txt,
                        'is_selected': is_sel,
                    }
        return buzzers

    def fetch_lesson_schedule_details(self, nz_journal_id: str, nz_schedule_id: str) -> dict:
        """
        Завантажити деталі уроку з форми редагування nz.ua:
        - buzzer_id (час уроку)
        - room_id / назва кабінету
        - lesson_type_id
        """
        url = f"{self.BASE_URL}/journal/add-edit-lesson?journal={nz_journal_id}&schedule={nz_schedule_id}"
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200 or '/login' in str(resp.url) or 'LoginForm' in resp.text:
                return {}
            soup = BeautifulSoup(resp.text, 'html.parser')
            buzzers = self._parse_buzzers(soup)
            selected_buzzer = None
            for b_id, b_info in buzzers.items():
                if b_info.get('is_selected'):
                    selected_buzzer = b_info
                    break

            if not selected_buzzer:
                sel = soup.select_one('select[id*="buzzer_id"], select[name*="buzzer_id"]')
                if sel:
                    opt = sel.select_one('option[selected]')
                    if opt and opt.get('value') in buzzers:
                        selected_buzzer = buzzers[opt['value']]

            room_name = ""
            room_sel = soup.select_one('select[id*="room_id"], select[name*="room_id"]')
            if room_sel:
                r_opt = room_sel.select_one('option[selected]')
                if r_opt:
                    room_name = r_opt.get_text(strip=True)

            return {
                'buzzer': selected_buzzer,
                'buzzers': buzzers,
                'room': room_name,
            }
        except Exception as e:
            logger.debug(f"fetch_lesson_schedule_details error for schedule {nz_schedule_id}: {e}")
            return {}

    def _parse_single_journal_page(self, raw_html: str, nz_journal_id: str) -> dict:
        soup = BeautifulSoup(raw_html, 'html.parser')

        table = soup.select_one('table.point-table')
        if not table:
            raise NZServiceError('Структуру журналу nz.ua не розпізнано. Локальні дані не змінено.')

        # 0.5. Парсимо дзвінки (buzzers)
        buzzers = self._parse_buzzers(soup)

        # 0. Визначаємо базовий навчальний рік із селектора семестрів або поточної дати
        base_year = timezone.now().year
        sem_opt = soup.select_one('select[id*="semester"] option[selected]') or soup.select_one('select[id*="semester"] option')
        if sem_opt:
            m_year = re.search(r'(\d{4})[-–/](\d{4})', sem_opt.get_text())
            if m_year:
                base_year = int(m_year.group(1))

        ukr_months = {
            'січ': 1, 'лют': 2, 'бер': 3, 'квіт': 4, 'трав': 5, 'черв': 6,
            'лип': 7, 'серп': 8, 'вер': 9, 'жовт': 10, 'лист': 11, 'груд': 12
        }

        # 1. Парсимо блок календарного плану та домашніх завдань (.homework-row або таблиця тем)
        hw_by_sch = {}
        hw_by_date_and_num = {}
        hw_by_date = defaultdict(list)
        hw_list = []

        hw_rows = list(soup.select('.homework-row'))
        if not hw_rows:
            for tbl in soup.find_all('table'):
                t_txt = tbl.get_text(' ', strip=True).lower()
                if any(k in t_txt for k in ('домашнє завдання', 'зміст', 'тема уроку')):
                    hw_rows.extend(tbl.find_all('tr'))
                    if hw_rows:
                        break

        col_date = None
        col_num = None
        col_topic = None
        col_hw = None
        col_notes = None

        header_row = None
        for r in hw_rows:
            r_classes = r.get('class', [])
            if any('header' in c for c in r_classes) or r.find('th'):
                header_row = r
                break
            cells = r.select('.homework__item') or r.find_all(['td', 'th'])
            c_texts = [c.get_text(' ', strip=True).lower() for c in cells]
            if any('дата' in t or 'date' in t for t in c_texts) and any(
                k in t for t in c_texts for k in ('зміст', 'тема', 'домашнє', 'завдання', '№')
            ):
                header_row = r
                break

        if header_row:
            h_cells = header_row.select('.homework__item') or header_row.find_all(['td', 'th'])
            for idx, c in enumerate(h_cells):
                h = c.get_text(' ', strip=True).lower()
                if 'дата' in h or 'date' in h:
                    col_date = idx
                elif any(k in h for k in ('№', 'номер', 'number', 'уроку')):
                    col_num = idx
                elif any(k in h for k in ('зміст', 'тема', 'назва', 'topic', 'title')):
                    col_topic = idx
                elif any(k in h for k in ('домашнє', 'завдання', 'дз', 'homework', 'hometask')):
                    col_hw = idx
                elif any(k in h for k in ('заміна', 'приміт', 'нотат', 'notes', 'substitut')):
                    col_notes = idx

        for r in hw_rows:
            if r is header_row:
                continue
            cells = r.select('.homework__item') or r.find_all(['td', 'th'])
            if not cells:
                continue
            items = [it.get_text(' ', strip=True) for it in cells]
            if not any(items):
                continue
            first_items_str = ' '.join(items).lower()
            if ('дата' in first_items_str and ('тема' in first_items_str or 'зміст' in first_items_str or 'домашнє' in first_items_str)):
                continue

            c_date, c_num, c_topic, c_hw = col_date, col_num, col_topic, col_hw
            if c_topic is None or c_hw is None:
                if len(items) == 5:
                    c_num, c_date, c_topic, c_hw = 0, 1, 2, 3
                elif len(items) == 6:
                    if not items[0] or r.find('a', href=re.compile(r'schedule=(\d+)')):
                        c_num, c_date, c_topic, c_hw = None, 1, 2, 3
                    else:
                        c_num, c_date, c_topic, c_hw = 0, 1, 2, 3
                elif len(items) >= 7:
                    c_num, c_date, c_topic, c_hw = 2, 1, 4, 5
                else:
                    c_num = 0 if len(items) > 0 and items[0].isdigit() else None
                    c_date = 1 if len(items) > 1 else 0
                    c_topic = 2 if len(items) > 2 else None
                    c_hw = 3 if len(items) > 3 else None

            sch_id = ""
            link = r.find('a', href=re.compile(r'schedule=(\d+)'))
            if link:
                sm = re.search(r'schedule=(\d+)', link['href'])
                if sm:
                    sch_id = sm.group(1)
            if not sch_id:
                sch_id = r.get('data-schedule') or r.get('data-schedule-id') or ""

            d_obj = None
            date_text = items[c_date] if c_date is not None and c_date < len(items) else ''
            dm_match = re.search(r'(\d{1,2})\s+([а-яА-Яіїєґ]+)', date_text.lower())
            if dm_match:
                d_day = int(dm_match.group(1))
                m_word = dm_match.group(2)
                for pfx, mo in ukr_months.items():
                    if m_word.startswith(pfx):
                        y = base_year if mo >= 8 else base_year + 1
                        try:
                            d_obj = datetime(y, mo, d_day).date()
                        except ValueError:
                            pass
                        break
            if not d_obj and date_text:
                dot_match = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?', date_text)
                if dot_match:
                    d_day = int(dot_match.group(1))
                    mo = int(dot_match.group(2))
                    y = int(dot_match.group(3)) if dot_match.group(3) else (base_year if mo >= 8 else base_year + 1)
                    if y < 100:
                        y += 2000
                    try:
                        d_obj = datetime(y, mo, d_day).date()
                    except ValueError:
                        pass

            lesson_num = None
            if c_num is not None and c_num < len(items):
                m_num = re.search(r'\d+', items[c_num])
                if m_num:
                    lesson_num = int(m_num.group(0))

            topic_txt = items[c_topic] if c_topic is not None and c_topic < len(items) else ''
            raw_hw = items[c_hw] if c_hw is not None and c_hw < len(items) else ''
            next_cell = items[c_hw + 1] if c_hw is not None and (c_hw + 1) < len(items) else ''

            due_date = None
            hw_txt = ''
            due_match = re.match(r'^на\s+(?:(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?|(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?)(?:\s*[:—-])?\s*', raw_hw, re.I)
            if due_match:
                due_date = self._parse_lesson_date(due_match.group(0), base_year)
                rem = raw_hw[due_match.end():].strip()
                if rem:
                    hw_txt = rem
                elif c_hw is not None and len(items) > (c_hw + 1) and (len(items) >= (c_hw + 3) or len(items) > 7):
                    hw_txt = next_cell
                elif c_hw is not None and len(items) == (c_hw + 2) and next_cell and not re.search(r'\bзаміна\b', next_cell, re.I):
                    hw_txt = next_cell
            else:
                if raw_hw:
                    hw_txt = raw_hw
                elif c_hw is not None and len(items) > (c_hw + 1) and (len(items) >= (c_hw + 3) or len(items) > 7):
                    hw_txt = next_cell

            if hw_txt and not due_date:
                m_next_due = re.match(r'^на\s+(?:(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?|(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?)(?:\s*[:—-])?\s*', hw_txt, re.I)
                if m_next_due:
                    due_date = self._parse_lesson_date(m_next_due.group(0), base_year)
                    hw_txt = hw_txt[m_next_due.end():].strip()

            hw_txt = hw_txt.strip()

            entry = {
                'date': d_obj,
                'lesson_number': lesson_num,
                'topic': topic_txt,
                'homework': hw_txt,
                'homework_due_date': due_date,
            }
            if sch_id:
                hw_by_sch[sch_id] = entry
            if d_obj and lesson_num:
                hw_by_date_and_num[(d_obj, lesson_num)] = entry
            if d_obj:
                hw_by_date[d_obj].append(entry)
            hw_list.append(entry)

        # 2. Заголовки колонок уроків (thead tr td.pt-point)
        header_pt_cells = table.select('thead tr td.pt-point')
        if not header_pt_cells:
            header_pt_cells = [
                c for c in table.select('thead tr td, thead tr th')
                if 'pt-point' in c.get('class', []) or (c.get('id') and c['id'].startswith('cell-0-'))
            ]

        lessons_data = []
        current_month = None
        last_parsed_date = None
        seen_date_numbers = set()

        for idx, cell in enumerate(header_pt_cells):
            col_idx = idx + 1

            # Місяць у span.pt-month (наприклад "Вер.", "Жовт.")
            m_elem = cell.find('span', class_='pt-month')
            if m_elem:
                m_txt = m_elem.get_text(strip=True).lower()
                for pfx, mo in ukr_months.items():
                    if m_txt.startswith(pfx):
                        current_month = mo
                        break

            link = cell.find('a', href=re.compile(r'schedule=(\d+)'))
            sch_id = ""
            day_num = None
            if link:
                sm = re.search(r'schedule=(\d+)', link['href'])
                if sm:
                    sch_id = sm.group(1)
                ltxt = link.get_text(strip=True)
                if ltxt.isdigit():
                    day_num = int(ltxt)
            else:
                dm = re.search(r'\b(\d{1,2})\b', cell.get_text(separator=' ', strip=True))
                if dm:
                    day_num = int(dm.group(1))

            hw_info = hw_by_sch.get(sch_id, {})
            parsed_date = hw_info.get('date')

            if not parsed_date and day_num and current_month:
                y = base_year if current_month >= 8 else base_year + 1
                try:
                    parsed_date = datetime(y, current_month, day_num).date()
                except ValueError:
                    pass

            if not parsed_date:
                # Перевіряємо data-date або DD.MM
                date_attr = cell.get('data-date') or ""
                if date_attr:
                    try:
                        parsed_date = datetime.strptime(date_attr, '%Y-%m-%d').date()
                    except ValueError:
                        pass
                if not parsed_date:
                    dm_match = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?', cell.get_text(separator=' ', strip=True))
                    if dm_match:
                        d, m = int(dm_match.group(1)), int(dm_match.group(2))
                        y = int(dm_match.group(3)) if dm_match.group(3) else (base_year if m >= 8 else base_year + 1)
                        if y < 100:
                            y += 2000
                        try:
                            parsed_date = datetime(y, m, d).date()
                        except ValueError:
                            pass

            column_type = self._column_type(cell, parsed_date or last_parsed_date)
            if not parsed_date and column_type != LessonType.ORDINARY:
                parsed_date = last_parsed_date
            if parsed_date:
                last_parsed_date = parsed_date

            # Якщо hw_info ще не знайдено за sch_id, шукаємо за датою або порядком
            if not hw_info:
                if parsed_date and hw_by_date.get(parsed_date):
                    hw_info = hw_by_date[parsed_date][0]
                elif idx < len(hw_list):
                    hw_info = hw_list[idx]

            parsed_num = hw_info.get('lesson_number')
            if parsed_date:
                if not parsed_num or (parsed_date, parsed_num) in seen_date_numbers:
                    candidate = 1
                    while (parsed_date, candidate) in seen_date_numbers:
                        candidate += 1
                    parsed_num = candidate
                seen_date_numbers.add((parsed_date, parsed_num))
            else:
                parsed_num = parsed_num or 1

            # Уточнений пошук hw_info за парою (дата, номер), якщо є
            if parsed_date and (parsed_date, parsed_num) in hw_by_date_and_num:
                hw_info = hw_by_date_and_num[(parsed_date, parsed_num)]

            lesson_obj = {
                'col_index': col_idx,
                'nz_schedule_id': sch_id,
                'date': parsed_date,
                'lesson_number': parsed_num,
                'lesson_time': None,
                'buzzer_id': '',
                'room': '',
                'topic': hw_info.get('topic', ''),
                'homework': hw_info.get('homework', ''),
                'homework_due_date': hw_info.get('homework_due_date'),
                'lesson_type': column_type,
            }
            lessons_data.append(lesson_obj)

        # Перевіряємо форми уроків, якщо вони вже присутні в HTML сторінки
        forms_by_sch = {}
        for f in soup.find_all('form'):
            action = f.get('action') or ''
            m_sch = re.search(r'schedule=(\d+)', action)
            if m_sch:
                forms_by_sch[m_sch.group(1)] = f

        for les in lessons_data:
            s_id = les.get('nz_schedule_id')
            if not s_id:
                continue
            f = forms_by_sch.get(s_id)
            if f:
                f_buzzers = self._parse_buzzers(f)
                for b_val, b_info in f_buzzers.items():
                    if b_info.get('is_selected'):
                        les['buzzer_id'] = b_val
                        les['lesson_number'] = b_info['number']
                        les['lesson_time'] = b_info['start_time']
                        break


        # 3. Список учнів та оцінки
        students_data = []
        marks_data = []

        tbody = table.select_one('tbody') or table
        rows = tbody.find_all('tr')

        for tr in rows:
            student_cell = tr.select_one('td.pt-theme, td[data-student-id]')
            if not student_cell:
                continue

            st_id = student_cell.get('data-student-id') or ""
            st_name = student_cell.get_text(strip=True)
            st_name_clean = re.sub(r'^\d+[\.\)]\s*', '', st_name).strip()

            if not st_id and not st_name_clean:
                continue

            students_data.append({
                'nz_student_id': st_id,
                'name': st_name_clean,
            })

            # Читаємо оцінки у стовпцях учня (td.pt-point відповідають колонкам уроків)
            row_pt_cells = tr.select('td.pt-point')
            for c_idx, cell in enumerate(row_pt_cells):
                if c_idx >= len(lessons_data):
                    break

                les_info = lessons_data[c_idx]

                inp = cell.find('input')
                val = ""
                mark_id = None
                if inp:
                    val = inp.get('value', '').strip()
                    m_id_attr = inp.get('data-mark-id')
                    if m_id_attr and m_id_attr.isdigit():
                        mark_id = int(m_id_attr)
                else:
                    val = cell.get_text(strip=True)

                if val or mark_id is not None or les_info['nz_schedule_id']:
                    marks_data.append({
                        'nz_student_id': st_id,
                        'student_name': st_name_clean,
                        'nz_schedule_id': les_info['nz_schedule_id'],
                        'date': les_info['date'],
                        'lesson_number': les_info['lesson_number'],
                        'value': val,
                        'mark_id': mark_id,
                    })

        return {
            'nz_journal_id': nz_journal_id,
            'students': students_data,
            'lessons': lessons_data,
            'marks': marks_data,
            'base_year': base_year,
            'buzzers': buzzers,
        }

    def fetch_journal_data(self, nz_journal_id: str, nz_subgroup_id: str = "", html_content: str = None) -> dict:
        """
        Завантажити та розпарсити повний вміст журналу з nz.ua:
        - Список учнів (nz_student_id, ПІБ)
        - Список проведених уроків/колонок (nz_schedule_id, date, lesson_number, topic, homework)
        - Оцінки та відвідування (schedule_id, student_id -> mark_value / symbol)
        - Розклад дзвінків (buzzers)
        Автоматично обробляє всі сторінки (пагінацію) журналу на nz.ua.
        """
        if html_content:
            self.extract_csrf(html_content)
            return self._parse_single_journal_page(html_content, nz_journal_id)

        url = f"{self.BASE_URL}/journal/index?journal={nz_journal_id}"
        if nz_subgroup_id:
            url += f"&subgroup={nz_subgroup_id}"

        resp = self.session.get(url, timeout=20)
        if resp.status_code != 200 or "/login" in resp.url or 'LoginForm' in resp.text:
            raise NZAuthError("Сесія nz.ua недійсна.")

        self.extract_csrf(resp.text)
        initial_soup = BeautifulSoup(resp.text, 'html.parser')

        # Визначаємо наявність пагінації сторінок на nz.ua
        page_links = initial_soup.find_all('a', href=re.compile(r'[?&]page=(\d+)'))
        pages = set()
        for a in page_links:
            m = re.search(r'[?&]page=(\d+)', a['href'])
            if m:
                pages.add(int(m.group(1)))

        active_page = 1
        act = initial_soup.select_one('ul.pagination li.active a, .pagination .active a')
        if act:
            m = re.search(r'[?&]page=(\d+)', act.get('href', ''))
            if m:
                active_page = int(m.group(1))
            elif act.get('data-page') and act['data-page'].isdigit():
                active_page = int(act['data-page']) + 1

        all_page_numbers = list(range(1, max(pages) + 1)) if pages else [1]

        if len(all_page_numbers) == 1:
            data = self._parse_single_journal_page(resp.text, nz_journal_id)
            students_data = data['students']
            lessons_data = data['lessons']
            marks_data = data['marks']
            buzzers = data['buzzers']
            base_year = data['base_year']
        else:
            students_data = []
            seen_students = set()
            lessons_data = []
            seen_lessons = set()
            marks_data = []
            seen_marks = set()
            buzzers = {}
            base_year = None

            for p in all_page_numbers:
                if p == active_page:
                    p_html = resp.text
                else:
                    p_url = f"{self.BASE_URL}/journal/index?journal={nz_journal_id}&page={p}"
                    if nz_subgroup_id:
                        p_url += f"&subgroup={nz_subgroup_id}"
                    p_resp = self.session.get(p_url, timeout=20)
                    if p_resp.status_code != 200 or '/login' in str(p_resp.url):
                        continue
                    p_html = p_resp.text

                try:
                    p_data = self._parse_single_journal_page(p_html, nz_journal_id)
                except Exception:
                    continue

                if base_year is None:
                    base_year = p_data.get('base_year')
                buzzers.update(p_data.get('buzzers', {}))

                for st in p_data.get('students', []):
                    st_key = st.get('nz_student_id') or st.get('name')
                    if st_key and st_key not in seen_students:
                        seen_students.add(st_key)
                        students_data.append(st)

                for les in p_data.get('lessons', []):
                    les_key = les.get('nz_schedule_id') or (les.get('date'), les.get('lesson_number'))
                    if les_key and les_key not in seen_lessons:
                        seen_lessons.add(les_key)
                        lessons_data.append(les)

                for m in p_data.get('marks', []):
                    m_key = (m.get('nz_student_id'), m.get('nz_schedule_id'), m.get('date'), m.get('lesson_number'))
                    if m_key not in seen_marks:
                        seen_marks.add(m_key)
                        marks_data.append(m)

            for idx, l in enumerate(lessons_data, 1):
                l['col_index'] = idx

        # 0.5. Парсимо дзвінки (buzzers), якщо ще не визначено
        if not buzzers and self.session and self.session.cookies.get('PHPSESSID'):
            try:
                resp_add = self.session.get(f"{self.BASE_URL}/journal/add-edit-lesson?journal={nz_journal_id}", timeout=10)
                if resp_add.status_code == 200 and '/login' not in str(resp_add.url):
                    buzzers = self._parse_buzzers(resp_add.text)
            except Exception:
                pass

        # Якщо сесія активна онлайн — завантажуємо розклад уроків
        if self.session and self.session.cookies.get('PHPSESSID'):
            schedule_cache = {}
            for les in lessons_data:
                s_id = les.get('nz_schedule_id')
                if not s_id or les.get('lesson_time'):
                    continue
                wday = les['date'].weekday() if les.get('date') else None
                if wday in schedule_cache:
                    cached = schedule_cache[wday]
                    les['buzzer_id'] = cached.get('buzzer_id', '')
                    les['lesson_number'] = cached.get('lesson_number', les['lesson_number'])
                    les['lesson_time'] = cached.get('lesson_time')
                    continue
                dtl = self.fetch_lesson_schedule_details(nz_journal_id, s_id)
                if dtl and dtl.get('buzzer'):
                    b = dtl['buzzer']
                    les['buzzer_id'] = b['buzzer_id']
                    les['lesson_number'] = b['number']
                    les['lesson_time'] = b['start_time']
                    if wday is not None:
                        schedule_cache[wday] = {
                            'buzzer_id': b['buzzer_id'],
                            'lesson_number': b['number'],
                            'lesson_time': b['start_time'],
                        }
                    if dtl.get('buzzers') and not buzzers:
                        buzzers = dtl['buzzers']

        return {
            'nz_journal_id': nz_journal_id,
            'students': students_data,
            'lessons': lessons_data,
            'marks': marks_data,
            'base_year': base_year or timezone.now().year,
            'buzzers': buzzers,
        }

    @staticmethod
    def _column_type(cell, lesson_date=None):
        text = cell.get_text(' ', strip=True).lower().replace('і', 'i')
        if 'рiчн' in text:
            return LessonType.ANNUAL
        if re.search(r'сем(?:естр|[.\s]|$)', text):
            second = bool(re.search(r'\b(ii|2)\b', text)) or (not re.search(r'\b(i|1)\b', text) and lesson_date and lesson_date.month < 8)
            if 'нуш' in text:
                return LessonType.SEMESTER_2_NUSH if second else LessonType.SEMESTER_1_NUSH
            return LessonType.SEMESTER_2 if second else LessonType.SEMESTER_1
        if 'модул' in text:
            return LessonType.MODULAR
        if 'темат' in text:
            if 'нуш' in text:
                return LessonType.THEMATIC_NUSH
            return LessonType.THEMATIC
        return LessonType.ORDINARY

    @transaction.atomic
    def import_journal_to_local(
        self,
        nz_journal_id: str,
        local_journal: Journal,
        nz_subgroup_id: str = "",
        user=None,
        html_content: str = None,
        replace: bool = False,
    ) -> dict:
        """
        Імпортувати дані з журналу nz.ua у локальний Journal:
        - Прив'язує nz_journal_id до локального журналу.
        - Синхронізує розклад дзвінків школи (LessonTime).
        - Синхронізує навчальний рік та семестр.
        - Синхронізує учнів (зв'язує за ПІБ або створює).
        - Синхронізує проведені уроки (nz_schedule_id, дата, час, номер, тема, ДЗ).
        - Завантажує оцінки та відвідування і позначає їх `is_synced_to_nz = True`.
        """
        data = self.fetch_journal_data(nz_journal_id, nz_subgroup_id, html_content=html_content)
        Journal.objects.select_for_update().get(pk=local_journal.pk)

        removed = None
        if replace:
            if not all(isinstance(data.get(key), list) for key in ('students', 'lessons', 'marks')) or not any(
                lesson.get('date') for lesson in data['lessons']
            ):
                raise NZServiceError('Онлайн-журнал не містить розпізнаних уроків. Локальні дані збережено; перевірте журнал у nz.ua.')
            removed = clear_journal_content(local_journal)

        local_journal.nz_journal_id = nz_journal_id
        if nz_subgroup_id:
            local_journal.nz_subgroup_id = nz_subgroup_id
        local_journal.nz_last_sync_at = timezone.now()
        local_journal.save(update_fields=['nz_journal_id', 'nz_subgroup_id', 'nz_last_sync_at'])

        stats = {
            'students_matched': 0,
            'students_created': 0,
            'lessons_synced': 0,
            'grades_synced': 0,
            'attendances_synced': 0,
            'local_changes_preserved': 0,
            'lesson_times_synced': 0,
        }

        # 0.1. Синхронізація часу дзвінків (LessonTime) школи
        buzzers = data.get('buzzers', {})
        lesson_times_synced = 0
        if buzzers and local_journal.school:
            school = local_journal.school
            for b_val, b_data in buzzers.items():
                b_num = b_data['number']
                b_start = b_data['start_time']
                b_end = b_data['end_time']
                lt = LessonTime.objects.filter(school=school, number=b_num).first()
                if not lt:
                    lt = LessonTime.objects.filter(school=school, start_time=b_start).first()
                if lt:
                    lt.number = b_num
                    lt.start_time = b_start
                    lt.end_time = b_end
                    lt.is_active = True
                    lt.save()
                else:
                    LessonTime.objects.create(
                        school=school,
                        number=b_num,
                        start_time=b_start,
                        end_time=b_end,
                        is_active=True
                    )
                lesson_times_synced += 1
        stats['lesson_times_synced'] = lesson_times_synced

        # 0. Перевірка та оновлення навчального року і семестрів локального журналу
        lesson_dates = [l['date'] for l in data['lessons'] if l.get('date')]
        if lesson_dates:
            min_d = min(lesson_dates)
            max_d = max(lesson_dates)
            start_y = min_d.year if min_d.month >= 8 else min_d.year - 1
            ay_name = f"{start_y}/{start_y + 1}"

            current_ay = local_journal.academic_year
            if not current_ay or not (current_ay.start_date <= min_d and current_ay.end_date >= max_d):
                school = local_journal.school
                target_ay = AcademicYear.objects.filter(school=school, name=ay_name).first()
                if not target_ay:
                    target_ay = AcademicYear.objects.create(
                        school=school,
                        name=ay_name,
                        start_date=date(start_y, 8, 20),
                        end_date=date(start_y + 1, 6, 30),
                        is_current=True,
                    )
                # Переконуємось, що семестри існують
                s1 = target_ay.semesters.filter(number=1).first()
                if not s1:
                    Semester.objects.create(
                        academic_year=target_ay,
                        number=1,
                        name="I семестр",
                        start_date=date(start_y, 8, 20),
                        end_date=date(start_y, 12, 31)
                    )
                s2 = target_ay.semesters.filter(number=2).first()
                if not s2:
                    Semester.objects.create(
                        academic_year=target_ay,
                        number=2,
                        name="II семестр",
                        start_date=date(start_y + 1, 1, 1),
                        end_date=date(start_y + 1, 6, 30)
                    )

                local_journal.academic_year = target_ay
                local_journal.save(update_fields=['academic_year'])
                if local_journal.student_class:
                    local_journal.student_class.academic_year = target_ay
                    local_journal.student_class.save(update_fields=['academic_year'])

        # 1. Студенти
        student_id_map = {}  # nz_student_id / full_name -> Student instance
        for st_info in data['students']:
            nz_s_id = st_info['nz_student_id']
            full_name = st_info['name']
            parts = full_name.split()
            last_name = parts[0] if len(parts) > 0 else "Учень"
            first_name = parts[1] if len(parts) > 1 else ""
            middle_name = parts[2] if len(parts) > 2 else ""

            # Шукаємо існуючого учня в школі
            student = None
            if nz_s_id:
                student = Student.objects.filter(school=local_journal.school, nz_student_id=nz_s_id).first()

            if not student and first_name:
                student = Student.objects.filter(
                    school=local_journal.school,
                    last_name__iexact=last_name,
                    first_name__iexact=first_name
                ).first()

            if student:
                if nz_s_id and not student.nz_student_id:
                    student.nz_student_id = nz_s_id
                    student.save(update_fields=['nz_student_id'])
                stats['students_matched'] += 1
            else:
                student = Student.objects.create(
                    school=local_journal.school,
                    last_name=last_name,
                    first_name=first_name or "Ім'я",
                    middle_name=middle_name,
                    nz_student_id=nz_s_id or ""
                )
                stats['students_created'] += 1

            # Додаємо зарахування до класу журналу, якщо відсутнє
            ClassEnrollment.objects.get_or_create(
                student=student,
                student_class=local_journal.student_class,
                defaults={'is_active': True}
            )

            if nz_s_id:
                student_id_map[nz_s_id] = student
            student_id_map[full_name] = student
            student_id_map[f"{last_name} {first_name}"] = student

        # 2. Уроки
        lesson_map = {}  # schedule_id / (date, number) / date -> Lesson instance
        for l_idx, l_info in enumerate(data['lessons'], start=1):
            l_date = l_info.get('date')
            if not l_date:
                continue

            sch_id = l_info.get('nz_schedule_id', '')
            l_num = l_info.get('lesson_number')
            l_time = l_info.get('lesson_time')
            lesson_type = l_info.get('lesson_type', LessonType.ORDINARY)
            topic = l_info.get('topic') or (dict(LessonType.choices)[lesson_type] if lesson_type != LessonType.ORDINARY else f"Урок {l_idx}")
            hw = l_info.get('homework') or ""

            # 1) Якщо час не отримано напряму з nz.ua, шукаємо у тижневому розкладі журналу
            if l_time is None:
                sched_entry = local_journal.weekly_schedule.filter(
                    weekday=l_date.weekday(),
                    start_date__lte=l_date,
                    end_date__gte=l_date
                ).first()
                if not sched_entry:
                    sched_entry = local_journal.weekly_schedule.filter(weekday=l_date.weekday()).first()
                if sched_entry:
                    l_time = sched_entry.lesson_time.start_time
                    l_num = sched_entry.lesson_time.number

            # 2) Якщо час ще не визначено, але є номер уроку
            if l_time is None and l_num:
                matching_lt = LessonTime.objects.filter(school=local_journal.school, number=l_num).first()
                if matching_lt:
                    l_time = matching_lt.start_time

            # 3) Якщо і номер уроку не визначено
            if not l_num:
                l_num = 1
                if l_time is None:
                    first_lt = LessonTime.objects.filter(school=local_journal.school, number=1).first()
                    if first_lt:
                        l_time = first_lt.start_time

            # Визначаємо семестр для уроку
            sem = None
            if local_journal.academic_year:
                sem = local_journal.academic_year.semesters.filter(
                    start_date__lte=l_date,
                    end_date__gte=l_date
                ).first()
                if not sem:
                    sem = local_journal.academic_year.semesters.first()

            lesson = None
            if sch_id:
                lesson = Lesson.objects.filter(journal=local_journal, nz_schedule_id=sch_id).first()

            if not lesson:
                # A date can contain several NZ columns. Never collapse different schedule IDs.
                lesson = Lesson.objects.filter(journal=local_journal, date=l_date, lesson_number=l_num, nz_schedule_id='').first()

            if lesson:
                lesson.nz_schedule_id = sch_id or lesson.nz_schedule_id
                occupied = local_journal.lessons.filter(date=l_date).exclude(pk=lesson.pk)
                if lesson.date != l_date:
                    lesson.date = l_date
                    lesson.lesson_number = l_num if not occupied.filter(lesson_number=l_num).exists() else (occupied.aggregate(last=Max('lesson_number'))['last'] or 0) + 1
                elif lesson.lesson_number != l_num and not occupied.filter(lesson_number=l_num).exists():
                    lesson.lesson_number = l_num
                if l_time is not None:
                    lesson.lesson_time = l_time
                if lesson.is_synced_to_nz:
                    lesson.topic = topic
                    lesson.homework = hw
                    lesson.homework_due_date = l_info.get('homework_due_date')
                    lesson.homework_due_lesson = None
                    lesson.homework_due_plan_topic = None
                    lesson.homework_for_next_lesson = False
                    lesson.lesson_type = l_info.get('lesson_type', lesson.lesson_type)
                else:
                    if (not lesson.homework or lesson.homework == lesson.nz_last_synced_homework) and hw:
                        lesson.homework = hw
                        lesson.homework_due_date = l_info.get('homework_due_date')
                    if not lesson.topic and topic:
                        lesson.topic = topic
                    stats['local_changes_preserved'] += 1
                if sem:
                    lesson.semester = sem
                lesson.nz_last_synced_topic = topic
                lesson.nz_last_synced_homework = hw
                lesson.save()
            else:
                if Lesson.objects.filter(journal=local_journal, date=l_date, lesson_number=l_num).exists():
                    l_num = (local_journal.lessons.filter(date=l_date).aggregate(last=Max('lesson_number'))['last'] or 0) + 1
                lesson = Lesson.objects.create(
                    journal=local_journal,
                    date=l_date,
                    lesson_number=l_num,
                    lesson_time=l_time,
                    semester=sem,
                    topic=topic,
                    homework=hw,
                    homework_due_date=l_info.get('homework_due_date'),
                    lesson_type=l_info.get('lesson_type', LessonType.ORDINARY),
                    nz_schedule_id=sch_id,
                    is_synced_to_nz=True,
                    nz_last_synced_topic=topic,
                    nz_last_synced_homework=hw,
                    created_by=user
                )

            stats['lessons_synced'] += 1
            if sch_id:
                lesson_map[sch_id] = lesson
            lesson_map[l_date] = lesson
            lesson_map[(l_date, l_num)] = lesson

        # Автоматично формуємо записи тижневого розкладу, якщо для журналу розклад ще не налаштовано
        if local_journal.weekly_schedule.count() == 0 and local_journal.academic_year:
            seen_slots = set()
            for les in local_journal.lessons.filter(lesson_time__isnull=False):
                wday = les.date.weekday()
                t = les.lesson_time
                if (wday, t) not in seen_slots:
                    seen_slots.add((wday, t))
                    slot_lt = LessonTime.objects.filter(school=local_journal.school, start_time=t).first()
                    if slot_lt:
                        WeeklyScheduleEntry.objects.create(
                            journal=local_journal,
                            weekday=wday,
                            lesson_time=slot_lt,
                            start_date=local_journal.academic_year.start_date,
                            end_date=local_journal.academic_year.end_date,
                        )

        # 3. Оцінки та відвідування
        for m in data['marks']:
            student = student_id_map.get(m['nz_student_id']) or student_id_map.get(m['student_name'])
            lesson = (
                lesson_map.get(m['nz_schedule_id']) or
                lesson_map.get((m['date'], m.get('lesson_number', 1))) or
                lesson_map.get(m['date'])
            )

            if not student or not lesson:
                continue

            raw_val = m.get('value', '').strip().upper()
            mark_id = m.get('mark_id')

            # Визначаємо чи це відвідування чи оцінка
            is_att = False
            att_status = None
            grade_val = ""

            if mark_id and mark_id in self.MARK_MAP_FROM_NZ:
                info = self.MARK_MAP_FROM_NZ[mark_id]
                if info[0] == 'attendance':
                    is_att = True
                    att_status = info[1]
                else:
                    grade_val = info[1]
            elif raw_val in ['Н', 'N']:
                is_att = True
                att_status = Attendance.Status.ABSENT_UNEXCUSED
            elif raw_val in ['ХВ', 'HB', 'БОЛ']:
                is_att = True
                att_status = Attendance.Status.ABSENT_EXCUSED
            elif raw_val in ['ЗП', 'ОП']:
                is_att = True
                att_status = Attendance.Status.LATE
            elif raw_val:
                grade_val = raw_val

            local_grade = Grade.objects.filter(lesson=lesson, student=student).first()
            local_attendance = Attendance.objects.filter(lesson=lesson, student=student).first()
            if local_grade and not local_grade.is_approved:
                stats['local_changes_preserved'] += 1
                continue
            if any(record and not record.is_synced_to_nz for record in (local_grade, local_attendance)):
                local_value = (local_attendance.get_symbol_ukr() if local_attendance else '') or (local_grade.value if local_grade else '')
                remote_value = {
                    Attendance.Status.ABSENT_UNEXCUSED: 'Н', Attendance.Status.ABSENT_EXCUSED: 'ХВ', Attendance.Status.LATE: 'ЗП',
                }.get(att_status, '') if is_att else grade_val
                if local_value != remote_value:
                    stats['local_changes_preserved'] += 1
                    continue
                # An exact read-back confirms the pending value without uploading it again.
                if local_grade:
                    Grade.objects.filter(pk=local_grade.pk).update(is_synced_to_nz=True, nz_last_synced_value=local_grade.value)
                if local_attendance:
                    Attendance.objects.filter(pk=local_attendance.pk).update(is_synced_to_nz=True, nz_last_synced_status=local_attendance.status)

            if is_att and att_status:
                att, _ = Attendance.objects.get_or_create(
                    lesson=lesson,
                    student=student,
                    defaults={
                        'status': att_status,
                        'is_synced_to_nz': True,
                        'nz_last_synced_status': att_status,
                    }
                )
                if not att.is_synced_to_nz or att.status != att_status:
                    att.status = att_status
                    att.is_synced_to_nz = True
                    att.nz_last_synced_status = att_status
                    att.save()
                Grade.objects.filter(lesson=lesson, student=student, is_synced_to_nz=True).update(value='', numeric_value=None, nz_last_synced_value='')
                stats['attendances_synced'] += 1

            elif grade_val:
                gr, _ = Grade.objects.get_or_create(
                    lesson=lesson,
                    student=student,
                    defaults={
                        'value': grade_val,
                        'is_synced_to_nz': True,
                        'nz_last_synced_value': grade_val,
                        'created_by': user,
                    }
                )
                if not gr.is_synced_to_nz or gr.value != grade_val:
                    gr.value = grade_val
                    gr.is_synced_to_nz = True
                    gr.nz_last_synced_value = grade_val
                    gr.save()
                Attendance.objects.filter(lesson=lesson, student=student, is_synced_to_nz=True).update(status=Attendance.Status.PRESENT, nz_last_synced_status=Attendance.Status.PRESENT)
                stats['grades_synced'] += 1

            else:
                Grade.objects.filter(lesson=lesson, student=student, is_synced_to_nz=True).update(
                    value='', numeric_value=None, nz_last_synced_value='')
                Attendance.objects.filter(lesson=lesson, student=student, is_synced_to_nz=True).update(
                    status=Attendance.Status.PRESENT, nz_last_synced_status=Attendance.Status.PRESENT)

        if replace:
            stats['replaced'] = True
            stats['removed'] = removed
            AuditLog.objects.create(school=local_journal.school, user=user, action=AuditLog.Action.IMPORT,
                target_model='Journal', target_id=str(local_journal.pk),
                description='Локальні уроки, план та оцінки замінено даними з nz.ua.',
                changes={'mode': 'replace', 'removed': removed, 'imported': dict(stats)})
        return stats

    def get_unsynced_changes(self, journal: Journal) -> dict:
        """
        Отримати перелік усіх несинхронізованих записів ДЛЯ ЦЬОГО ЖУРНАЛУ:
        - Оцінки (Grade де is_synced_to_nz=False)
        - Відвідування (Attendance де is_synced_to_nz=False)
        - Теми уроків (Lesson де is_synced_to_nz=False)
        """
        unsynced_lessons = list(
            Lesson.objects.filter(
                journal=journal,
                is_synced_to_nz=False,
                is_ignored_for_nz=False
            )
        )

        changes = []

        cells = self._pending_cells(journal)
        grades_count = 0
        attendance_count = 0
        for grade, attendance, active, value in cells:
            is_attendance = bool(attendance and attendance.get_symbol_ukr()) or grade is None
            kind = 'attendance' if is_attendance else 'grade'
            grades_count += not is_attendance
            attendance_count += is_attendance
            can_sync = bool(active.student.nz_student_id and active.lesson.nz_schedule_id and value in self.MARK_MAP_TO_NZ)
            changes.append({
                'type': kind, 'type_display': 'Відвідування' if is_attendance else 'Оцінка',
                'object_id': active.pk, 'student_name': active.student.get_short_name_ukr(),
                'date': active.lesson.date.strftime('%d.%m.%Y'),
                'old_value': (attendance.nz_last_synced_status if is_attendance and attendance else grade.nz_last_synced_value) or '—',
                'new_value': value or 'Очищено', 'can_sync': can_sync,
                'warning': '' if can_sync else ('Позначку не підтверджено в nz.ua; вона залишиться локально.'
                                              if value not in self.MARK_MAP_TO_NZ else 'Відсутній nz ID учня або уроку'),
            })

        # 3. Теми уроків
        from apps.journals.services.topic_copy import numbered_lessons
        all_journal_lessons = list(journal.lessons.select_related('linked_plan_topic').order_by('date', 'lesson_number', 'pk'))
        seq_numbers = {l.pk: number for number, l in numbered_lessons(all_journal_lessons).items()}
        for l in unsynced_lessons:
            l.sequence_number = seq_numbers.get(l.pk)
            has_lesson_id = bool(l.nz_schedule_id)
            warning = self._homework_sync_warning(l)
            cal_num = l.get_calendar_lesson_number()
            changes.append({
                'type': 'lesson_topic',
                'type_display': f'Тема/ДЗ (№{cal_num})',
                'object_id': l.id,
                'student_name': '—',
                'date': l.date.strftime('%d.%m.%Y'),
                'old_value': l.nz_last_synced_topic or '—',
                'new_value': l.topic,
                'old_homework': l.nz_last_synced_homework,
                'new_homework': l.homework,
                'homework_target': l.get_homework_target_label(),
                'can_sync': has_lesson_id and not warning,
                'warning': warning if has_lesson_id else "Урок ще не створений на nz.ua",
            })

        return {
            'journal_id': journal.id,
            'journal_title': str(journal),
            'nz_journal_id': journal.nz_journal_id,
            'is_linked_to_nz': bool(journal.nz_journal_id),
            'total_count': len(changes),
            'grades_count': grades_count,
            'attendances_count': attendance_count,
            'lessons_count': len(unsynced_lessons),
            'changes': changes,
            'blocked_grades_count': Grade.objects.filter(lesson__journal=journal, is_approved=False).count(),
        }

    @staticmethod
    def _due_lesson(lesson):
        return lesson._sync_due_lesson if hasattr(lesson, '_sync_due_lesson') else lesson.get_homework_due_lesson()

    @staticmethod
    def _homework_sync_warning(lesson):
        if lesson.homework:
            due = NZService._due_lesson(lesson)
            if not due or not due.nz_schedule_id:
                if not lesson.homework_due_date:
                    return 'Урок, на який задано ДЗ, ще не має онлайн-прив’язки.'
        return ''

    @staticmethod
    def _parse_lesson_date(text, base_year):
        """Parse only date-shaped data; never infer a due date from arbitrary text."""
        iso = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
        numeric = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', text)
        month_names = {'січ': 1, 'лют': 2, 'бер': 3, 'квіт': 4, 'трав': 5, 'черв': 6,
                       'лип': 7, 'серп': 8, 'вер': 9, 'жовт': 10, 'лист': 11, 'груд': 12}
        words = re.search(r'\b(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?', text.lower())
        short_num = re.search(r'\b(\d{1,2})\.(\d{1,2})\b', text)
        try:
            if iso:
                return date(*map(int, iso.groups()))
            if numeric:
                day, month, year = map(int, numeric.groups())
                return date(year, month, day)
            if words:
                month = next((m for prefix, m in month_names.items() if words[2].startswith(prefix)), None)
                if month:
                    year = int(words[3]) if words[3] else base_year + (month < 8)
                    return date(year, month, int(words[1]))
            if short_num:
                day, month = map(int, short_num.groups())
                if 1 <= month <= 12 and 1 <= day <= 31:
                    year = base_year + (month < 8)
                    return date(year, month, day)
        except ValueError:
            return None
        return None

    def _homework_target_fields(self, lesson, post_url, soup=None):
        due = self._due_lesson(lesson)
        due_date = due.date if due else lesson.homework_due_date
        if not (lesson.homework_for_next_lesson or lesson.homework_due_lesson_id or
                lesson.homework_due_plan_topic_id or lesson.homework_due_date or due or lesson.homework):
            return {}
        if not due_date:
            return {}
        if soup is None:
            response = self.session.get(post_url, timeout=15)
            if response.status_code != 200 or '/login' in str(response.url):
                raise NZServiceError('Не вдалося перевірити форму домашнього завдання nz.ua.')
            soup = BeautifulSoup(response.text, 'html.parser')
        # Dates and schedule IDs are both used by NZ forms. Only submit an option
        # actually supplied by that journal's form, never an invented parameter.
        id_matches, date_matches = [], []
        for select in soup.select('select[name]'):
            if not any(word in select['name'].lower() for word in ('home', 'task', 'schedule', 'due', 'date')):
                continue
            for option in select.select('option[value]:not([disabled])'):
                value = option['value']
                if due and due.nz_schedule_id and value == due.nz_schedule_id:
                    id_matches.append((select['name'], value))
                    continue
                parsed = self._parse_lesson_date(value, lesson.journal.academic_year.start_date.year)
                parsed = parsed or self._parse_lesson_date(option.get_text(' ', strip=True), lesson.journal.academic_year.start_date.year)
                if parsed == due_date:
                    date_matches.append((select['name'], value))
        matches = id_matches or date_matches
        if len(matches) != 1:
            if lesson.homework or lesson.homework_for_next_lesson or lesson.homework_due_lesson_id or lesson.homework_due_plan_topic_id:
                raise NZServiceError('Поле «Домашнє завдання на урок» у nz.ua не підтверджено. ДЗ залишено локально.')
            return {}
        return dict(matches)

    def _lesson_form_payload(self, lesson, post_url):
        response = self.session.get(post_url, timeout=15)
        if response.status_code != 200 or '/login' in str(response.url):
            raise NZAuthError('Не вдалося відкрити форму теми / ДЗ nz.ua. Оновіть підключення.')
        soup = BeautifulSoup(response.text, 'html.parser')
        forms = []
        for form in soup.select('form'):
            names = {el.get('name') for el in form.select('input[name], textarea[name]')}
            if {'OsvitaScheduleReal[lesson_topic]', 'OsvitaScheduleReal[hometask]'} <= names:
                forms.append(form)
        if len(forms) != 1:
            raise NZServiceError('Поля форми теми / ДЗ nz.ua не підтверджено. Зміни залишено локально.')
        form = forms[0]
        payload = {}
        for el in form.select('input[name], textarea[name], select[name]'):
            if el.has_attr('disabled') or el.get('type', '').lower() in ('file', 'submit', 'button', 'reset', 'password'):
                continue
            if el.get('type', '').lower() in ('checkbox', 'radio') and not el.has_attr('checked'):
                continue
            if el.name == 'select':
                option = el.select_one('option[selected]') or el.select_one('option:not([disabled])')
                payload[el['name']] = option.get('value', option.get_text()) if option else ''
            else:
                payload[el['name']] = el.get_text() if el.name == 'textarea' else el.get('value', '')
        token = self.extract_csrf(response.text)
        if not token:
            raise NZServiceError('Не вдалося перевірити CSRF форми nz.ua.')
        payload['_csrf'] = token
        payload['OsvitaScheduleReal[lesson_topic]'] = lesson.topic
        payload['OsvitaScheduleReal[hometask]'] = lesson.homework or ''
        cal_num = lesson.get_calendar_lesson_number()
        for field_name in list(payload.keys()):
            lower = field_name.lower()
            if any(k in lower for k in ['lesson_num', 'lesson_number', 'lesson_order', 'num_lesson', 'lessonnum', '[order]', '[number]']) and 'buzzer' not in lower:
                payload[field_name] = str(cal_num)
            if 'buzzer_id' in lower:
                buzzers = self._parse_buzzers(form)
                for b_id, b_info in buzzers.items():
                    if lesson.lesson_time and b_info['start_time'] == lesson.lesson_time:
                        payload[field_name] = b_id
                        break
                    elif lesson.lesson_number and b_info['number'] == lesson.lesson_number:
                        payload[field_name] = b_id
                        break
        payload.update(self._homework_target_fields(lesson, post_url, soup=form))
        return payload

    @staticmethod
    def _response_saved(response, expected_mark=None, expected_value=None):
        if response.status_code != 200 or '/login' in str(response.url):
            return False
        try:
            data = json.loads(response.text)
        except (ValueError, TypeError):
            return False
        if data is True:
            return True
        if not isinstance(data, dict) or data.get('success') is False or data.get('error') or 'message' in data:
            return False
        if expected_mark is not None and ('markValue' in data or 'markId' in data):
            if 'markValue' not in data or 'markId' not in data:
                return False
            # The saved NZ client treats these fields as the confirmed cell.
            actual = str(data['markValue'] or '').strip().upper()
            aliases = {'Н/З': 'НЕЗАР'}
            if aliases.get(actual, actual) != aliases.get(expected_value, expected_value):
                return False
            return str(data['markId']) == str(expected_mark) or (expected_mark == 0 and data['markId'] is None)
        return data.get('success') is True or data.get('status') in (True, 'success', 'ok') or data.get('result') is True

    def pending_changes_fingerprint(self, journal):
        lessons = list(journal.lessons.filter(is_synced_to_nz=False, is_ignored_for_nz=False).order_by('pk').values())
        targets = []
        for lesson in journal.lessons.filter(is_synced_to_nz=False, is_ignored_for_nz=False).order_by('pk'):
            due = lesson.get_homework_due_lesson()
            targets.append((lesson.pk, due.pk if due else None, due.nz_schedule_id if due else '',
                            str(due.date) if due else '', due.lesson_number if due else None))
        cells = [(g.pk if g else None, a.pk if a else None, value,
                  str(g.updated_at) if g else '', str(a.updated_at) if a else '',
                  active.student.nz_student_id, active.lesson.nz_schedule_id,
                  str(active.lesson.date), g.comment if g else '')
                 for g, a, active, value in self._pending_cells(journal)]
        return hashlib.sha256(json.dumps([lessons, targets, cells], sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _pending_cells(journal):
        ignored_student_ids = set(journal.ignored_students.values_list('pk', flat=True))
        keys = set(Grade.objects.filter(
            lesson__journal=journal, is_synced_to_nz=False, is_ignored_for_nz=False
        ).exclude(student_id__in=ignored_student_ids).values_list('lesson_id', 'student_id'))
        keys.update(Attendance.objects.filter(
            lesson__journal=journal, is_synced_to_nz=False, is_ignored_for_nz=False
        ).exclude(student_id__in=ignored_student_ids).values_list('lesson_id', 'student_id'))
        lesson_ids, student_ids = {k[0] for k in keys}, {k[1] for k in keys}
        grades = {(g.lesson_id, g.student_id): g for g in Grade.objects.filter(
            lesson_id__in=lesson_ids, student_id__in=student_ids, is_ignored_for_nz=False).select_related('lesson', 'student')}
        attendances = {(a.lesson_id, a.student_id): a for a in Attendance.objects.filter(
            lesson_id__in=lesson_ids, student_id__in=student_ids, is_ignored_for_nz=False).select_related('lesson', 'student')}
        cells = []
        for key in sorted(keys):
            grade, attendance = grades.get(key), attendances.get(key)
            if grade and not grade.is_approved:
                continue
            symbol = attendance.get_symbol_ukr() if attendance else ''
            active = attendance if symbol else (grade or attendance)
            value = symbol or (grade.value.strip().upper() if grade else '')
            cells.append((grade, attendance, active, value))
        return cells

    def push_journal_changes(self, journal: Journal, user=None, dry_run: bool = False) -> NZSyncLog:
        """
        Вивантажити ТІЛЬКИ ЗМІНЕНІ записи конкретного журналу на nz.ua:
        - Rate limiting: затримка 0.4с між запитами.
        - Dry Run: режим попереднього тестування без фізичної відправки.
        - Зберігає детальний NZSyncLog зі звітом.
        """
        if not journal.nz_journal_id:
            raise NZServiceError("Цей журнал не прив'язаний до журналу на nz.ua.")

        unsynced_lessons = list(
            Lesson.objects.filter(
                journal=journal,
                is_synced_to_nz=False,
                is_ignored_for_nz=False
            )
        )

        from apps.journals.services.topic_copy import numbered_lessons
        all_journal_lessons = list(journal.lessons.select_related('linked_plan_topic').order_by('date', 'lesson_number', 'pk'))
        seq_numbers = {l.pk: number for number, l in numbered_lessons(all_journal_lessons).items()}
        for lesson in all_journal_lessons:
            lesson.sequence_number = seq_numbers.get(lesson.pk)
        for lesson in unsynced_lessons:
            lesson.sequence_number = seq_numbers.get(lesson.pk)
            lesson._sync_due_lesson = self._due_lesson(lesson)
        cells = self._pending_cells(journal)
        total_items = len(cells) + len(unsynced_lessons)
        if not dry_run and total_items and not self.csrf_token:
            resp = self.session.get(f"{self.BASE_URL}/journal/index?journal={journal.nz_journal_id}", timeout=15)
            if resp.status_code != 200 or '/login' in str(resp.url) or 'LoginForm' in resp.text:
                raise NZAuthError('Сесія nz.ua недійсна. Онлайн-зміни не надсилалися.')
            if not self.extract_csrf(resp.text):
                raise NZServiceError('Не отримано CSRF nz.ua. Онлайн-зміни не надсилалися.')
        report_details = []
        success_count = 0
        error_count = 0

        # 1. Синхронізація тем уроків
        for l in unsynced_lessons:
            cal_num = l.get_calendar_lesson_number()
            item_desc = f"Тема уроку №{cal_num} від {l.date.strftime('%d.%m.%Y')}: «{l.topic}»"
            if not l.nz_schedule_id:
                error_count += 1
                report_details.append({
                    'type': 'lesson',
                    'item': item_desc,
                    'status': 'error',
                    'error': 'Урок не має ID розкладу (nz_schedule_id) на nz.ua'
                })
                continue

            warning = self._homework_sync_warning(l)
            if warning:
                error_count += 1
                report_details.append({'type': 'lesson', 'item': item_desc, 'status': 'error', 'error': warning})
                continue

            if dry_run:
                success_count += 1
                report_details.append({
                    'type': 'lesson',
                    'item': item_desc,
                    'status': 'dry_run_success',
                    'message': f"[Тестовий режим] Буде відправлено тему: {l.topic}"
                })
                continue

            # Фізична відправка на nz.ua
            try:
                time.sleep(self.REQUEST_DELAY_SECONDS)
                post_url = f"{self.BASE_URL}/journal/add-edit-home-task?schedule={l.nz_schedule_id}&journal={journal.nz_journal_id}"
                payload = self._lesson_form_payload(l, post_url)
                headers = {'X-CSRF-Token': self.csrf_token, 'X-Requested-With': 'XMLHttpRequest'}
                resp = self.session.post(post_url, data=payload, headers=headers, timeout=15)

                if self._response_saved(resp):
                    Lesson.objects.filter(pk=l.pk).update(nz_last_synced_topic=l.topic, nz_last_synced_homework=l.homework)
                    Lesson.objects.filter(pk=l.pk, updated_at=l.updated_at).update(is_synced_to_nz=True)
                    success_count += 1
                    report_details.append({
                        'type': 'lesson',
                        'item': item_desc,
                        'status': 'success',
                        'message': 'Тему успішно збережено в nz.ua'
                    })
                else:
                    error_count += 1
                    report_details.append({
                        'type': 'lesson',
                        'item': item_desc,
                        'status': 'error',
                        'error': f'nz.ua не підтвердив збереження (код {resp.status_code}). Запис залишено локально.'
                    })
            except Exception as e:
                error_count += 1
                report_details.append({
                    'type': 'lesson',
                    'item': item_desc,
                    'status': 'error',
                    'error': str(e)
                })

        # NZ has one mark per cell. A grade and attendance clear must never be sent separately.
        total_items = len(unsynced_lessons) + len(cells)
        for grade, attendance, active, value in cells:
            # Recheck approval immediately before any write, including direct service use.
            if grade and not Grade.objects.filter(pk=grade.pk, is_approved=True, updated_at=grade.updated_at).exists():
                error_count += 1
                report_details.append({'type': 'grade', 'item': 'Імпортована оцінка', 'status': 'error',
                    'error': 'Оцінка не схвалена або змінилася. Онлайн-запис не надіслано.'})
                continue
            symbol = attendance.get_symbol_ukr() if attendance else ''
            active = attendance if symbol else (grade or attendance)
            value = symbol or (grade.value.strip().upper() if grade else '')
            kind = 'attendance' if symbol or not grade else 'grade'
            description = f"Клітинка {active.student.get_short_name_ukr()} на {active.lesson.date:%d.%m.%Y}: «{value or 'Очищено'}»"
            if not active.student.nz_student_id or not active.lesson.nz_schedule_id:
                error_count += 1
                report_details.append({'type': kind, 'item': description, 'status': 'error', 'error': 'Відсутній ID учня або уроку на nz.ua'})
                continue
            mark_id = self.MARK_MAP_TO_NZ.get(value)
            if mark_id is None:
                error_count += 1
                report_details.append({'type': kind, 'item': description, 'status': 'error', 'error': 'Непідтримувана позначка; онлайн-оцінку не змінено.'})
                continue
            if dry_run:
                success_count += 1
                report_details.append({'type': kind, 'item': description, 'status': 'dry_run_success', 'message': '[Тестовий режим] Один запис клітинки.'})
                continue
            try:
                time.sleep(self.REQUEST_DELAY_SECONDS)
                payload = {
                    '_csrf': self.csrf_token, 'schedule': active.lesson.nz_schedule_id,
                    'student': active.student.nz_student_id, 'mark': mark_id,
                    # Existing NZ schedule IDs identify the column. Its saved
                    # client posts an empty lessonDate for these columns.
                    'lessonDate': '', 'comment': grade.comment if grade else '',
                }
                resp = self.session.post(f"{self.BASE_URL}/journal/set-mark", data=payload,
                    headers={'X-CSRF-Token': self.csrf_token, 'X-Requested-With': 'XMLHttpRequest'}, timeout=15)
                if not self._response_saved(resp, expected_mark=mark_id, expected_value=value):
                    raise NZServiceError(f'nz.ua не підтвердив збереження (код {resp.status_code}). Запис залишено локально.')
                with transaction.atomic():
                    # Only the exact sent snapshot becomes synced. Concurrent local edits survive.
                    if grade:
                        Grade.objects.filter(pk=grade.pk).update(nz_last_synced_value='' if symbol else grade.value)
                        Grade.objects.filter(pk=grade.pk, updated_at=grade.updated_at).update(is_synced_to_nz=True)
                    if attendance:
                        status = attendance.status if symbol else Attendance.Status.PRESENT
                        Attendance.objects.filter(pk=attendance.pk).update(nz_last_synced_status=status)
                        Attendance.objects.filter(pk=attendance.pk, updated_at=attendance.updated_at).update(is_synced_to_nz=True)
                success_count += 1
                report_details.append({'type': kind, 'item': description, 'status': 'success', 'message': 'nz.ua підтвердив збереження клітинки.'})
            except Exception as exc:
                error_count += 1
                report_details.append({'type': kind, 'item': description, 'status': 'error', 'error': str(exc)})

        # Підсумковий статус
        if error_count == 0:
            status = NZSyncLog.Status.SUCCESS
        elif success_count > 0:
            status = NZSyncLog.Status.PARTIAL
        else:
            status = NZSyncLog.Status.FAILED

        if not dry_run:
            journal.nz_last_sync_at = timezone.now()
            journal.save(update_fields=['nz_last_sync_at'])

        log = NZSyncLog.objects.create(
            journal=journal,
            user=user,
            status=status,
            items_count=total_items,
            success_count=success_count,
            error_count=error_count,
            details={'dry_run': dry_run, 'items': report_details},
            error_message="Є помилки при записі окремих рядків" if error_count > 0 else ""
        )

        return log
