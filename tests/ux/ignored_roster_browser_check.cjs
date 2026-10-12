// Synthetic Docker fixture only; exercise the reported class page and both receivers.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ids = JSON.parse(fs.readFileSync('/artifacts/ignore-fixture.json', 'utf8'));
let checks = 0;
let browser;
function check(value, message) { assert.ok(value, message); checks++; console.log('PASS:', message); }

(async () => {
  browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const context = await browser.newContext();
  const journal = await context.newPage(), work = await context.newPage(), test = await context.newPage();
  const errors = [];
  for (const page of [journal, work, test]) page.on('pageerror', error => errors.push(error.message));
  await journal.goto('http://127.0.0.1:19185/accounts/login/');
  await journal.locator('[name=username]').fill('synthetic_teacher');
  await journal.locator('#id_password').fill('synthetic-test');
  await Promise.all([journal.waitForURL(value => !value.pathname.includes('login')), journal.locator('form:has(#id_password) button[type=submit]').click()]);
  for (const [page, url] of [[work, 'http://127.0.0.1:19100/teacher/login/'], [test, 'http://127.0.0.1:19180/teacher/login']]) {
    await page.goto(url);
    await page.locator('[name=username]').fill('synthetic_teacher');
    await page.locator('[name=password]').fill('synthetic-test');
    await Promise.all([page.waitForURL(value => !value.pathname.includes('login')), page.locator('button[type=submit]').click()]);
  }
  await journal.goto(`http://127.0.0.1:19185/journal/${ids.journal}/`);
  async function verifyLists(expected) {
    await work.goto(`http://127.0.0.1:19100/teacher/students/?class_group=${ids.classes.schoolwork}`);
    check(await work.locator(`#student-row-${ids.native.work}`).count() === expected, 'Task teacher class page has the expected pupil row');
    const heading = work.locator('text=Список учнів класу').first();
    check((await heading.innerText()).includes(`(${expected})`), 'Task teacher class heading reports the active count');
    await test.goto('http://127.0.0.1:19180/teacher/roster-sync');
    check(await test.locator(`#student-view-${ids.native.test}`).count() === expected, 'Test teacher class page has the expected pupil row');
  }
  async function toggle(ignored) {
    await journal.locator(`.student-row[data-student-id="${ids.pupil}"] .student-number-btn`).click();
    await journal.locator(`#studentMenu_${ids.pupil} [role=menuitemcheckbox]`).click();
    await journal.waitForFunction(({pupil, ignored}) =>
      document.querySelector(`.student-row[data-student-id="${pupil}"]`)?.classList.contains('row-student-ignored') === ignored,
      {pupil: ids.pupil, ignored});
    await journal.waitForFunction(() => document.getElementById('siteBusy').hidden);
    check(true, 'Journal ignore action completes and hides loading');
  }
  await verifyLists(1);
  await toggle(true);
  await verifyLists(0);
  await work.screenshot({path: '/artifacts/ignored-task-list.png', fullPage: false});
  await toggle(false);
  await verifyLists(1);
  check(errors.length === 0, `No browser errors: ${errors.join('; ')}`);
  console.log(`Completed ${checks} ignored-roster browser checks.`);
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exit(1); });
