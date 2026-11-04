'use strict';
// Real Console interactions at desktop/mobile, using an explicitly supplied Playwright installation.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const [base, output, moduleFile] = process.argv.slice(2);
const target = new URL(base);
assert.equal(target.hostname, '127.0.0.1'); assert.notEqual(target.port, '8000');
const {chromium} = createRequire(path.resolve(moduleFile))('playwright');
fs.mkdirSync(output, {recursive: true});
const errors = [], external = [], runs = [];
async function saved(page, tid, revision) {
  for (let i = 0; i < 60; i++) {
    const r = await page.request.get(`${base}/api/research/tasks/${tid}`);
    assert.equal(r.status(), 200);
    const task = await r.json();
    if (task.revision === revision && task.status !== 'RUNNING') {
      assert.equal(task.status, 'COMPLETE', JSON.stringify(task.error)); return task;
    }
    await page.waitForTimeout(100);
  }
  throw Error('SCRIPTED_RESEARCH_DEADLINE');
}
(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.MIA_BROWSER_CHANNEL || 'chrome'});
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({colorScheme: 'dark', viewport: {width, height: width === 390 ? 844 : 1000}});
      const page = await context.newPage();
      let expectedFault = false, faultCount = 0, researchPosts = 0;
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', m => { if (m.type() === 'error') {
        if (expectedFault && m.text().includes('503')) faultCount++; else errors.push(m.text());
      }});
      page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/api/portfolio-priority/research')) researchPosts++; });
      await page.route('**/*', route => {
        if (new URL(route.request().url()).origin !== target.origin) { external.push(route.request().url()); return route.abort(); }
        return route.continue();
      });
      await page.goto(base);
      if (process.env.MIA_PUBLIC_DEFAULT_ENTRY === '1') {
        await page.getByRole('link', {name: '组合研究优先级', exact: true}).click();
      }
      await page.getByRole('button', {name: '选择 APP', exact: true}).waitFor();
      assert.ok((await page.locator('#portfolio-priority').innerText()).includes('当前报价 0/6'));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.getByText('查看四账户、现金、全部持仓与 as-of', {exact: true}).click();
      assert.equal(await page.locator('.pp-account').count(), 4);
      assert.ok((await page.locator('#pp-ledger').innerText()).includes('AAPL · 8 股'));
      await page.getByRole('button', {name: '选择 AAPL', exact: true}).click();
      assert.equal(await page.getByRole('button', {name: '开始 Fast 研究', exact: true}).isEnabled(), false);
      await page.getByRole('button', {name: '选择 APP', exact: true}).click();
      assert.equal(await page.getByRole('button', {name: '开始 Fast 研究', exact: true}).isEnabled(), false);
      await page.getByRole('combobox', {name: '选择研究账户', exact: true}).selectOption('SYN-GROWTH');
      await page.screenshot({path: path.join(output, `priority-${width}.png`), fullPage: true});
      await page.getByRole('button', {name: '开始 Fast 研究', exact: true}).click();
      await page.waitForURL(url => url.searchParams.has('research_task'));
      const tid = new URL(page.url()).searchParams.get('research_task');
      const fast = await saved(page, tid, 1);
      await page.getByRole('heading', {name: 'APP · 投资逻辑复核', exact: true}).waitFor();
      const frozen = await (await page.request.get(`${base}/api/research/tasks/${tid}/revisions/1`)).text();
      await page.getByRole('button', {name: '继续同一 Task 的 Deep', exact: true}).click();
      const deep = await saved(page, tid, 2);
      await page.getByText('Deep 补入可比期间公开财务事实；UNDETERMINED。', {exact: true}).waitFor();
      await page.waitForFunction(() => document.getElementById('desk-task-meta').textContent.includes('第 2 轮'));
      assert.ok(deep.assessment.support.length && deep.assessment.counterevidence.length);
      const caseFile = page.getByRole('region', {name: '版本与执行收据'});
      await page.waitForFunction(() => document.activeElement?.classList.contains('pp-receipt'));
      assert.ok(await caseFile.evaluate(section => {
        const heading = section.querySelector('h2').getBoundingClientRect();
        return heading.top >= 0 && heading.bottom < innerHeight;
      }), 'Completed case heading must be visible without scrolling');
      const originalCard = caseFile.locator('[data-revision="1"]');
      const selectedCard = caseFile.locator('[data-revision="2"]');
      assert.ok((await originalCard.innerText()).includes(fast.assessment.core_judgment.text));
      assert.ok((await selectedCard.innerText()).includes(deep.assessment.counterevidence[0].text));
      assert.ok((await selectedCard.innerText()).includes(deep.assessment.next_checks[0].text));
      assert.equal(await originalCard.locator('.pp-case-decision').innerText(), 'UNDETERMINED');
      assert.equal(await selectedCard.locator('.pp-case-decision').innerText(), 'UNDETERMINED');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const contrastByTheme = {};
      for (const theme of ['auto', 'light']) {
        await page.getByRole('combobox', {name: '外观主题', exact: true}).selectOption(theme);
        contrastByTheme[theme] = await caseFile.evaluate(section => {
          const luminance = color => {
            const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => {
              const channel = value / 255;
              return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
            });
            return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
          };
          return [...section.querySelectorAll('.pp-case-version h3, .pp-case-actions a')].map(element => {
            let parent = element;
            while (getComputedStyle(parent).backgroundColor === 'rgba(0, 0, 0, 0)') parent = parent.parentElement;
            const foreground = luminance(getComputedStyle(element).color);
            const background = luminance(getComputedStyle(parent).backgroundColor);
            return {text: element.textContent, ratio: (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)};
          });
        });
        assert.equal(contrastByTheme[theme].length, 4);
        assert.ok(contrastByTheme[theme].every(item => item.ratio >= 4.5), JSON.stringify(contrastByTheme));
      }
      await caseFile.screenshot({path: path.join(output, `case-file-${width}.png`)});
      const evidenceJump = caseFile.getByRole('link', {name: '前往本版依据'});
      await evidenceJump.focus();
      await evidenceJump.press('Enter');
      await page.waitForURL(url => url.hash === '#desk-evidence-open');
      await page.locator('#desk-evidence-open').click();
      assert.match(await page.locator('#desk-evidence-scope').innerText(), /第 2 轮/);
      await page.keyboard.press('Escape');
      await caseFile.getByText('查看新增证据、财务期间与来源', {exact: true}).click();
      assert.match(await caseFile.locator('.pp-case-evidence').innerText(), /0001751008-26-000059/);
      await page.getByText('查看实际工具选择与执行 Trace', {exact: true}).click();
      await page.locator('.pp-receipt a[href^="/api/execution-traces/"]').first().waitFor();
      assert.equal(await caseFile.locator('li').filter({hasText: /^版本 1 ·/}).count(), 0);
      assert.ok((await page.locator('.pp-receipt').innerText()).includes('sec.fundamentals'));
      // The existing, shipped single-account What-if form remains the calculator.
      await caseFile.getByRole('link', {name: '前往单账户 What-if'}).click();
      await page.waitForURL(url => url.hash === '#desk-case-whatif');
      await page.getByRole('textbox', {name: '增减股数（正数）', exact: true}).fill('5');
      await page.getByRole('textbox', {name: 'APP 假设成交价 · USD/股', exact: true}).fill('200');
      await page.getByText('费用、其他持仓计价与资本假设', {exact: true}).click();
      await page.getByRole('textbox', {name: '交易费用 · USD', exact: true}).fill('1');
      await page.getByRole('textbox', {name: 'UNP · 5 股 · 假设计价 USD/股', exact: true}).fill('240');
      await page.getByRole('textbox', {name: '假设情景日期', exact: true}).fill('2026-10-30');
      await page.getByRole('checkbox', {name: /确认采用自定假设价格/}).check();
      let receiptResponse = page.waitForResponse(r => r.url().endsWith(`/api/research/tasks/${tid}/what-if`) && r.request().method() === 'POST');
      await page.getByRole('button', {name: '预览组合变化', exact: true}).click();
      let receipt = await (await receiptResponse).json();
      assert.equal(receipt.after.cash, '199'); assert.equal(receipt.after.shares, '35'); assert.equal(receipt.after.nav, '8399');
      assert.equal(await (await page.request.get(`${base}/api/research/tasks/${tid}/revisions/1`)).text(), frozen);
      await page.screenshot({path: path.join(output, `whatif-${width}.png`), fullPage: true});
      await page.getByRole('textbox', {name: '增减股数（正数）', exact: true}).fill('7');
      receiptResponse = page.waitForResponse(r => r.url().endsWith(`/api/research/tasks/${tid}/what-if`) && r.request().method() === 'POST');
      // This is an intentional typed refusal, not a product console fault.
      const beforeErrors = errors.length;
      await page.getByRole('button', {name: '预览组合变化', exact: true}).click();
      assert.equal((await receiptResponse).status(), 422);
      await page.waitForTimeout(100);
      const expectedRefusalErrors = errors.splice(beforeErrors);
      assert.ok(expectedRefusalErrors.every(e => e.includes('422')));
      await page.reload();
      await page.getByRole('heading', {name: 'APP · 投资逻辑复核', exact: true}).waitFor();
      assert.equal((await saved(page, tid, 2)).revision, 2); assert.equal(researchPosts, 2);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      // Open the saved Fast version through the existing history UI. The extension
      // must bind to that version too, and never continue a historical view.
      await page.getByRole('button', {name: '历史版本', exact: true}).click();
      const historicalWhatIf = page.waitForResponse(r => r.url().endsWith(`/api/research/tasks/${tid}/what-if?revision=1`) && r.request().method() === 'GET' && r.ok());
      await page.locator('#desk-history-select').selectOption('1');
      await page.waitForURL(url => url.searchParams.get('revision') === '1');
      await page.waitForFunction(() => document.querySelector('#portfolio-priority [role="status"]').textContent.includes('历史版本只读'));
      await historicalWhatIf;
      await originalCard.waitFor();
      assert.equal(await selectedCard.count(), 0);
      await caseFile.getByText('查看实际工具选择与执行 Trace', {exact: true}).click();
      await caseFile.locator('li').filter({hasText: /^版本 1 ·/}).first().waitFor();
      assert.equal(await caseFile.locator('li').filter({hasText: /^版本 2 ·/}).count(), 0);
      assert.equal(await page.getByRole('button', {name: '继续同一 Task 的 Deep', exact: true}).isVisible(), false);
      await page.getByRole('button', {name: '最新一轮', exact: true}).click();
      await page.waitForURL(url => !url.searchParams.has('revision'));
      // A second Task makes a stale same-task continuation observable: history
      // opens APP after UNP, so the top panel must not keep UNP's task id.
      await page.getByRole('button', {name: '返回工作台', exact: true}).click();
      await page.getByRole('button', {name: '选择 UNP', exact: true}).click();
      await page.getByRole('combobox', {name: '选择研究账户', exact: true}).selectOption('SYN-GROWTH');
      await page.getByRole('button', {name: '开始 Fast 研究', exact: true}).click();
      await page.waitForURL(url => url.searchParams.has('research_task') && url.searchParams.get('research_task') !== tid);
      const otherTid = new URL(page.url()).searchParams.get('research_task');
      await saved(page, otherTid, 1);
      await page.getByRole('heading', {name: 'UNP · 投资逻辑复核', exact: true}).waitFor();
      await caseFile.getByRole('heading', {name: 'UNP · 投资案卷', exact: true}).waitFor();
      assert.equal(await caseFile.locator('[data-revision]').count(), 1);
      await page.getByRole('button', {name: '返回工作台', exact: true}).click();
      await page.locator('#desk-recent button').filter({hasText: 'APP'}).first().click();
      await page.waitForURL(url => url.searchParams.get('research_task') === tid);
      await page.waitForFunction(() => document.querySelector('#portfolio-priority [role="status"]').textContent.startsWith('APP ·'));
      assert.equal(await page.getByRole('button', {name: '继续同一 Task 的 Deep', exact: true}).isVisible(), false);
      assert.equal(researchPosts, 3);
      // Fail one overview GET, then recover from the visible read-only retry.
      await page.route('**/api/portfolio-priority', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"SCRIPTED_READ_FAILURE"}'}));
      expectedFault = true;
      await page.goto(base);
      await page.getByRole('button', {name: '重新读取旅程', exact: true}).waitFor();
      await page.unroute('**/api/portfolio-priority');
      await page.getByRole('button', {name: '重新读取旅程', exact: true}).click();
      await page.getByRole('button', {name: '选择 APP', exact: true}).waitFor();
      expectedFault = false; assert.equal(researchPosts, 3);
      await page.getByRole('combobox', {name: '外观主题', exact: true}).selectOption('dark');
      await page.screenshot({path: path.join(output, `dark-${width}.png`), fullPage: true});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      runs.push({width, task_id: tid, revisions: [fast.revision, deep.revision],
        research_posts: researchPosts, original_preserved: true, whatif_cash: '199',
        history_task_switch_bound: true, historical_version_read_only: true, case_comparison: true, completed_case_focused: true,
        revision_bound_evidence_whatif_trace: true, contrast_by_theme: contrastByTheme,
        insufficient_cash_refused: true, get_failure_recovered: true, expected_503_console_count: faultCount,
        expected_422_console_count: expectedRefusalErrors.length, overflow: false});
      await context.close();
    }
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    fs.writeFileSync(path.join(output, 'BROWSER_RESULT.json'), JSON.stringify({verdict: 'PASS', runs, errors, external}, null, 2));
    console.log(JSON.stringify({verdict: 'PASS', widths: runs.map(r => r.width), errors, external}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
