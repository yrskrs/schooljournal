// Chromium checks on the disposable outside_grid.compose.yaml environment only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://nginx';
const artifacts = process.env.AUDIT_ARTIFACTS || '/artifacts';

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const errors = [];
  const checks = [];
  fs.mkdirSync(artifacts, {recursive: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/accounts/login/');
    await page.locator('#id_username').fill('outside_audit');
    await page.locator('#id_password').fill('synthetic-outside-password');
    await page.locator('form').filter({has: page.locator('#id_username')}).locator('button[type=submit]').click();
    await page.waitForURL(url => !url.pathname.includes('/login'));
    await page.goto(base + '/outside-students/?journal=1');
    assert.equal((await page.request.get(base+'/static/css/styles.css')).status(), 200);
    const table = page.locator('.outside-grades-table');
    await table.waitFor({state: 'visible'});
    assert.equal(await table.locator('tbody tr').count(), 2);
    assert.equal(await table.locator('.outside-grade-mark').count(), 6);
    assert((await table.innerText()).includes('Тема 8 вересня'));
    assert((await table.innerText()).includes('Тема жовтня'));
    assert((await table.innerText()).includes('Збережений Учень'));
    await page.screenshot({path: artifacts + '/outside_grid_desktop.png', fullPage: true});
    checks.push('Saved pupils and all six grades are visible despite disabled canonical connections; real lesson topics form the columns');
    await page.setViewportSize({width: 390, height: 844});
    const layout = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth,
      wide: [...document.querySelectorAll('body *')].map(el => ({tag: el.tagName, cls: el.className,
        right: el.getBoundingClientRect().right, width: el.getBoundingClientRect().width,
        overflow: getComputedStyle(el).overflowX})).filter(el => el.right > innerWidth + 1).slice(0, 20)}));
    await page.screenshot({path: artifacts + '/outside_grid_mobile.png', fullPage: true});
    assert(layout.scroll <= layout.width + 1, JSON.stringify(layout));
    const scroll = page.locator('.outside-table-scroll');
    assert(await scroll.evaluate(el => el.scrollWidth > el.clientWidth));
    await scroll.evaluate(el => {el.scrollLeft = 250;});
    const nameColumn = await table.locator('tbody .sticky-col-name').first().boundingBox();
    const scrollBox = await scroll.boundingBox();
    assert(nameColumn.x >= scrollBox.x && nameColumn.x < scrollBox.x + 70);
    await scroll.evaluate(el => {el.scrollLeft = 0;});
    await page.screenshot({path: artifacts + '/outside_grid_mobile.png', fullPage: true});
    checks.push('Mobile grid scrolls inside its container; pupil names remain fixed and the page does not overflow');
    await page.locator('[name=q]').fill('Збережений');
    await page.getByRole('button', {name: 'Показати', exact: true}).click();
    assert.equal(await table.locator('tbody tr').count(), 1);
    await page.locator('[name=month]').fill('2026-10');
    await page.getByRole('button', {name: 'Показати', exact: true}).click();
    assert.equal(await table.locator('.outside-grade-mark').count(), 0);
    assert((await table.innerText()).includes('Тема жовтня'));
    await page.getByRole('link', {name: 'Скинути пошук і період', exact: true}).click();
    assert.equal(await table.locator('.outside-grade-mark').count(), 6);
    checks.push('Search and month filters preserve the journal; resetting restores all saved results');
    await table.locator('.outside-grade-value').first().click();
    await page.waitForURL('**/api-inbox/?student=*&all=1');
    assert((await page.locator('body').innerText()).includes('Збережений Учень'));
    await page.goto(base + '/outside-students/?journal=1');
    await page.getByRole('link', {name: '← До основного журналу', exact: true}).click();
    await page.waitForURL('**/journal/1/');
    await page.locator('#journalTools summary').click();
    await page.getByRole('link', {name: 'Таблиця окремих учнів', exact: true}).click();
    await page.waitForURL('**/outside-students/?journal=1');
    checks.push('A grade opens the canonical pupil works; the main journal links directly to the outside grid');
    assert.deepEqual(errors, []);
    fs.writeFileSync(artifacts+'/browser_checks.json', JSON.stringify({passed: checks, javascript_errors: errors}, null, 2));
    console.log(JSON.stringify({passed: checks.length, checks, javascript_errors: errors}, null, 2));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
