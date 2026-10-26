'use strict';
// Actual public-IVS result-card interactions on a synthetic loopback server.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const [base, output, moduleFile] = process.argv.slice(2);
const target = new URL(base);
assert.equal(target.hostname, '127.0.0.1');
assert.notEqual(target.port, '8000');
const {chromium} = createRequire(path.resolve(moduleFile))('playwright');
const errors = [], external = [], checks = [];
fs.mkdirSync(output, {recursive: true});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function completed(page, taskId, revision) {
  for (let i = 0; i < 60; i++) {
    const response = await page.request.get(`${base}/api/research/tasks/${taskId}`);
    assert.equal(response.status(), 200);
    const task = await response.json();
    if (task.revision === revision && task.status !== 'RUNNING') {
      assert.equal(task.status, 'COMPLETE', JSON.stringify(task.error));
      return task;
    }
    await pause(500);
  }
  throw Error('Scripted task completion deadline');
}
(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.MIA_BROWSER_CHANNEL === 'chromium' ? {} : {channel: process.env.MIA_BROWSER_CHANNEL || 'chrome'})});
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({viewport: {width, height: width === 390 ? 844 : 900}});
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
      await page.route('**/*', route => {
        if (new URL(route.request().url()).origin !== target.origin) {
          external.push(route.request().url()); return route.abort();
        }
        return route.continue();
      });
      await page.goto(base);
      await page.locator('#public-ivs-banner').waitFor();
      const banner = await page.locator('#public-ivs-banner').innerText();
      for (const token of ['REAL_PUBLIC_SEC_METADATA', 'SYNTHETIC', 'SCRIPTED']) assert.ok(banner.includes(token));
      await page.getByRole('button', {name: '研究这份披露', exact: true}).first().waitFor();
      await page.screenshot({path: path.join(output, `public-ivs-${width}.png`)});
      await page.getByRole('button', {name: '研究这份披露', exact: true}).first().click();
      await page.getByRole('button', {name: 'Fast · 快速', exact: true}).click();
      await page.getByRole('button', {name: '开始研究', exact: true}).click();
      const dialog = page.getByRole('dialog');
      assert.ok((await dialog.innerText()).includes('无费用'));
      await dialog.getByRole('button', {name: '确认开始研究', exact: true}).click();
      await page.waitForURL(url => url.searchParams.has('research_task'));
      const taskId = new URL(page.url()).searchParams.get('research_task');
      const fast = await completed(page, taskId, 1);
      assert.equal(fast.mode, 'FAST');
      assert.ok(fast.filing_evidence.source_record_id);
      assert.ok(fast.assessment.key_reasons.some(reason => reason.evidence_ids.length));
      const original = await (await page.request.get(`${base}/api/research/tasks/${taskId}/revisions/1`)).text();
      await page.getByRole('button', {name: '继续为 Deep · 保留同一条记录', exact: true}).click();
      await dialog.getByRole('button', {name: '确认继续研究', exact: true}).click();
      const deep = await completed(page, taskId, 2);
      // The versioned serializer omits the historical DEEP default.
      assert.equal(deep.mode ?? 'DEEP', 'DEEP');
      assert.deepEqual(deep.filing_evidence, fast.filing_evidence);
      await page.getByRole('textbox', {name: '增减股数（正数）', exact: true}).fill('5');
      await page.getByRole('textbox', {name: 'UNP 假设成交价 · USD/股', exact: true}).fill('240');
      await page.getByText('费用、其他持仓计价与资本假设', {exact: true}).click();
      await page.getByRole('textbox', {name: '交易费用 · USD', exact: true}).fill('1');
      await page.getByRole('textbox', {name: 'KR · 10 股 · 假设计价 USD/股', exact: true}).fill('58');
      await page.getByRole('textbox', {name: '假设情景日期', exact: true}).fill('2026-10-24');
      const attest = page.getByRole('checkbox', {name: /确认采用自定假设价格/});
      await attest.check();
      const receiptResponse = page.waitForResponse(response => response.url().includes(`/api/research/tasks/${taskId}/what-if`)
        && response.request().method() === 'POST');
      await page.getByRole('button', {name: '预览组合变化', exact: true}).click();
      const response = await receiptResponse;
      assert.equal(response.status(), 200);
      const receipt = await response.json();
      assert.equal(receipt.before.nav, '12980'); assert.equal(receipt.after.nav, '12979');
      assert.equal(receipt.after.cash, '8799');
      await page.getByText('12,980 → 12,979', {exact: true}).waitFor();
      assert.equal(await (await page.request.get(`${base}/api/research/tasks/${taskId}/revisions/1`)).text(), original);
      await page.screenshot({path: path.join(output, `public-ivs-whatif-${width}.png`)});
      await page.reload();
      await page.getByRole('button', {name: '历史版本', exact: true}).waitFor();
      const saved = await (await page.request.get(`${base}/api/research/tasks/${taskId}`)).json();
      assert.equal(saved.revision, 2); assert.equal(saved.status, 'COMPLETE');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.ok(!(await page.locator('body').innerText()).includes('原研究方向 null'));
      const observation = await (await page.request.get(`${base}/api/research/tasks/${taskId}/observation`)).json();
      assert.ok(JSON.stringify(observation).includes(taskId));
      checks.push({width, taskId, revisions: [1, 2], original_preserved: true,
        actual_result_card_whatif: true, nav: ['12980', '12979'], reload_preserved: true});
      await context.close();
    }
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output, 'public-ivs-browser.json'), JSON.stringify({status: 'PASS', checks, errors, external}, null, 2));
  console.log(JSON.stringify({status: 'PASS', widths: [1440, 390], actual_result_card_whatif: true}));
})().catch(error => {
  fs.writeFileSync(path.join(output, 'public-ivs-browser.json'), JSON.stringify({status: 'FAIL', failure: error.stack, checks, errors, external}, null, 2));
  console.error(error.stack); process.exitCode = 1;
});
