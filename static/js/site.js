/* Request lifecycle and page position. No journal data is stored in the browser. */
const SITE_REQUEST_TIMEOUT = 120000;
const siteBusyTasks = new Map();
let siteBusySequence = 0;

function renderSiteBusy() {
  const indicator = document.getElementById('siteBusy');
  if (!indicator) return;
  indicator.hidden = siteBusyTasks.size === 0;
  document.getElementById('siteBusyText').textContent = Array.from(siteBusyTasks.values()).at(-1)?.label || '';
}

function beginSiteBusy(label = 'Обробка запиту…') {
  const id = ++siteBusySequence;
  const timer = setTimeout(() => { siteBusyTasks.delete(id); renderSiteBusy(); }, SITE_REQUEST_TIMEOUT);
  siteBusyTasks.set(id, { label, timer });
  renderSiteBusy();
  return () => {
    clearTimeout(timer);
    siteBusyTasks.delete(id);
    renderSiteBusy();
  };
}

async function siteFetch(url, options = {}, label = 'Завантаження…') {
  if (label === 'Завантаження…') {
    const path = String(url);
    if (path.includes('/nz/sync/')) label = 'Вивантаження оцінок у nz.ua…';
    else if (path.includes('/nz/import/')) label = 'Завантаження даних журналу…';
    else if (path.includes('/toggle-ignore/')) label = 'Оновлення статусу учня та списків…';
    else if (options.method?.toUpperCase() === 'POST') label = 'Збереження змін…';
  }
  const finish = beginSiteBusy(label);
  const controller = new AbortController();
  const signal = options.signal;
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener('abort', abort, { once: true });
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error('Час очікування вичерпано. Перевірте стан журналу перед повторною дією.'));
      }, SITE_REQUEST_TIMEOUT);
    });
    return await Promise.race([
      (async () => {
        const response = await fetch(url, { ...options, signal: controller.signal });
        // Include the response body in the lifecycle, not just the headers.
        await response.clone().arrayBuffer();
        return response;
      })(), timeout,
    ]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    finish();
  }
}

function capturePagePosition() {
  const containers = Array.from(document.querySelectorAll('.journal-scroll-wrapper, .table-responsive'));
  const anchor = Array.from(document.querySelectorAll('[id^="lesson-row-"], [id^="plan-row-"]'))
    .find(element => {
      const rect = element.getBoundingClientRect();
      return rect.bottom > 0 && rect.top < window.innerHeight;
    });
  return { x: window.scrollX, y: window.scrollY,
    anchor: anchor?.id, offset: anchor?.getBoundingClientRect().top,
    containers: containers.map(element => ({ x: element.scrollLeft, y: element.scrollTop })) };
}

function restorePagePosition(position) {
  if (!position) return;
  document.querySelectorAll('.journal-scroll-wrapper, .table-responsive').forEach((element, index) => {
    element.scrollLeft = position.containers?.[index]?.x || 0;
    element.scrollTop = position.containers?.[index]?.y || 0;
  });
  const anchor = position.anchor && document.getElementById(position.anchor);
  const y = anchor ? window.scrollY + anchor.getBoundingClientRect().top - position.offset : position.y;
  window.scrollTo(position.x || 0, y || 0);
}

function positionKey() {
  return `schooljournal-position:${document.body.dataset.userId || 'public'}:${location.pathname}`;
}

function rememberPagePosition() {
  try { sessionStorage.setItem(positionKey(), JSON.stringify({ ...capturePagePosition(), savedAt: Date.now() })); }
  catch (_) { /* Storage can be disabled; the action must still work. */ }
}

let journalRefreshPending = null;

function applyJournalPage(html) {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const incoming = parsed.getElementById('journalLiveContent');
  const current = document.getElementById('journalLiveContent');
  if (!incoming || !current || incoming.dataset.journalId !== current.dataset.journalId) {
    throw new Error('Не вдалося оновити журнал. Перевірте вхід і повторіть оновлення.');
  }
  const position = capturePagePosition();
  const search = document.getElementById('studentSearchInput')?.value || '';
  const toolsOpen = document.getElementById('journalTools')?.open || false;
  const syncOpen = document.getElementById('journalSyncDetails')?.open || false;
  const focusedId = document.activeElement?.id;
  document.querySelectorAll('body > .student-action-dropdown').forEach(menu => menu.remove());
  current.replaceWith(incoming);
  const syncDetails = document.getElementById('journalSyncDetails');
  if (syncDetails) syncDetails.open = syncOpen;
  const noticeLink = document.querySelector('[data-api-notifications]');
  const newNoticeLink = parsed.querySelector('[data-api-notifications]');
  if (noticeLink && newNoticeLink) noticeLink.replaceChildren(...Array.from(newNoticeLink.childNodes));
  // Refresh server messages without removing client notifications.
  document.getElementById('messages-container')?.remove();
  const messages = parsed.getElementById('messages-container');
  if (messages) incoming.before(messages);
  document.body.style.overflow = '';
  window.htmx?.process(incoming);
  document.dispatchEvent(new Event('journal:refreshed'));
  const input = document.getElementById('studentSearchInput');
  if (input) { input.value = search; input.dispatchEvent(new Event('input', { bubbles: true })); }
  const tools = document.getElementById('journalTools');
  if (tools) tools.open = toolsOpen;
  if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
  restorePagePosition(position);
}

async function refreshJournal() {
  if (journalRefreshPending) return journalRefreshPending;
  journalRefreshPending = (async () => {
    const response = await siteFetch(location.href, { headers: { 'X-Requested-With': 'XMLHttpRequest' } }, 'Оновлення журналу…');
    if (!response.ok || response.redirected) throw new Error('Не вдалося оновити журнал. Перевірте вхід і повторіть.');
    applyJournalPage(await response.text());
  })();
  try { return await journalRefreshPending; }
  finally { journalRefreshPending = null; }
}

document.addEventListener('DOMContentLoaded', () => {
  try {
    const key = positionKey();
    const position = JSON.parse(sessionStorage.getItem(key) || 'null');
    sessionStorage.removeItem(key);
    if (!location.hash && position && Date.now() - position.savedAt < 30 * 60 * 1000) {
      restorePagePosition(position);
      requestAnimationFrame(() => restorePagePosition(position));
    }
  } catch (_) { /* Unavailable or stale storage is harmless. */ }
  revealPageTarget();
  const htmxRequests = new Map();
  if (window.htmx) window.htmx.config.timeout = SITE_REQUEST_TIMEOUT;
  document.body.addEventListener('htmx:beforeRequest', event => {
    htmxRequests.set(event.detail.xhr, beginSiteBusy('Збереження…'));
  });
  const finishHtmx = event => {
    htmxRequests.get(event.detail.xhr)?.();
    htmxRequests.delete(event.detail.xhr);
  };
  ['htmx:afterRequest', 'htmx:sendError', 'htmx:timeout', 'htmx:sendAbort'].forEach(name => {
    document.body.addEventListener(name, finishHtmx);
  });
  document.body.addEventListener('htmx:timeout', () => showToast('Час очікування збереження вичерпано. Оновіть дані перед повторною дією.', 'warning'));
});

function revealPageTarget() {
  if (!location.hash) return;
  let id;
  try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
  const target = document.getElementById(id);
  if (!target) return;
  let element = target;
  while (element) {
    if (element.tagName === 'DETAILS') element.open = true;
    element = element.parentElement;
  }
  target.scrollIntoView({block: 'start'});
}

window.addEventListener('hashchange', revealPageTarget);

window.addEventListener('pagehide', rememberPagePosition);
window.addEventListener('pageshow', () => {
  siteBusyTasks.forEach(task => clearTimeout(task.timer));
  siteBusyTasks.clear();
  renderSiteBusy();
  document.querySelectorAll('[data-site-submitting]').forEach(form => delete form.dataset.siteSubmitting);
});

document.addEventListener('submit', async event => {
  if (event.defaultPrevented) return;
  const form = event.target;
  if (form.method.toLowerCase() !== 'post' || form.hasAttribute('hx-post') || form.querySelector('[hx-post]')) return;
  if (form.dataset.siteSubmitting) { event.preventDefault(); return; }
  rememberPagePosition();
  // Journal mutations return fresh HTML through the existing authenticated POST routes.
  if (!form.closest('#journalLiveContent') || form.target === '_blank') {
    beginSiteBusy('Збереження…');
    return;
  }
  event.preventDefault();
  const data = new FormData(form, event.submitter);
  form.dataset.siteSubmitting = 'true';
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    const response = await siteFetch(form.getAttribute('action') || location.href, {
      method: 'POST', body: data,
      headers: { 'X-CSRFToken': getCookie('schooljournal_csrftoken'), 'X-Requested-With': 'XMLHttpRequest' },
    }, 'Збереження змін…');
    if ((response.headers.get('Content-Type') || '').includes('application/json')) {
      const result = await response.json();
      if (form.dataset.syncOperation && result.operation !== form.dataset.syncOperation) {
        throw new Error('Отримано відповідь іншої операції. Оновіть журнал і повторіть дію.');
      }
      if (!response.ok || result.success !== true) {
        if (form.dataset.syncOperation && result.attention_url) {
          try { await refreshJournal(); } catch (_) { /* Keep the operation error visible. */ }
          showSyncAttention(result.attention_url);
        }
        throw new Error(result.message || 'Не вдалося зберегти зміни. Перевірте дані й повторіть.');
      }
      await refreshJournal();
      if (form.dataset.syncOperation && result.level === 'warning' && result.attention_url) showSyncAttention(result.attention_url);
      showToast(result.message || 'Зміни збережено.', result.level || 'success');
      return;
    }
    if (!response.ok) throw new Error('Не вдалося зберегти зміни. Перевірте дані й повторіть.');
    const target = new URL(response.url);
    if (target.origin !== location.origin || target.pathname !== location.pathname) {
      location.assign(response.url);
      return;
    }
    // Preserve filters and the messages carried by the POST redirect.
    const html = await response.text();
    if (!location.search || target.search === location.search) {
      applyJournalPage(html);
    } else {
      const messages = new DOMParser().parseFromString(html, 'text/html').getElementById('messages-container');
      await refreshJournal();
      if (messages) {
        document.getElementById('messages-container')?.remove();
        document.getElementById('journalLiveContent').before(messages);
      }
    }
  } catch (error) { showToast(error.message, 'danger'); }
  finally {
    delete form.dataset.siteSubmitting;
    if (button) button.disabled = false;
  }
});

function showSyncAttention(url) {
  const container = document.getElementById('journalSyncAttention');
  if (!container) return;
  const target = new URL(url, location.href);
  if (target.origin !== location.origin) return;
  const link = document.createElement('a');
  link.href = target.href;
  link.className = 'api-attention-banner';
  link.textContent = 'Є питання після обміну. Відкрити сповіщення та виправити →';
  container.replaceChildren(link);
}
