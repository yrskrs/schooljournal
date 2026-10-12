const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:19185';
let browser;
let count = 0;
function check(value, message) { assert.ok(value, message); count++; console.log('PASS:', message); }

(async () => {
  browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  async function login(username) {
    await page.goto(base + '/accounts/login/');
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill('roster-only-tests');
    await page.locator('form:has(#id_password) button[type=submit]').click();
    await page.goto(base + '/roster-sync/');
  }
  await login('roster_teacher');
  check(await page.locator('#rosterClass option').count() === 2, 'Classes are populated without server peer configuration');
  check(!(await page.locator('#rosterClass').innerText()).includes('7-В'), 'Another profile’s class is absent');
  check(await page.locator('.roster-advanced').getAttribute('open') === null, 'Advanced peer exchange is collapsed');
  const second = await page.locator('#rosterClass option').nth(1).getAttribute('value');
  await page.locator('#rosterClass').selectOption(second);
  await page.getByRole('button', { name: 'Показати журнали' }).click();
  check(await page.locator('#rosterClass').inputValue() === second, 'Class selection remains selected after opening its journals');
  check(await page.locator('.roster-journal h3').innerText() === 'Синтетична математика', 'Selected class shows its subject journal');
  check((await page.locator('.roster-journal').innerText()).includes('1 учн.'), 'Journal shows its ignore count');
  check(await page.getByRole('heading', { name: '3. Отримуйте оцінки в журналі' }).isVisible(), 'Roster transfer and grade import have clear separate instructions');
  await page.screenshot({ path: '/artifacts/roster-desktop.png', fullPage: true });
  await page.getByRole('link', { name: 'Підключити тести / завдання', exact: true }).click();
  check(await page.getByRole('heading', { name: 'Підключити тести та завдання', exact: true }).isVisible(), 'Class action opens the existing connection flow');
  await page.goto(base + '/roster-sync/');
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Roster page fits a mobile screen');
  await page.screenshot({ path: '/artifacts/roster-mobile.png', fullPage: true });
  await page.goto(base + '/accounts/logout/');
  await login('roster_personal');
  check(await page.locator('#rosterClass option').count() === 1, 'Personal nz.ua profile sees its class without a local school');
  check((await page.locator('#rosterClass').innerText()).includes('7-В'), 'Personal class is selectable');
  check(errors.length === 0, 'No uncaught JavaScript errors');
  console.log(`PASS: ${count} roster browser checks`);
  await browser.close();
})().catch(async error => { console.error(error); await browser?.close(); process.exitCode = 1; });
