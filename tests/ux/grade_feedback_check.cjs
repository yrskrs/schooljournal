const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  const fixture = JSON.parse(fs.readFileSync(process.env.GRADE_FEEDBACK_FIXTURE || '/artifacts/grade-feedback-browser-fixture.json'));
  let checks = 0;
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    await page.context().addCookies(fixture.cookies);
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
    await page.goto(fixture.url);
    await page.waitForSelector('#formPullTestGrades');
    page.on('dialog', dialog => dialog.accept());
    for (const formId of ['formSendTestRoster','formPullTestGrades','formSendWorkRoster','formPullWorkGrades']) {
      const form = page.locator('#'+formId);
      const operation = await form.getAttribute('data-sync-operation');
      const action = await form.getAttribute('action');
      const button = form.locator('button[type=submit]');
      assert.equal(await button.evaluate(el=>el.form.id),formId);
      const wait = page.waitForResponse(res=>res.request().method()==='POST'&&new URL(res.url()).pathname===action);
      await button.click();
      const response = await wait;
      assert.equal(response.status(),200);
      const body = await response.json();
      assert.equal(body.operation,operation);
      assert.equal(body.success,true);
      assert.equal(body.level,'warning');
      assert.equal(operation==='grades',body.message.startsWith('📥 Оцінки'));
      await page.waitForFunction(message=>document.querySelector('#toast-container')?.lastElementChild?.textContent.includes(message),body.message);
      await page.waitForFunction(id=>!document.querySelector('#'+id)?.dataset.siteSubmitting,formId);
      assert.equal(page.url(),fixture.url);
      checks++;
    }
    await page.screenshot({path:'/artifacts/grade-feedback-'+viewport.width+'.png',fullPage:true});
    await page.close();
  }
  await browser.close();
  console.log('PASS: '+checks+' actual button submissions at desktop/mobile widths; correct routes, JSON operations, warnings and fresh toasts');
})().catch(error=>{console.error(error);process.exitCode=1});
