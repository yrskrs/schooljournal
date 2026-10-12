// Run with Node: node scripts/test_updates_ui.cjs
// Tests the real template script with API responses and a small DOM fixture.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
module.exports = async function verify(root, template) {
  const html = fs.readFileSync(path.join(root, template), 'utf8');
  const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1]
    .replace('{% if can_update %}true{% else %}false{% endif %}', 'true');
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      const attributes = new Map();
      const value = {hidden: false, open: false, disabled: false, textContent: '', innerHTML: '', value: '', style: {}, scrollHeight: 100, scrollTop: 0, clientHeight: 100,
        classList: {add: (...names) => names.forEach(n => classes.add(n)), toggle: (name, on) => on ? classes.add(name) : classes.delete(name), contains: name => classes.has(name)},
        setAttribute: (name, v) => attributes.set(name, v), removeAttribute: name => attributes.delete(name), getAttribute: name => attributes.get(name),
        querySelector: () => element(id + '-circle')};
      elements.set(id, value);
    }
    return elements.get(id);
  }
  let response = {state: 'idle'}, copied, posted;
  const context = vm.createContext({
    document: {getElementById: element, querySelector: () => ({value: 'synthetic-csrf'}), addEventListener() {}},
    FormData: class {constructor() {this.fields = {}} append(k, v) {this.fields[k] = v}},
    fetch: async (url, options) => {if (options.method === 'POST') posted = options; return {ok: true, json: async () => response}},
    navigator: {clipboard: {writeText: async value => {copied = value}}},
    confirm: () => true, setInterval: () => 1, clearInterval() {}, Date, console,
  });
  vm.runInContext(source, context);
  const success = {state: 'success', job_id: 'done', step: 5, progress: 100, current_version: '9.2.0', logs: ['first', 'second']};
  context.renderStatus(success);
  assert.equal(element('updateProgressSection').open, false, 'Stored success must be collapsed');
  assert.equal(element('progressPercentage').textContent, '100%');
  for (let n = 1; n <= 5; n++) {
    assert(element('step' + n).classList.contains('completed'), 'Every step must finish');
    assert(!element('step' + n).classList.contains('active'));
    assert.equal(element('step' + n + '-circle').textContent, '✓');
  }
  assert.equal(element('terminalBody').textContent, 'first\nsecond');
  await context.copyTerminalLogs();
  assert.equal(copied, 'first\nsecond');
  element('updateProgressSection').open = true;
  context.renderStatus(success);
  assert.equal(element('updateProgressSection').open, true, 'Repeated status must preserve user expansion');
  const running = {state: 'running', job_id: 'new', step: 5, progress: 100, current_version: '9.2.0', logs: []};
  context.renderStatus(running);
  assert.equal(element('progressPercentage').textContent, '99%', 'Running must not claim completion');
  assert.equal(element('updateProgressSection').open, true);
  assert(element('step5').classList.contains('active'));
  element('updateProgressSection').open = false;
  context.renderStatus(running);
  assert.equal(element('updateProgressSection').open, false, 'Polling must preserve manual collapse');
  context.renderStatus({...success, job_id: 'new'});
  assert.equal(element('updateProgressSection').open, false);
  response = {has_update: true, current_version: '9.2.0', latest_version: '9.3.0', releases: [{version: '9.3.0', title: '<unsafe>', changes: ['One change']}]};
  await context.checkUpdates();
  assert(!element('updateCheckResult').hidden);
  assert(element('updateCheckResult').innerHTML.includes('9.3.0'));
  assert(element('updateCheckResult').innerHTML.includes('&lt;unsafe&gt;'));
  assert(element('updateCheckResult').innerHTML.includes('btnStartUpdate'));
  assert(!element('btnCheckUpdates').disabled);
  response = {has_update: false, current_version: '9.3.0', latest_version: '9.3.0'};
  await context.checkUpdates();
  assert(element('updateCheckResult').innerHTML.includes('Новіших версій немає'));
  assert(!element('updateCheckResult').innerHTML.includes('btnStartUpdate'));
  response = {ok: false, error: 'GitHub unavailable'};
  await context.checkUpdates();
  assert(element('updateCheckResult').innerHTML.includes('GitHub unavailable'));
  assert(!element('btnCheckUpdates').disabled);
  context.renderStatus({state: 'error', job_id: 'failed', step: 4, progress: 80, current_version: '9.2.0', error: 'build failed', rolled_back: true, logs: ['restore done']});
  assert(element('updateProgressSection').open);
  assert(!element('rollbackNotice').hidden);
  assert(element('updateDiagnostics').value.includes('build failed\n\nrestore done'));
  await context.copyTerminalLogs();
  assert(copied.includes('build failed'));
  context.beginOperation('Перезбірка поточної версії', '9.2.0');
  context.renderStatus(success);
  assert.equal(element('progressTitle').textContent, 'Перезбірка поточної версії', 'Old status cannot override pending launch');
  vm.runInContext('launchPending = false; activeJobId = "active"', context);
  context.renderStatus({...success, job_id: 'old'});
  assert.equal(element('progressTitle').textContent, 'Перезбірка поточної версії', 'Unrelated job must be ignored');
  vm.runInContext('activeJobId = null; setBusy(false)', context);
  response = {ok: true, job_id: 'accepted'};
  await context.proceedWithUpdate('HEAD');
  assert.equal(posted.method, 'POST');
  const payload = typeof posted.body === 'string' ? JSON.parse(posted.body) : posted.body.fields;
  assert.equal(payload.ref, 'HEAD');
  assert.equal(payload.version, '9.2.0');
  assert.equal(element('progressTitle').textContent, 'Перезбірка поточної версії');
  console.log(path.basename(root) + ': update UI regression checks passed');
};
if (require.main === module) {
  module.exports(path.resolve(__dirname, '..'), 'templates/portal/admin/updates.html').catch(error => {console.error(error); process.exitCode = 1});
}
