// Exercise the real journal submit handler: JSON must never navigate to a POST-only URL.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {};
const navigations = [], toasts = [], pages = [];
let refreshes = 0, response;
const context = vm.createContext({
  document: {
    cookie: '',
    addEventListener: (name, callback) => { listeners[name] = callback; },
    body: { dataset: {}, addEventListener() {} }, querySelectorAll: () => [],
  },
  window: { addEventListener() {} },
  location: { origin: 'http://journal.invalid', pathname: '/journal/1/', search: '', assign: url => navigations.push(url) },
  FormData: class {}, URL, Date, Promise, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync('static/js/site.js', 'utf8'), context);
context.rememberPagePosition = () => {};
context.getCookie = () => '';
context.siteFetch = async () => response;
context.refreshJournal = async () => { refreshes += 1; };
context.showToast = (message, level) => toasts.push({ message, level });
context.applyJournalPage = html => pages.push(html);

async function submit(body, ok = true, contentType = 'application/json', url = 'http://journal.invalid/journal/1/connection/2/send-roster/', operation = '') {
  response = { ok, url, headers: { get: () => contentType }, json: async () => body, text: async () => body };
  // A control named action can shadow form.action; the attribute remains the route.
  const form = { method: 'post', action: { name: 'action' }, dataset: { syncOperation: operation }, hasAttribute: () => false, querySelector: () => null,
    closest: () => ({}), target: '', getAttribute: () => url };
  const button = { disabled: false };
  let prevented = false;
  await listeners.submit({ defaultPrevented: false, target: form, submitter: button, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(button.disabled, false);
  assert.equal(form.dataset.siteSubmitting, undefined);
}

(async () => {
  await submit({ success: true, message: 'Список передано.' });
  assert.equal(refreshes, 1);
  assert.deepEqual(navigations, []);
  assert.deepEqual(toasts.pop(), { message: 'Список передано.', level: 'success' });

  await submit({ success: false, message: 'Перевірте відповідність учня.' }, false);
  assert.deepEqual(toasts.pop(), { message: 'Перевірте відповідність учня.', level: 'danger' });
  assert.equal(refreshes, 1);
  assert.deepEqual(navigations, []);

  await submit({ success: true, message: 'Потрібна перевірка.', level: 'warning' });
  assert.equal(toasts.pop().level, 'warning');
  assert.equal(refreshes, 2);

  await submit({ message: 'Передачу не підтверджено.' });
  assert.equal(toasts.pop().level, 'danger');
  assert.equal(refreshes, 2);

  await submit('<html>fresh journal</html>', true, 'text/html', 'http://journal.invalid/journal/1/');
  assert.deepEqual(pages, ['<html>fresh journal</html>']);
  assert.deepEqual(navigations, []);

  await submit('<html>review</html>', true, 'text/html', 'http://journal.invalid/journal/1/external-grades/');
  assert.deepEqual(navigations, ['http://journal.invalid/journal/1/external-grades/']);
  await submit({ success: true, operation: 'roster', message: 'Список передано.' }, true, 'application/json', undefined, 'roster');
  assert.equal(refreshes, 3);
  assert.equal(toasts.pop().message, 'Список передано.');

  await submit({ success: true, operation: 'grades', message: '📥 Оцінки: додано 0.', level: 'warning' }, true, 'application/json', 'http://journal.invalid/journal/1/pull/schoolwork/', 'grades');
  assert.equal(refreshes, 4);
  assert.deepEqual(toasts.pop(), { message: '📥 Оцінки: додано 0.', level: 'warning' });

  await submit({ success: true, operation: 'roster', message: 'Список передано.' }, true, 'application/json', undefined, 'grades');
  assert.equal(refreshes, 4);
  assert.equal(toasts.pop().level, 'danger');
  assert.equal(toasts.some(toast => toast.message === 'Список передано.'), false);
  console.log('PASS: 9 journal response scenarios, including operation identity and shadowed form.action');
})().catch(error => { console.error(error); process.exitCode = 1; });
