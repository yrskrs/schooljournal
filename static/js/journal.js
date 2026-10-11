/**
 * Електронний шкільний журнал — Інтерактивна поведінка клієнта
 */

let currentActiveCellInput = null;
const JOURNAL_SUMMARY_TYPES = ['THEMATIC', 'MODULAR', 'THEMATIC_NUSH', 'SEMESTER_1', 'SEMESTER_2', 'SEMESTER_1_NUSH', 'SEMESTER_2_NUSH', 'ANNUAL'];
const JOURNAL_THEMATIC_TYPES = ['THEMATIC', 'MODULAR', 'THEMATIC_NUSH'];

document.addEventListener('DOMContentLoaded', function () {
  // 1. Пошук учнів на льоту
  const searchInput = document.getElementById('studentSearchInput');
  if (searchInput) {
    searchInput.addEventListener('input', function (e) {
      const q = e.target.value.toLowerCase().trim();
      const rows = document.querySelectorAll('.student-row');
      rows.forEach(function (row) {
        const name = row.getAttribute('data-student-name') || '';
        if (name.toLowerCase().includes(q)) {
          row.style.display = '';
        } else {
          row.style.display = 'none';
        }
      });
    });
  }

  // 2. Налаштування CSRF для HTMX
  document.body.addEventListener('htmx:configRequest', function (evt) {
    const csrfToken = getCookie('schooljournal_csrftoken');
    if (csrfToken) {
      evt.detail.headers['X-CSRFToken'] = csrfToken;
    }
  });

  // 3. Обробка помилок HTMX та виявлення конфліктів (HTTP 409)
  document.body.addEventListener('htmx:responseError', function (evt) {
    if (evt.detail.xhr.status === 409) {
      showToast('Увага: Конфлікт редагування! Дані клітинки були змінені іншим користувачем. Оновіть таблицю.', 'danger');
    } else if (evt.detail.xhr.status === 400) {
      showToast('Некоректне значення! Перевірте шкалу оцінювання (1-12) або позначку (Н, ХВ, ЗП).', 'warning');
    } else {
      showToast('Помилка сервера при збереженні. Спробуйте ще раз.', 'danger');
    }
  });

  // 4. Підсвічування успішного збереження
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    const target = evt.detail.target;
    if (target) {
      if (target.classList.contains('journal-single-cell-wrap') || target.classList.contains('cell-box')) {
        target.classList.add('grade-saved');
        setTimeout(function () {
          target.classList.remove('grade-saved');
        }, 900);
      }
    }
  });

  // 5. Ініціалізація швидкого багаторівневого селектора (Quick Picker)
  initQuickPicker();

  // 6. Клавіатурна навігація по комірках
  initCellKeyboardNav();

  // 7. Зв'язок колонок уроків з рядками в календарному плані
  initLessonLinks();
  initLessonScheduling();
  initAssessmentSources();
  initJournalActions();
});

// Швидкий селектор оцінок та відвідування (Quick Picker)
function initQuickPicker() {
  const picker = document.getElementById('journalQuickPicker');
  if (!picker) return;

  // Клік на кнопку пікера
  picker.addEventListener('click', function (e) {
    const btn = e.target.closest('.qp-btn');
    if (!btn || !currentActiveCellInput) return;

    e.preventDefault();
    e.stopPropagation();

    const val = btn.getAttribute('data-value');
    currentActiveCellInput.value = val;
    // Відправляємо зміну через HTMX
    if (window.htmx) {
      window.htmx.trigger(currentActiveCellInput, 'change');
    } else {
      currentActiveCellInput.dispatchEvent(new Event('change', { bubbles: true }));
    }
    closeQuickPicker();
  });

  // Відкриття пікера при фокусі на інпут комірки
  document.addEventListener('focusin', function (e) {
    const input = e.target.closest('.journal-cell-input');
    if (input) {
      openQuickPickerFor(input);
    }
  });

  document.addEventListener('click', function (e) {
    const input = e.target.closest('.journal-cell-input');
    if (input) {
      openQuickPickerFor(input);
      return;
    }
    // Якщо клік за межами пікера і не на інпуті — закриваємо
    if (!e.target.closest('#journalQuickPicker')) {
      closeQuickPicker();
    }
  });

  // Закриття по Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      closeQuickPicker();
    }
  });

  // Репозиціонування при скролі будь-якого контейнера або вікна
  window.addEventListener('scroll', function () {
    if (currentActiveCellInput && picker.style.display !== 'none') {
      const rect = currentActiveCellInput.getBoundingClientRect();
      if (rect.bottom < 60 || rect.top > window.innerHeight - 30) {
        closeQuickPicker();
      } else {
        positionQuickPicker(currentActiveCellInput);
      }
    }
  }, true);
}

function openQuickPickerFor(input) {
  currentActiveCellInput = input;
  const picker = document.getElementById('journalQuickPicker');
  if (!picker) return;

  const wrap = input.closest('.journal-single-cell-wrap');
  const studentNameEl = document.getElementById('qpStudentName');
  const lessonInfoEl = document.getElementById('qpLessonInfo');

  if (wrap) {
    const sName = wrap.getAttribute('data-student-name') || input.getAttribute('data-student-name') || 'Учень';
    const lInfo = wrap.getAttribute('data-lesson-info') || input.getAttribute('data-lesson-info') || 'Урок';
    if (studentNameEl) studentNameEl.textContent = sName;
    if (lessonInfoEl) lessonInfoEl.textContent = `Дата: ${lInfo}`;
  }

  // Підсвітка обраного значення
  const curVal = (input.value || '').trim().toUpperCase();
  picker.querySelectorAll('.qp-btn').forEach(function (btn) {
    if (btn.getAttribute('data-value') === curVal && curVal !== '') {
      btn.style.boxShadow = '0 0 0 2px var(--primary)';
    } else {
      btn.style.boxShadow = '';
    }
  });

  positionQuickPicker(input);
}

function positionQuickPicker(input) {
  const picker = document.getElementById('journalQuickPicker');
  if (!picker) return;

  picker.style.display = 'block';

  const rect = input.getBoundingClientRect();
  const pickerRect = picker.getBoundingClientRect();
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;

  let left = rect.left;
  if (rect.left + pickerRect.width > viewportWidth - 12) {
    left = viewportWidth - pickerRect.width - 12;
  }
  if (left < 10) left = 10;

  // Під коміркою, або зверху якщо не вміщається знизу
  let top = rect.bottom + 4;
  if (top + pickerRect.height > viewportHeight - 12) {
    top = Math.max(10, rect.top - pickerRect.height - 4);
  }

  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;
}

function closeQuickPicker() {
  const picker = document.getElementById('journalQuickPicker');
  if (picker) {
    picker.style.display = 'none';
  }
}

// Клавіатурна навігація: Enter, стрілки, Tab
function initCellKeyboardNav() {
  document.addEventListener('keydown', function (e) {
    const input = e.target.closest('.journal-cell-input');
    if (!input) return;

    const td = input.closest('td.journal-cell-td');
    if (!td) return;
    const tr = td.parentElement;
    const tbody = tr.parentElement;

    const colIndex = Array.prototype.indexOf.call(tr.children, td);
    const rows = Array.from(tbody.querySelectorAll('tr.student-row'));
    const rowIndex = rows.indexOf(tr);

    let nextInput = null;

    if (e.key === 'Enter' || e.key === 'ArrowDown') {
      e.preventDefault();
      input.dispatchEvent(new Event('change', { bubbles: true }));
      closeQuickPicker();
      if (rowIndex + 1 < rows.length) {
        const nextTr = rows[rowIndex + 1];
        const nextTd = nextTr.children[colIndex];
        if (nextTd) nextInput = nextTd.querySelector('.journal-cell-input');
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      input.dispatchEvent(new Event('change', { bubbles: true }));
      closeQuickPicker();
      if (rowIndex - 1 >= 0) {
        const prevTr = rows[rowIndex - 1];
        const prevTd = prevTr.children[colIndex];
        if (prevTd) nextInput = prevTd.querySelector('.journal-cell-input');
      }
    } else if (e.key === 'ArrowRight' && (input.selectionEnd === input.value.length || input.value === '')) {
      const nextTd = td.nextElementSibling;
      if (nextTd && nextTd.classList.contains('journal-cell-td')) {
        nextInput = nextTd.querySelector('.journal-cell-input');
      }
    } else if (e.key === 'ArrowLeft' && (input.selectionStart === 0 || input.value === '')) {
      const prevTd = td.previousElementSibling;
      if (prevTd && prevTd.classList.contains('journal-cell-td')) {
        nextInput = prevTd.querySelector('.journal-cell-input');
      }
    }

    if (nextInput) {
      nextInput.focus();
      nextInput.select();
    }
  });
}

// Зв'язок колонок уроків з календарним списком внизу
function initLessonLinks() {
  document.querySelectorAll('.lesson-th').forEach(function (th) {
    const lessonId = th.getAttribute('data-lesson-id');
    if (!lessonId) return;

    th.addEventListener('mouseenter', function () {
      const row = document.getElementById(`lesson-row-${lessonId}`);
      if (row) row.classList.add('highlighted');
    });
    th.addEventListener('mouseleave', function () {
      const row = document.getElementById(`lesson-row-${lessonId}`);
      if (row) row.classList.remove('highlighted');
    });
  });
}

// Допоміжні функції для модальних вікон
function openModal(id) {
  const el = document.getElementById(id);
  if (id === 'createLessonModal') updateCreateLessonFields();
  if (el) el.classList.add('open');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
  if (id === 'createLessonModal') {
    document.getElementById('insert_before_id').value = '';
    document.getElementById('insert_before_hint').textContent = '';
  }
}

function journalLessonData() {
  const element = document.getElementById('journal-lesson-data');
  return element ? JSON.parse(element.textContent) : [];
}

function lessonLabel(lesson) {
  const [year, month, day] = lesson.date.split('-');
  return `${day}.${month}.${year} · урок №${lesson.number} · ${lesson.topic}`;
}

function fillHomeworkChoices(select, date, number, excludedId, selectedValue) {
  if (!select) return;
  const previous = selectedValue !== undefined ? selectedValue : (select.options.length ? select.value : 'next');
  const summaries = JOURNAL_SUMMARY_TYPES;
  const lessons = journalLessonData();
  const days = new Set(/^\d{4}-\d{2}-\d{2}$/.test(date) ? [date] : []);
  if (select.id === 'create_homework_target' || select === document.getElementById('create_homework_target')) {
    lessonRepeatDates().dates.forEach(day => days.add(day));
  }
  lessons.filter(l => l.date >= date && !summaries.includes(l.type)).forEach(l => days.add(l.date));
  const plans = document.getElementById('plan_topic_select');
  const planned = plans ? Array.from(plans.options).filter(o => o.value && !summaries.includes(o.dataset.type) &&
    o.value !== document.getElementById('plan_topic_id_input').value) : [];
  planned.filter(o => o.dataset.date && o.dataset.date >= date).forEach(o => days.add(o.dataset.date));
  select.replaceChildren(new Option('Наступний урок (автоматично)', 'next'), new Option('Без дати виконання', ''));
  Array.from(days).sort().forEach(day => select.add(new Option(homeworkDateLabel(day), `date:${day}`)));
  planned.filter(o => !o.dataset.date).forEach(o => select.add(new Option(`Тема без дати: ${o.textContent.trim()}`, `plan:${o.value}`)));
  let selected = previous;
  if (selected && selected.startsWith('lesson:')) {
    const target = lessons.find(l => String(l.id) === selected.slice(7));
    if (target) selected = `date:${target.date}`;
  } else if (selected && selected.startsWith('plan:') && plans) {
    const target = Array.from(plans.options).find(o => o.value === selected.slice(5));
    if (target && target.dataset.date) selected = `date:${target.dataset.date}`;
  }
  // Retain a stored assignment while opening the editor; changing the source
  // date resets an unavailable choice instead of adding an earlier day.
  if (selectedValue !== undefined && selected && !Array.from(select.options).some(o => o.value === selected)) {
    const label = selected.startsWith('date:') ? `Раніше обрано: ${homeworkDateLabel(selected.slice(5))}` : 'Раніше обрана тема';
    select.add(new Option(label, selected));
  }
  select.value = Array.from(select.options).some(o => o.value === selected) ? selected : 'next';
}

function homeworkDateLabel(date) {
  const [year, month, day] = date.split('-');
  return `${day}.${month}.${year}`;
}

function lessonRepeatDates() {
  const start = document.getElementById('create_lesson_date')?.value || '';
  if (isSummaryCreateLesson()) return { dates: start ? [start] : [], error: '' };
  const frequency = document.getElementById('lesson_repeat_frequency')?.value || 'none';
  if (frequency === 'none') return { dates: start ? [start] : [], error: '' };
  const parse = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value ? null : date;
  };
  const first = parse(start);
  if (!first) return { dates: [], error: 'Оберіть дату першого уроку.' };
  const bounds = document.getElementById('createLessonForm')?.dataset || {};
  if ((bounds.yearStart && start < bounds.yearStart) || (bounds.yearEnd && start > bounds.yearEnd)) {
    return { dates: [], error: 'Повторення має залишатися в межах навчального року.' };
  }
  const interval = frequency === 'weekly' ? 1 : frequency === 'biweekly' ? 2 :
    frequency === 'custom' ? Number(document.getElementById('lesson_repeat_interval')?.value) : 0;
  if (!Number.isInteger(interval) || interval < 1 || interval > 52) return { dates: [], error: 'Вкажіть інтервал від 1 до 52 тижнів.' };
  const byDate = document.getElementById('lesson_repeat_end')?.value === 'date';
  const until = byDate ? parse(document.getElementById('lesson_repeat_until')?.value || '') : null;
  if (byDate && (!until || until < first)) return { dates: [], error: 'Кінцева дата має бути не раніше першого уроку.' };
  const count = byDate ? Math.floor((until - first) / (7 * interval * 86400000)) + 1 :
    Number(document.getElementById('lesson_repeat_count')?.value);
  if (!Number.isInteger(count) || count < (byDate ? 1 : 2) || count > 100) return { dates: [], error: 'Оберіть до 100 уроків; кількість включає перший.' };
  const dates = Array.from({ length: count }, (_, index) => {
    const current = new Date(first.getTime());
    current.setUTCDate(current.getUTCDate() + 7 * interval * index);
    return current.toISOString().slice(0, 10);
  });
  if (bounds.yearEnd && (dates[dates.length - 1] > bounds.yearEnd ||
      (byDate && document.getElementById('lesson_repeat_until').value > bounds.yearEnd))) {
    return { dates: [], error: 'Повторення має залишатися в межах навчального року.' };
  }
  return { dates, error: '' };
}

function updateLessonRecurrence() {
  if (isSummaryCreateLesson()) {
    updateCreateLessonMode();
    return;
  }
  const frequency = document.getElementById('lesson_repeat_frequency');
  if (!frequency) return;
  const active = frequency.value !== 'none';
  const end = document.getElementById('lesson_repeat_end');
  const byDate = end.value === 'date';
  document.getElementById('lessonRepeatOptions').hidden = !active;
  end.disabled = !active;
  for (const [inputId, groupId, enabled] of [
    ['lesson_repeat_interval', 'lessonRepeatIntervalField', active && frequency.value === 'custom'],
    ['lesson_repeat_count', 'lessonRepeatCountField', active && !byDate],
    ['lesson_repeat_until', 'lessonRepeatUntilField', active && byDate],
  ]) {
    const input = document.getElementById(inputId);
    input.disabled = !enabled;
    input.required = enabled;
    document.getElementById(groupId).hidden = !enabled;
  }
  document.getElementById('lesson_repeat_until').min = document.getElementById('create_lesson_date').value;
  const result = lessonRepeatDates();
  const preview = document.getElementById('lessonRepeatPreview');
  preview.classList.toggle('text-danger', Boolean(result.error));
  const shown = result.dates.slice(0, 6).map(homeworkDateLabel);
  preview.textContent = result.error || (active ? `Уроків: ${result.dates.length}. Дати: ${shown.join(', ')}${result.dates.length > 6 ? `, …, ${homeworkDateLabel(result.dates[result.dates.length - 1])}` : ''}. Для повторень номер на дату добирається автоматично.` : '');
  const submit = document.getElementById('lessonCreateSubmit');
  submit.disabled = active && Boolean(result.error);
  submit.textContent = active && !result.error ? `Створити уроки (${result.dates.length})` : 'Зберегти урок';
  fillHomeworkChoices(document.getElementById('create_homework_target'), document.getElementById('create_lesson_date').value,
    Number(document.getElementById('create_lesson_number').value));
}

function isSummaryCreateLesson() {
  return JOURNAL_SUMMARY_TYPES.includes(document.getElementById('create_lesson_type')?.value);
}

function updateCreateLessonMode() {
  const type = document.getElementById('create_lesson_type');
  if (!type) return false;
  const summary = isSummaryCreateLesson();
  for (const [id, enabled] of [['createLessonOrdinaryFields', !summary], ['createLessonSummaryFields', summary]]) {
    const fields = document.getElementById(id);
    fields.hidden = !enabled;
    fields.disabled = !enabled;
  }
  document.getElementById('createLessonNumberField').hidden = summary;
  const number = document.getElementById('create_lesson_number');
  number.disabled = summary;
  number.required = !summary;
  document.getElementById('insert_before_id').disabled = summary;
  document.getElementById('insert_before_hint').hidden = summary;
  document.getElementById('create_lesson_date_label').textContent = summary ? 'Відображати колонку в журналі після *' : 'Дата уроку *';
  const date = document.getElementById('create_lesson_date');
  const form = document.getElementById('createLessonForm');
  date.min = summary ? form.dataset.yearStart : '';
  date.max = summary ? form.dataset.yearEnd : '';
  document.getElementById('createLessonModalTitle').textContent = {
    THEMATIC: 'Додати тематичне оцінювання', SEMESTER_1: 'Додати оцінку за I семестр',
    SEMESTER_2: 'Додати оцінку за II семестр', ANNUAL: 'Додати річну оцінку',
    MODULAR: 'Додати модульне оцінювання', THEMATIC_NUSH: 'Додати тематичне оцінювання НУШ',
    SEMESTER_1_NUSH: 'Додати оцінку за I семестр НУШ', SEMESTER_2_NUSH: 'Додати оцінку за II семестр НУШ',
  }[type.value] || 'Додати проведений урок';
  const planId = document.getElementById('plan_topic_id_input');
  const plan = Array.from(document.getElementById('plan_topic_select')?.options || []).find(option => option.value === planId.value);
  if (planId.value && (!plan || (summary && plan.dataset.type !== type.value) ||
      (!summary && JOURNAL_SUMMARY_TYPES.includes(plan.dataset.type)))) planId.value = '';
  if (summary) {
    const submit = document.getElementById('lessonCreateSubmit');
    submit.disabled = false;
    submit.textContent = 'Створити колонку';
    const parameters = new URLSearchParams({grade_type: type.value, date: date.value,
      lesson_time: document.getElementById('create_lesson_time').value,
      title: document.getElementById('create_summary_title').value});
    document.getElementById('create_summary_calculate').href = `${form.dataset.assessmentUrl}?${parameters}`;
  }
  return summary;
}

function validateSummaryCalculation() {
  return document.getElementById('createLessonForm').reportValidity();
}

function updateCreateLessonFields() {
  const date = document.getElementById('create_lesson_date');
  if (!date) return;
  const number = document.getElementById('create_lesson_number');
  const beforeId = document.getElementById('insert_before_id').value;
  const before = journalLessonData().find(l => String(l.id) === beforeId);
  if (before && before.date !== date.value) {
    document.getElementById('insert_before_id').value = '';
    document.getElementById('insert_before_hint').textContent = '';
  }
  number.value = before && before.date === date.value ? before.number :
    Math.max(0, ...journalLessonData().filter(l => l.date === date.value).map(l => l.number)) + 1;
  if (updateCreateLessonMode()) return;
  fillHomeworkChoices(document.getElementById('create_homework_target'), date.value, Number(number.value));
  updateLessonRecurrence();
}

function initLessonScheduling() {
  document.getElementById('create_lesson_type')?.addEventListener('change', updateCreateLessonFields);
  ['create_lesson_time', 'create_summary_title'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', updateCreateLessonMode);
  });
  ['lesson_repeat_frequency', 'lesson_repeat_end', 'lesson_repeat_interval', 'lesson_repeat_count', 'lesson_repeat_until'].forEach(id => {
    const input = document.getElementById(id);
    if (input) {
      input.addEventListener('change', updateLessonRecurrence);
      input.addEventListener('input', updateLessonRecurrence);
    }
  });
  const date = document.getElementById('create_lesson_date');
  if (date) {
    date.addEventListener('change', updateCreateLessonFields);
    document.getElementById('create_lesson_number').addEventListener('change', function () {
      fillHomeworkChoices(document.getElementById('create_homework_target'), date.value, Number(this.value));
    });
    updateCreateLessonFields();
  }
  const editDate = document.getElementById('edit_date_input');
  if (editDate) {
    editDate.addEventListener('change', function () {
      document.getElementById('edit_number_input').value = Math.max(0, ...journalLessonData()
        .filter(l => l.date === this.value && String(l.id) !== document.getElementById('edit_lesson_id').value)
        .map(l => l.number)) + 1;
      fillHomeworkChoices(document.getElementById('edit_homework_target'), this.value,
        Number(document.getElementById('edit_number_input').value), document.getElementById('edit_lesson_id').value);
    });
    document.getElementById('edit_number_input').addEventListener('change', function () {
      fillHomeworkChoices(document.getElementById('edit_homework_target'), editDate.value,
        Number(this.value), document.getElementById('edit_lesson_id').value);
    });
  }
  const createForm = document.getElementById('createLessonForm');
  if (createForm?.dataset.scheduledDate) {
    document.getElementById('create_lesson_date').value = createForm.dataset.scheduledDate;
    selectSchoolLessonTime(document.getElementById('create_lesson_time'), createForm.dataset.scheduledTime);
    openModal('createLessonModal');
  }
}

function selectSchoolLessonTime(select, value, preserve = false) {
  if (!select) return;
  if (!select.options) { select.value = value || ''; return; }
  Array.from(select.options).filter(option => option.dataset.legacy).forEach(option => option.remove());
  if (preserve && value && !Array.from(select.options).some(option => option.value === value)) {
    const option = new Option(`Раніше збережено: ${value}`, value);
    option.dataset.legacy = 'true';
    select.add(option);
  }
  select.value = Array.from(select.options).some(option => option.value === value) ? value : '';
}

function selectPlanTopic(select) {
  const opt = select.options[select.selectedIndex];
  document.getElementById('plan_topic_id_input').value = select.value;
  if (!select.value) return;
  document.getElementById('lesson_topic_input').value = opt.dataset.title;
  document.getElementById('lesson_hw_input').value = opt.dataset.hw;
  document.getElementById('createLessonForm').elements.lesson_type.value = opt.dataset.type;
  const summaryTitle = document.getElementById('create_summary_title');
  if (summaryTitle) summaryTitle.value = JOURNAL_SUMMARY_TYPES.includes(opt.dataset.type) ? opt.dataset.title : '';
  if (opt.dataset.date) document.getElementById('create_lesson_date').value = opt.dataset.date;
  updateCreateLessonFields();
}

function createFromPlan(id) {
  const select = document.getElementById('plan_topic_select');
  select.value = id;
  selectPlanTopic(select);
  openModal('createLessonModal');
}

function insertBeforeLesson(id) {
  const lesson = journalLessonData().find(l => l.id === id);
  if (!lesson) return;
  document.getElementById('createLessonForm').reset();
  document.getElementById('create_lesson_date').value = lesson.date;
  document.getElementById('insert_before_id').value = id;
  document.getElementById('insert_before_hint').textContent = `Новий урок буде перед: ${lessonLabel(lesson)}. Наступні номери цього дня змістяться; оцінки збережуться.`;
  openModal('createLessonModal');
}

function openEditLessonModalById(id) {
  const lesson = journalLessonData().find(l => l.id === id);
  if (!lesson) return;
  const [year, month, day] = lesson.date.split('-');
  openEditLessonModal(id, `${day}.${month}.${year}`, lesson.number, lesson.topic, lesson.homework, lesson.type, lesson.notes);
  document.getElementById('edit_date_input').value = lesson.date;
  document.getElementById('edit_date_input').readOnly = lesson.nz_bound;
  document.getElementById('edit_date_hint').textContent = lesson.nz_bound ? 'Онлайн-дата: змініть її на nz.ua або обміняйте теми.' : 'Урок можна перенести на іншу дату.';
  document.getElementById('edit_number_input').value = lesson.number;
  selectSchoolLessonTime(document.getElementById('edit_time_input'), lesson.time, true);
  fillHomeworkChoices(document.getElementById('edit_homework_target'), lesson.date, lesson.number, lesson.id, lesson.homework_target);
}

function openSwapTopics(id) {
  const data = journalLessonData();
  const first = data.find(l => l.id === id);
  if (!first) return;
  document.getElementById('swap_first_id').value = id;
  document.getElementById('swap_first_label').textContent = lessonLabel(first);
  const select = document.getElementById('swap_second_id');
  select.replaceChildren(new Option('Оберіть урок', ''));
  data.filter(l => l.id !== id && !JOURNAL_SUMMARY_TYPES.includes(l.type))
    .forEach(l => select.add(new Option(lessonLabel(l), l.id)));
  openModal('swapTopicsModal');
}

function selectAssessmentLessons(selected) {
  document.querySelectorAll('#assessmentForm input[name="lesson_ids"]').forEach(input => { input.checked = selected && !input.disabled; });
}

let assessmentRequest = null;
let assessmentRequestVersion = 0;
let assessmentOpener = null;
let assessmentPreviousOverflow = '';

function closeAssessmentModal() {
  assessmentRequestVersion += 1;
  assessmentRequest?.abort();
  const modal = document.getElementById('assessmentModal');
  if (!modal?.classList.contains('open')) return;
  modal.classList.remove('open');
  document.body.style.overflow = assessmentPreviousOverflow;
  assessmentOpener?.focus();
}

async function loadAssessmentModal(url, options = {}) {
  assessmentRequest?.abort();
  assessmentRequest = new AbortController();
  const version = ++assessmentRequestVersion;
  const content = document.getElementById('assessmentModalContent');
  content.setAttribute('aria-busy', 'true');
  const buttons = Array.from(content.querySelectorAll('button[type="submit"]'));
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await fetch(url, { ...options, credentials: 'same-origin', signal: assessmentRequest.signal });
    if (!response.ok || response.redirected) throw new Error('Не вдалося завантажити розрахунок. Оновіть сторінку й спробуйте ще раз.');
    if (response.headers.get('content-type')?.includes('application/json')) {
      const result = await response.json();
      if (version === assessmentRequestVersion && result.redirect) window.location.assign(result.redirect);
      return;
    }
    const html = await response.text();
    if (version !== assessmentRequestVersion) return;
    content.innerHTML = html;
    if (!content.querySelector('#assessmentForm[data-compact]')) throw new Error('Не вдалося відкрити вікно розрахунку. Оновіть сторінку.');
    initCompactAssessment();
    const focusTarget = content.querySelector('.errorlist') || content.querySelector('#assessmentResult') || content.querySelector('#id_date');
    if (focusTarget) {
      if (!focusTarget.matches('input, select, button')) focusTarget.tabIndex = -1;
      focusTarget.focus();
    }
  } catch (error) {
    if (error.name === 'AbortError' || version !== assessmentRequestVersion) return;
    let message = content.querySelector('#assessmentModalError');
    if (!message) {
      message = document.createElement('p');
      message.className = 'alert alert-danger';
      message.setAttribute('role', 'alert');
      content.appendChild(message);
    }
    message.textContent = error.message;
    message.hidden = false;
  } finally {
    if (version === assessmentRequestVersion) {
      content.removeAttribute('aria-busy');
      buttons.forEach(button => { button.disabled = false; });
    }
  }
}

function openAssessmentModal(link) {
  const modal = document.getElementById('assessmentModal');
  if (!modal) return;
  assessmentOpener = link;
  const menu = link.closest('details');
  if (menu) { menu.open = false; assessmentOpener = menu.querySelector('summary'); }
  if (!modal.classList.contains('open')) assessmentPreviousOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  document.getElementById('assessmentModalContent').innerHTML = '<h2 id="assessmentModalTitle">Розрахунок оцінок</h2><p role="status">Завантаження…</p>';
  modal.classList.add('open');
  modal.querySelector('.modal-close').focus();
  const url = new URL(link.href, window.location.href);
  url.searchParams.set('modal', '1');
  loadAssessmentModal(url);
}

function initJournalActions() {
  const modal = document.getElementById('assessmentModal');
  if (!modal) return;
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-assessment-link]');
    if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      event.preventDefault();
      openAssessmentModal(link);
    }
    const menu = document.getElementById('journalActions');
    if (menu && !menu.contains(event.target)) menu.open = false;
    if (event.target === modal) closeAssessmentModal();
  });
  document.addEventListener('keydown', event => {
    if (!modal.classList.contains('open')) {
      if (event.key === 'Escape') document.getElementById('journalActions').open = false;
      return;
    }
    if (event.key === 'Escape') { event.preventDefault(); closeAssessmentModal(); }
    if (event.key === 'Tab') {
      const focusable = Array.from(modal.querySelectorAll('a, button, input, select, [tabindex]'))
        .filter(element => !element.disabled && element.tabIndex >= 0 && element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
}

function initCompactAssessment() {
  const form = document.getElementById('assessmentForm');
  if (!form?.dataset.compact) return;
  const types = Array.from(form.querySelectorAll('input[name="source_types"]'));
  const all = document.getElementById('assessmentSelectAll');
  const start = form.elements.start_date, end = form.elements.end_date;
  const periods = JSON.parse(document.getElementById('assessment-period-data').textContent);
  function invalidatePreview() {
    const result = document.getElementById('assessmentResult');
    if (result) result.hidden = true;
    if (form.elements.preview_token) form.elements.preview_token.value = '';
  }
  function updateSelection() {
    const count = types.filter(input => input.checked).length;
    all.checked = count === types.length;
    all.indeterminate = count > 0 && count < types.length;
  }
  function updateNote() {
    document.getElementById('assessmentPeriodNote').textContent = `Період розрахунку: ${homeworkDateLabel(start.value)} — ${homeworkDateLabel(end.value)}.`;
  }
  all.addEventListener('change', () => {
    types.forEach(input => { input.checked = all.checked; });
    updateSelection();
    invalidatePreview();
  });
  types.forEach(input => input.addEventListener('change', () => { updateSelection(); invalidatePreview(); }));
  form.elements.date.addEventListener('change', () => {
    const date = form.elements.date.value;
    if (!date || date < periods.year_start || date > periods.year_end) return;
    const semester = periods.semesters.find(s => s.start <= date && date <= s.end);
    const kind = form.elements.grade_type;
    if (kind.value.startsWith('SEMESTER_') && semester) kind.value = `SEMESTER_${semester.number}${kind.value.endsWith('_NUSH') ? '_NUSH' : ''}`;
    form.elements.semester_id.value = semester?.id || '';
    start.value = kind.value === 'ANNUAL' ? periods.year_start : semester?.start || periods.year_start;
    const kinds = JOURNAL_THEMATIC_TYPES.includes(kind.value) ? [kind.value] : kind.value.startsWith('SEMESTER_') ?
      (kind.value.endsWith('_NUSH') ? ['SEMESTER_1_NUSH', 'SEMESTER_2_NUSH'] : ['SEMESTER_1', 'SEMESTER_2']) : [];
    const previous = periods.boundaries.filter(b => kinds.includes(b.type) && start.value <= b.date && b.date <= date)
      .sort((a, b) => a.date.localeCompare(b.date) || a.number - b.number || a.id - b.id).pop();
    if (previous) start.value = previous.date;
    end.value = date;
    updateNote();
    invalidatePreview();
  });
  [start, end].forEach(input => input.addEventListener('change', () => { updateNote(); invalidatePreview(); }));
  form.elements.lesson_time.addEventListener('change', invalidatePreview);
  form.elements.date.min = periods.year_start;
  form.elements.date.max = periods.year_end;
  [start, end].forEach(input => { input.min = periods.year_start; input.max = periods.year_end; });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const body = new FormData(form);
    body.set('action', event.submitter?.value || 'preview');
    loadAssessmentModal(form.getAttribute('action'), { method: 'POST', body });
  });
  updateSelection();
  updateNote();
}

function initAssessmentSources() {
  const type = document.getElementById('id_grade_type');
  const source = document.getElementById('id_source');
  const data = document.getElementById('assessment-source-data');
  if (!type || !source || !data) return;
  const items = JSON.parse(data.textContent);
  const typeInputs = Array.from(document.querySelectorAll('#assessmentForm input[name="source_types"]'));
  const allowedType = value => source.value === 'all' ||
    (source.value === 'current' && ![...JOURNAL_SUMMARY_TYPES, 'ADJUSTED'].includes(value)) ||
    (source.value === 'thematic' && value === 'THEMATIC') ||
    (source.value === 'semester' && ['SEMESTER_1', 'SEMESTER_2'].includes(value));
  function selectSources(selectMatching = true) {
    const selectedTypes = typeInputs.filter(input => input.checked && !input.disabled).map(input => input.value);
    const start = document.getElementById('id_start_date')?.value || '';
    const end = document.getElementById('id_end_date')?.value || '';
    const allowed = items.filter(item => allowedType(item.type) &&
      (!typeInputs.length || selectedTypes.includes(item.type)) &&
      (!item.date || ((!start || item.date >= start) && (!end || item.date <= end)))).map(item => item.id);
    document.querySelectorAll('#assessmentForm input[name="lesson_ids"]').forEach(input => {
      const eligible = allowed.includes(input.value);
      input.disabled = !eligible;
      const label = input.closest('label');
      if (label) label.hidden = !eligible;
      if (!eligible) input.checked = false;
      else if (selectMatching && !input.value.startsWith('legacy:')) input.checked = true;
    });
  }
  function updateTypes(reset) {
    typeInputs.forEach(input => {
      const eligible = allowedType(input.value);
      input.disabled = !eligible;
      const label = input.closest('label');
      if (label) label.hidden = !eligible;
      if (reset) input.checked = eligible;
      else if (!eligible) input.checked = false;
    });
    const target = document.getElementById('id_target_lesson');
    if (target) {
      Array.from(target.options).forEach(option => {
        option.disabled = Boolean(option.value) && !items.some(item => item.id === option.value && item.type === type.value);
        option.hidden = option.disabled;
      });
      if (target.selectedIndex >= 0 && target.options[target.selectedIndex].disabled) target.value = '';
    }
  }
  type.addEventListener('change', function () {
    source.value = this.value === 'ANNUAL' ? 'semester' : JOURNAL_THEMATIC_TYPES.includes(this.value) ? 'current' : 'thematic';
    updateTypes(true);
    selectSources();
  });
  source.addEventListener('change', function () { updateTypes(true); selectSources(); });
  typeInputs.forEach(input => input.addEventListener('change', function () { selectSources(); }));
  ['id_start_date', 'id_end_date'].forEach(id => document.getElementById(id)?.addEventListener('change', function () { selectSources(); }));
  updateTypes(false);
  selectSources(false);
}

// Заповнення модального вікна редагування уроку
function openEditLessonModal(id, date, number, topic, hw, type, notes) {
  document.getElementById('edit_lesson_id').value = id;
  document.getElementById('edit_lesson_date').textContent = date;
  document.getElementById('edit_lesson_number').textContent = number;
  document.getElementById('edit_lesson_topic').value = topic;
  document.getElementById('edit_lesson_hw').value = hw;
  document.getElementById('edit_lesson_type').value = type;
  document.getElementById('edit_lesson_notes').value = notes;

  // Set form action dynamically
  const form = document.getElementById('editLessonForm');
  const actionTpl = form.getAttribute('data-action-template');
  form.action = actionTpl.replace('/lesson/0/edit/', `/lesson/${id}/edit/`);

  openModal('editLessonModal');
}

// Перемикач відвідуваності (legacy підтримка)
function cycleAttendance(btn, lessonId, studentId, journalId) {
  const current = btn.getAttribute('data-status') || 'PRESENT';
  const cycle = {
    'PRESENT': 'ABSENT_UNEXCUSED',
    'ABSENT_UNEXCUSED': 'ABSENT_EXCUSED',
    'ABSENT_EXCUSED': 'LATE',
    'LATE': 'PRESENT'
  };
  const nextStatus = cycle[current] || 'PRESENT';
  const clientVersion = btn.getAttribute('data-version') || '';

  const formData = new FormData();
  formData.append('lesson_id', lessonId);
  formData.append('student_id', studentId);
  formData.append('status', nextStatus);
  formData.append('client_version', clientVersion);

  fetch(`/journal/${journalId}/attendance/update/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('schooljournal_csrftoken')
    },
    body: formData
  })
  .then(function(res) {
    if (res.status === 409) {
      showToast('Конфлікт: відвідуваність була змінена іншим користувачем!', 'danger');
      return;
    }
    return res.text();
  })
  .then(function(html) {
    if (html) {
      const container = btn.closest('.attendance-cell-container');
      if (container) {
        container.outerHTML = html;
      }
    }
  })
  .catch(function(err) {
    console.error(err);
  });
}

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== '') {
    const cookies = document.cookie.split(';');
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === (name + '=')) {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

// Спливаючі сповіщення (Toast)
function showToast(message, type) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.position = 'fixed';
    container.style.bottom = '20px';
    container.style.right = '20px';
    container.style.zIndex = '9999';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.gap = '10px';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `alert alert-${type || 'info'}`;
  toast.style.margin = '0';
  toast.style.boxShadow = 'var(--shadow-lg)';
  toast.style.minWidth = '300px';
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(function () {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s ease';
    setTimeout(function () {
      toast.remove();
    }, 300);
  }, 4000);
}

// ==========================================
// Інтеграція з nz.ua (Нові знання)
// ==========================================

function openNzSyncModal(journalId) {
  let modalContainer = document.getElementById('nzSyncModalContainer');
  if (!modalContainer) {
    modalContainer = document.createElement('div');
    modalContainer.id = 'nzSyncModalContainer';
    document.body.appendChild(modalContainer);
  }

  modalContainer.innerHTML = `
    <div class="modal-overlay open" style="z-index: 1050; display: flex; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.6); backdrop-filter: blur(4px); align-items: center; justify-content: center; padding: 1rem;" onclick="if(event.target===this)closeNzSyncModal()">
      <div class="modal-card" style="max-width: 420px; width: 100%; text-align: center; padding: 2.25rem 1.5rem; border-radius: var(--radius-md); box-shadow: var(--shadow-lg); background: white;">
        <div class="spinner-border text-primary" role="status" style="width: 2.5rem; height: 2.5rem; margin: 0 auto 1rem; color: var(--primary);"></div>
        <div style="font-weight: 600; color: #1e3a8a; font-size: 1.05rem;">Завантаження незбережених змін...</div>
        <div style="font-size: 0.85rem; color: #64748b; margin-top: 0.35rem;">Опитування локальних даних та nz.ua</div>
      </div>
    </div>
  `;

  fetch(`/journal/${journalId}/nz/preview/`)
    .then(function (res) {
      if (!res.ok) throw new Error('Помилка завантаження прев\'ю nz.ua');
      return res.text();
    })
    .then(function (html) {
      modalContainer.innerHTML = html;
    })
    .catch(function (err) {
      modalContainer.innerHTML = '';
      showToast('Не вдалося відкрити діалог nz.ua: ' + err.message, 'danger');
    });
}

function closeNzSyncModal() {
  const container = document.getElementById('nzSyncModalContainer');
  if (container) {
    container.innerHTML = '';
  }
}

function ignoreNzRecord(journalId, itemType, objectId, btnElement) {
  if (btnElement) {
    btnElement.disabled = true;
    btnElement.textContent = '...';
  }
  const formData = new FormData();
  formData.append('item_type', itemType);
  formData.append('object_id', objectId);

  fetch(`/journal/${journalId}/nz/ignore-change/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('schooljournal_csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: formData
  })
    .then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, data: data };
      });
    })
    .then(function (resObj) {
      if (resObj.ok && resObj.data.status === 'ok') {
        showToast(resObj.data.message || 'Запис ігноровано при вивантаженні', 'success');
        openNzSyncModal(journalId);
      } else {
        throw new Error(resObj.data.message || 'Помилка при ігноруванні запису');
      }
    })
    .catch(function (err) {
      if (btnElement) {
        btnElement.disabled = false;
        btnElement.textContent = '🚫 Ігнорувати запис';
      }
      showToast('Помилка: ' + err.message, 'danger');
    });
}

function pullNzJournal(journalId) {
  const btn = document.getElementById('btnNzPull');
  const modalPullBtn = document.getElementById('nzModalPullBtn');

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true" style="width: 0.85rem; height: 0.85rem; margin-right: 0.25rem;"></span> Завантаження...';
  }
  if (modalPullBtn) {
    modalPullBtn.disabled = true;
    modalPullBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true" style="width: 0.85rem; height: 0.85rem; margin-right: 0.25rem;"></span> Завантаження з nz.ua...';
  }

  showToast('Завантаження оцінок та уроків з nz.ua...', 'info');

  const formData = new FormData();
  formData.append('format', 'json');

  fetch(`/journal/${journalId}/nz/import/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('schooljournal_csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: formData
  })
    .then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, data: data };
      });
    })
    .then(function (resObj) {
      if (resObj.ok && resObj.data.success) {
        showToast(resObj.data.message || 'Оцінки та уроки успішно оновлено з nz.ua!', 'success');
        setTimeout(function () {
          location.reload();
        }, 700);
      } else {
        throw new Error(resObj.data.error || resObj.data.message || 'Помилка при завантаженні даних з nz.ua');
      }
    })
    .catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span style="font-size: 1.1rem; line-height: 1;">⤓</span> <span>Оновити з nz.ua</span>';
      }
      if (modalPullBtn) {
        modalPullBtn.disabled = false;
        modalPullBtn.innerHTML = '⤓ Завантажити актуальні дані з nz.ua';
      }
      showToast('Не вдалося завантажити дані: ' + err.message, 'danger');
    });
}

function pullNzJournalFromFile(journalId, input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  const modalPullBtn = document.getElementById('nzModalPullBtn');
  if (modalPullBtn) {
    modalPullBtn.disabled = true;
    modalPullBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true" style="width: 0.85rem; height: 0.85rem; margin-right: 0.25rem;"></span> Імпорт з файлу...';
  }
  showToast('Імпорт розкладу дзвінків та уроків з файлу...', 'info');

  const formData = new FormData();
  formData.append('format', 'json');
  formData.append('html_file', file);

  fetch(`/journal/${journalId}/nz/import/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('schooljournal_csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: formData
  })
    .then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, data: data };
      });
    })
    .then(function (resObj) {
      if (resObj.ok && resObj.data.success) {
        showToast(resObj.data.message || 'Журнал успішно завантажено з файлу!', 'success');
        setTimeout(function () {
          location.reload();
        }, 700);
      } else {
        throw new Error(resObj.data.error || resObj.data.message || 'Помилка при імпорті з файлу');
      }
    })
    .catch(function (err) {
      if (modalPullBtn) {
        modalPullBtn.disabled = false;
        modalPullBtn.innerHTML = '⤓ Завантажити з nz.ua';
      }
      showToast('Не вдалося імпортувати файл: ' + err.message, 'danger');
    });
}

function submitNzSync(journalId) {
  const submitBtn = document.getElementById('nzSyncSubmitBtn');
  const dryRunCheckbox = document.getElementById('nzDryRunCheckbox');
  const loadingIndicator = document.getElementById('nzSyncLoadingIndicator');
  const previewTableBox = document.getElementById('nzSyncPreviewTableBox');
  const resultContainer = document.getElementById('nzSyncResultContainer');
  const resultBanner = document.getElementById('nzSyncResultBanner');
  const resultList = document.getElementById('nzSyncResultList');

  const isDryRun = dryRunCheckbox ? dryRunCheckbox.checked : false;

  if (submitBtn) {
    submitBtn.disabled = true;
  }
  if (loadingIndicator) {
    loadingIndicator.style.display = 'block';
  }
  if (previewTableBox) {
    previewTableBox.style.opacity = '0.5';
    previewTableBox.style.pointerEvents = 'none';
  }

  const formData = new FormData();
  formData.append('dry_run', isDryRun ? '1' : '0');
  const previewToken = document.getElementById('nzSyncPreviewToken');
  formData.append('preview_token', previewToken ? previewToken.value : '');

  fetch(`/journal/${journalId}/nz/sync/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('schooljournal_csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: formData
  })
    .then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, data: data };
      });
    })
    .then(function (resObj) {
      if (loadingIndicator) {
        loadingIndicator.style.display = 'none';
      }
      if (previewTableBox) {
        previewTableBox.style.display = 'none';
      }
      if (resultContainer) {
        resultContainer.style.display = 'block';
      }

      const data = resObj.data;

      if (resObj.ok && data.status === 'success') {
        const isSuccessFull = (data.error_count === 0);
        resultBanner.className = isSuccessFull ? 'alert alert-success' : 'alert alert-warning';
        resultBanner.style.background = isSuccessFull ? '#dcfce7' : '#fef3c7';
        resultBanner.style.color = isSuccessFull ? '#15803d' : '#b45309';
        resultBanner.style.border = isSuccessFull ? '1px solid #bbf7d0' : '1px solid #fde68a';
        resultBanner.innerHTML = `
          <div style="font-size: 1rem; font-weight: 700; margin-bottom: 0.25rem;">
            ${isSuccessFull ? '&#10004; ' : '&#9888; '} ${escapeJournalText(data.message)}
          </div>
          <div style="font-size: 0.85rem;">
            Всього елементів: <strong>${data.items_count}</strong> |
            Успішно: <strong style="color: #16a34a;">${data.success_count}</strong> |
            Помилок: <strong style="color: #dc2626;">${data.error_count}</strong>
            ${data.dry_run ? ' <span class="badge bg-secondary ms-1">Тестовий прогін</span>' : ''}
          </div>
        `;

        if (data.details && data.details.length > 0) {
          let listHtml = '<ul style="margin: 0; padding-left: 1.25rem;">';
          data.details.forEach(function (itm) {
            const isOk = (itm.status === 'success' || itm.status === 'dry_run_success');
            const sym = isOk ? '<span style="color: #16a34a; font-weight: bold;">&#10004;</span>' : '<span style="color: #dc2626; font-weight: bold;">&#10008;</span>';
            listHtml += `<li style="margin-bottom: 0.25rem;">${sym} ${escapeJournalText(itm.item)} ${itm.error ? `<span style="color: #dc2626;">(${escapeJournalText(itm.error)})</span>` : ''}</li>`;
          });
          listHtml += '</ul>';
          resultList.innerHTML = listHtml;
        }

        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.className = 'btn btn-success';
          submitBtn.innerHTML = '&#8635; Оновити сторінку';
          submitBtn.onclick = function () {
            location.reload();
          };
        }

        showToast(data.message, isSuccessFull ? 'success' : 'warning');

      } else {
        resultBanner.className = 'alert alert-danger';
        resultBanner.style.background = '#fee2e2';
        resultBanner.style.color = '#b91c1c';
        resultBanner.style.border = '1px solid #fecaca';
        resultBanner.innerHTML = `<strong>Помилка nz.ua:</strong> ${escapeJournalText(data.message || 'Не вдалося виконати синхронізацію')}`;
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = 'Спробувати знову';
        }
        showToast(data.message || 'Помилка синхронізації', 'danger');
      }
    })
    .catch(function (err) {
      if (loadingIndicator) loadingIndicator.style.display = 'none';
      if (resultContainer) resultContainer.style.display = 'block';
      resultBanner.className = 'alert alert-danger';
      resultBanner.innerHTML = `<strong>Мережева помилка:</strong> ${escapeJournalText(err.message)}`;
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = 'Спробувати знову';
      }
      showToast('Помилка: ' + err.message, 'danger');
    });
}

function escapeJournalText(value) {
  const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => entities[char]);
}

function toggleStudentIgnore(journalId, studentId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.style.opacity = '0.6';
  }
  const csrfToken = getCookie('schooljournal_csrftoken') || '';
  fetch(`/journal/${journalId}/student/${studentId}/toggle-ignore/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': csrfToken,
      'Accept': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
  })
    .then(function (response) {
      if (!response.ok) {
        throw new Error('Помилка сервера при зміні статусу учня');
      }
      return response.json();
    })
    .then(function (data) {
      if (data.status === 'ok') {
        showToast(data.message, 'success');
        window.location.reload();
      } else {
        throw new Error(data.message || 'Не вдалося змінити статус');
      }
    })
    .catch(function (err) {
      showToast(err.message, 'danger');
      if (btn) {
        btn.disabled = false;
        btn.style.opacity = '1';
      }
    });
}

function toggleStudentActionMenu(event, menuId) {
  if (event) {
    event.stopPropagation();
  }
  const menu = document.getElementById(menuId);
  const isShown = menu && menu.style.display === 'block';
  closeAllStudentActionMenus();
  if (menu && !isShown) {
    const trigger = event.currentTarget;
    // A sticky cell creates its own stacking context, and the table scroll
    // clips descendants. Place the menu outside both before positioning it.
    document.body.appendChild(menu);
    menu.studentMenuTrigger = trigger;
    menu.style.display = 'block';
    trigger.setAttribute('aria-expanded', 'true');
    const rect = trigger.getBoundingClientRect();
    const margin = 8;
    menu.style.maxHeight = `${Math.max(0, window.innerHeight - margin * 2)}px`;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))}px`;
    const top = rect.bottom + height + margin <= window.innerHeight ? rect.bottom : rect.top - height;
    menu.style.top = `${Math.max(margin, top)}px`;
    const firstAction = menu.querySelector('button');
    if (firstAction) firstAction.focus();
  }
}

function closeAllStudentActionMenus(restoreFocus = false) {
  document.querySelectorAll('.student-action-dropdown').forEach(function(el) {
    const wasShown = el.style.display === 'block';
    el.style.display = 'none';
    if (el.studentMenuTrigger) {
      el.studentMenuTrigger.setAttribute('aria-expanded', 'false');
      if (wasShown && restoreFocus) el.studentMenuTrigger.focus();
    }
  });
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.student-action-menu-wrap') && !e.target.closest('.student-action-dropdown')) {
    closeAllStudentActionMenus();
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeAllStudentActionMenus(true);
});
document.addEventListener('scroll', function(e) {
  if (!e.target.closest || !e.target.closest('.student-action-dropdown')) closeAllStudentActionMenus();
}, true);
window.addEventListener('resize', function() { closeAllStudentActionMenus(); });

