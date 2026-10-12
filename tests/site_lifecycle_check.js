// Isolated lifecycle tests: concurrent requests, errors, cancellation and timeout.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const indicator = { hidden: true };
const label = { textContent: '' };
const timers = new Map();
let timerId = 0;
let fetchImpl;
const context = vm.createContext({
  document: {
    getElementById: id => ({ siteBusy: indicator, siteBusyText: label }[id]),
    addEventListener() {}, body: { dataset: {}, addEventListener() {} }, querySelectorAll: () => [],
  },
  window: { addEventListener() {} }, AbortController, URL, Date, Promise,
  location: { pathname: '/journal/1/' },
  setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
  clearTimeout: id => timers.delete(id),
  fetch: (...args) => fetchImpl(...args),
});
vm.runInContext(fs.readFileSync('static/js/site.js', 'utf8'), context);
const request = () => vm.runInContext('siteFetch("/synthetic/")', context);
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const response = { clone: () => ({ arrayBuffer: async () => new ArrayBuffer(0) }) };

(async () => {
  const completions = [];
  fetchImpl = () => new Promise(resolve => completions.push(resolve));
  const first = request(), second = request();
  assert.equal(indicator.hidden, false);
  completions[0](response); await first;
  assert.equal(indicator.hidden, false, 'One finished request cannot hide another request');
  completions[1](response); await second;
  assert.equal(indicator.hidden, true); assert.equal(timers.size, 0);

  fetchImpl = async () => { throw new Error('synthetic network failure'); };
  await assert.rejects(request(), /network failure/);
  assert.equal(indicator.hidden, true); assert.equal(timers.size, 0);

  let finishBody;
  fetchImpl = async () => ({ clone: () => ({ arrayBuffer: () => new Promise(resolve => { finishBody = resolve; }) }) });
  const body = request(); await tick();
  assert.equal(indicator.hidden, false, 'The response body belongs to the loading lifecycle');
  finishBody(new ArrayBuffer(0)); await body;
  assert.equal(indicator.hidden, true);

  fetchImpl = () => new Promise(() => {});
  const stalled = request();
  const rejected = assert.rejects(stalled, /Час очікування вичерпано/);
  [...timers.values()].forEach(callback => callback());
  await rejected;
  assert.equal(indicator.hidden, true); assert.equal(timers.size, 0);

  fetchImpl = (_, options) => new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('cancelled'))));
  context.external = new AbortController();
  const cancelled = vm.runInContext('siteFetch("/synthetic/", { signal: external.signal })', context);
  context.external.abort();
  await assert.rejects(cancelled, /cancelled/);
  assert.equal(indicator.hidden, true); assert.equal(timers.size, 0);
  const offscreen = { id: 'lesson-row-1', getBoundingClientRect: () => ({ top: 1500, bottom: 1550 }) };
  context.window.innerHeight = 800;
  context.window.scrollY = 700; context.window.scrollX = 0;
  context.document.querySelectorAll = selector => selector.startsWith('[id^=') ? [offscreen] : [];
  let position = vm.runInContext('capturePagePosition()', context);
  assert.equal(position.anchor, undefined, 'An offscreen calendar row cannot change page position after reload');
  offscreen.getBoundingClientRect = () => ({ top: 100, bottom: 150 });
  position = vm.runInContext('capturePagePosition()', context);
  assert.equal(position.anchor, offscreen.id);
  assert.equal(position.offset, 100);
  console.log('PASS: 16 request lifecycle and scroll checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
