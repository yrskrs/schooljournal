// Run inside the documented Playwright Docker container; all data is synthetic.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:19185';
const journalId = process.env.UX_JOURNAL_ID || '2';
let checks = 0;
let browser;
function check(value, message) { assert.ok(value, message); checks++; console.log('PASS:', message); }

(async () => {
  browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(base + '/accounts/login/');
  await page.locator('[name=username]').fill('synthetic_teacher');
  await page.locator('#id_password').fill('synthetic-test');
  await page.locator('form:has(#id_password) button[type=submit]').click();
  await page.goto(`${base}/journal/${journalId}/`);
  check(await page.locator('#journalLiveContent').count() === 1, 'Journal loads through Nginx with local scripts');
  await page.locator('#studentSearchInput').fill('Учень24');
  await page.locator('.journal-scroll-wrapper').evaluate(element => { element.scrollLeft = 700; });
  const row = page.locator('[id^=lesson-row-]').nth(20);
  const lessonId = (await row.getAttribute('id')).replace('lesson-row-', '');
  await row.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => ({ y: scrollY, x: document.querySelector('.journal-scroll-wrapper').scrollLeft, row: document.querySelectorAll('[id^=lesson-row-]')[20].getBoundingClientRect().top }));
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
  await row.locator('[title="Редагувати тему та ДЗ"]').click();
  await page.locator('#edit_lesson_topic').fill('Синтетична відредагована тема');
  await page.locator('#editLessonForm button[type=submit]').click();
  await page.waitForFunction(id => document.getElementById(`lesson-row-${id}`)?.textContent.includes('Синтетична відредагована тема'), lessonId);
  check(navigations === 0, 'Saving a lesson updates the journal without a document navigation');
  check(await page.locator('#studentSearchInput').inputValue() === 'Учень24', 'Search survives saving');
  check(await page.locator('.journal-scroll-wrapper').evaluate(element => element.scrollLeft) === before.x, 'Horizontal table position survives saving');
  check(Math.abs(await page.locator(`#lesson-row-${lessonId}`).evaluate(element => element.getBoundingClientRect().top) - before.row) < 10, 'Calendar position survives saving');
  check(await page.locator('#siteBusy').isHidden(), 'Saving finishes the loading animation');
  await page.locator(`#lesson-row-${lessonId} [title="Видалити урок"]`).click();
  await page.waitForFunction(id => !document.getElementById(`lesson-row-${id}`), lessonId);
  check(navigations === 0, 'Deleting a synthetic lesson updates the journal in place');
  check(await page.locator('.journal-scroll-wrapper').evaluate(element => element.scrollLeft) === before.x, 'Deleting preserves horizontal scroll');
  await page.evaluate(() => window.scrollTo(0, 700));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
  const reloadPosition = await page.evaluate(() => capturePagePosition());
  await page.reload();
  await page.waitForFunction(position => {
    const anchor = position.anchor && document.getElementById(position.anchor);
    return anchor ? Math.abs(anchor.getBoundingClientRect().top - position.offset) < 10
                  : Math.abs(scrollY - position.y) < 10;
  }, reloadPosition);
  check(true, 'Manual reload restores the visible calendar row even when table height changes');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.locator('.navbar').evaluate(element => element.getBoundingClientRect().height) < 90, 'Mobile navigation stays compact');
  check(await page.locator('#siteNavigation').isHidden(), 'Mobile navigation is collapsed');
  await page.locator('.nav-toggle').click();
  check(await page.locator('#siteNavigation').isVisible(), 'Mobile menu opens');
  await page.locator('.nav-toggle').click();
  await page.screenshot({ path: '/artifacts/mobile.png', fullPage: false });
  console.log('Mobile dimensions:', await page.locator('.journal-header').evaluate(element => ({ height: element.getBoundingClientRect().height, width: document.documentElement.scrollWidth })));
  check(await page.locator('.journal-header').evaluate(element => element.getBoundingClientRect().height) < 300, 'Mobile journal header stays compact');
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile page has no horizontal overflow outside its tables');
  await page.screenshot({ path: '/artifacts/mobile.png', fullPage: false });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  check(await page.locator('.nav-toggle').isHidden(), 'Desktop navigation has no redundant menu button');
  check(await page.locator('.navbar').evaluate(element => element.getBoundingClientRect().height) < 100, 'Desktop navigation stays compact');
  await page.screenshot({ path: '/artifacts/desktop.png', fullPage: false });

  // A slow preview can be dismissed; its eventual response must not reopen it.
  await page.route(`**/journal/${journalId}/nz/preview/`, async route => {
    await new Promise(resolve => setTimeout(resolve, 400));
    try { await route.continue(); } catch (_) {}
  });
  await page.locator('#btnNzPush').click();
  check(await page.locator('#siteBusy').isVisible(), 'Preview shows loading status');
  await page.evaluate(() => closeNzSyncModal());
  await page.waitForTimeout(600);
  check(await page.locator('#siteBusy').isHidden(), 'Cancelled preview stops loading');
  check(await page.locator('#nzSyncModalContainer').innerHTML() === '', 'Cancelled preview stays closed');
  await page.unroute(`**/journal/${journalId}/nz/preview/`);

  await page.locator('#btnNzPush').click();
  await page.locator('#nzSyncSubmitBtn').waitFor();
  const dry = page.locator('#nzDryRunCheckbox');
  if (await dry.isChecked()) await dry.uncheck();
  const badgeBefore = await page.locator('#btnNzPush .badge').innerText();
  let refreshes = 0;
  // NZ itself is not contacted: the server contract is covered by Django tests.
  await page.route(`**/journal/${journalId}/nz/sync/`, route => route.fulfill({ json: {
    status: 'success', error_count: 0, success_count: 1, items_count: 1,
    message: 'Синтетичне вивантаження завершено', dry_run: false, details: [],
  } }));
  await page.route(`**/journal/${journalId}/`, async route => {
    refreshes++;
    const response = await route.fetch();
    let html = await response.text();
    // Simulate a freshly confirmed server state returned after the tested POST.
    html = html.replace(/(<span class="badge"[^>]*title="[^"]*">)\s*\d+\s*(<\/span>)/, '$1✓$2');
    await route.fulfill({ response, body: html });
  });
  const navigationBeforePush = navigations;
  await page.locator('#nzSyncSubmitBtn').click();
  await page.waitForFunction(() => document.getElementById('btnNzPush')?.querySelector('.badge')?.textContent.trim() === '✓');
  check(refreshes === 1, 'Successful upload automatically fetches fresh journal state once');
  check(navigations === navigationBeforePush, 'Upload does not reload the document');
  check(badgeBefore !== '✓', 'Upload replaces the stale unsynced badge');
  check(await page.locator('#siteBusy').isHidden(), 'Upload and refresh finish the loading animation');
  await page.evaluate(() => closeNzSyncModal());
  await page.unroute(`**/journal/${journalId}/`);
  await page.unroute(`**/journal/${journalId}/nz/sync/`);

  await page.route(`**/journal/${journalId}/nz/import/`, route => route.abort('failed'));
  await page.locator('#btnNzPull').click();
  await page.waitForFunction(() => !document.getElementById('btnNzPull').disabled && document.getElementById('siteBusy').hidden);
  check(true, 'Network failure restores the import button and hides loading');
  await page.unroute(`**/journal/${journalId}/nz/import/`);
  const pupilId = await page.locator('.student-row').first().getAttribute('data-student-id');
  await page.locator(`.student-row[data-student-id="${pupilId}"] .student-number-btn`).click();
  await page.locator(`#studentMenu_${pupilId} [role=menuitemcheckbox]`).click();
  await page.waitForFunction(id => document.querySelector(`.student-row[data-student-id="${id}"]`)?.classList.contains('row-student-ignored'), pupilId);
  check(await page.locator(`#studentMenu_${pupilId}`).count() === 1, 'Refreshing an ignored pupil removes the old floating menu');
  await page.locator(`.student-row[data-student-id="${pupilId}"] .student-number-btn`).click();
  check(await page.locator(`#studentMenu_${pupilId} [role=menuitemcheckbox]`).getAttribute('aria-checked') === 'true', 'Fresh pupil menu shows the current ignore state');
  await page.locator(`#studentMenu_${pupilId} [role=menuitemcheckbox]`).click();
  await page.waitForFunction(id => !document.querySelector(`.student-row[data-student-id="${id}"]`)?.classList.contains('row-student-ignored'), pupilId);
  check(await page.locator(`#studentMenu_${pupilId}`).count() === 1, 'Restoring the pupil keeps one current menu');
  const cell = page.locator(`.student-row[data-student-id="${pupilId}"] .journal-cell-input`).first();
  await cell.fill('9');
  await cell.dispatchEvent('change');
  await page.waitForFunction(id => document.querySelector(`.student-row[data-student-id="${id}"] .journal-cell-input`)?.value === '9' && document.querySelector(`.student-row[data-student-id="${id}"] .journal-single-cell-wrap`)?.classList.contains('sync-local'), pupilId);
  check(true, 'HTMX saves grades after multiple journal refreshes');
  await page.evaluate(() => refreshJournal());
  await page.locator(`.student-row[data-student-id="${pupilId}"] .journal-cell-input`).first().click();
  const saved = page.waitForResponse(response => response.url().includes('/grade/update/') && response.request().method() === 'POST');
  await page.locator('#journalQuickPicker .qp-btn[data-value="10"]').click();
  check((await saved).ok(), 'Quick picker sends a successful authenticated grade POST');
  await page.waitForFunction(id => document.querySelector(`.student-row[data-student-id="${id}"] .journal-cell-input`)?.value === '10', pupilId);
  check(true, 'Quick grade picker remains usable after replacing the journal');
  check(errors.length === 0, 'No uncaught JavaScript errors: ' + errors.join('; '));
  console.log(`PASS: ${checks} browser checks`);
  await browser.close();
})().catch(async error => { console.error(error); await browser?.close(); process.exitCode = 1; });
