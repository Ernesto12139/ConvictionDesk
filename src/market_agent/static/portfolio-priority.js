/* A Console extension. Research reading, history, citations and What-if reuse research-desk.js. */
(() => {
  'use strict';
  const root = document.getElementById('portfolio-priority');
  if (!root) return;
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; };
  const button = (text, action, className) => { const b = el('button', text, className); b.type = 'button'; b.addEventListener('click', action); return b; };
  const dollar = value => value == null ? 'UNKNOWN' : '$' + value; // Python supplied exact decimal text; no JS money arithmetic.
  const stamp = value => value.replace('T', ' ').replace(/(?:\+00:00|Z)$/, ' UTC');
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : data.detail?.code || 'REQUEST_FAILED');
    return data;
  }
  let overview, selected, active, sending = false, readGeneration = 0;
  const status = el('p', '', 'pp-coverage'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const account = el('select'); account.id = 'pp-account'; account.setAttribute('aria-label', '选择研究账户');
  const chosen = el('p', '先从左侧选择一个持仓。');
  const start = button('开始 Fast 研究', () => submit('FAST'), 'primary'); start.disabled = true;
  const deep = button('继续同一 Task 的 Deep', () => submit('DEEP'), 'primary'); deep.hidden = true;
  const retry = button('重试失败研究', () => submit(active.mode || 'FAST')); retry.hidden = true;
  const read = button('读取保存状态', () => readTask()); read.hidden = true;
  let submittedCase = null;
  const receipt = el('section', undefined, 'pp-receipt'); receipt.hidden = true; receipt.setAttribute('aria-label', '版本与执行收据'); receipt.tabIndex = -1;
  window.addEventListener('mia-task-opened', event => {
    if (submittedCase?.taskId !== event.detail.taskId || submittedCase.revision !== event.detail.revision || receipt.hidden) return;
    receipt.focus({preventScroll: true});
    receipt.scrollIntoView({block: 'start', behavior: 'instant'});
    submittedCase = null;
  });
  account.addEventListener('change', () => { start.disabled = !account.value || sending; });

  function choose(candidate) {
    if (sending) return;
    ++readGeneration;
    receipt.hidden = true;
    selected = candidate; active = null;
    chosen.textContent = `${candidate.symbol} · 累计投入成本 ${dollar(candidate.committed_cost)}；覆盖 ${candidate.account_ids.length} 个合成账户。`;
    const placeholder = el('option', '明确选择一个账户'); placeholder.value = '';
    account.replaceChildren(placeholder);
    for (const id of candidate.account_ids) {
      const a = overview.accounts.find(item => item.account_id === id);
      const option = el('option', `${a.name} (${id}) · 现金 ${dollar(a.cash)}`); option.value = id; account.append(option);
    }
    start.disabled = true; deep.hidden = true; retry.hidden = true;
    status.textContent = candidate.sec_status === 'NOT_COVERED' ? '此持仓没有打包 SEC 来源；只保留排序，不能构造研究。' : '选定账户后开始。研究与 What-if 仅使用该账户；其他账户现金不能补足。';
    if (candidate.sec_status === 'NOT_COVERED') account.disabled = true; else account.disabled = false;
    document.getElementById('pp-selection').scrollIntoView({block: 'nearest'});
  }

  async function submit(mode) {
    if (sending) return;
    if (new URL(location.href).searchParams.has('revision')) {
      status.textContent = '历史版本只读。请先回到最新一轮，再继续同一 Task。'; return;
    }
    const continuing = active && (mode === 'DEEP' || active.status === 'FAILED');
    const payload = continuing ? {symbol: active.focus_symbol, account_id: active.account_id, mode,
      task_id: active.task_id, expected_revision: active.revision} : {symbol: selected?.symbol, account_id: account.value, mode};
    if (!payload.symbol || !payload.account_id) return;
    sending = true; start.disabled = true; deep.disabled = true; retry.disabled = true;
    status.textContent = '正在保存 SCRIPTED 研究任务；无模型调用、无费用。';
    try {
      active = await api('/api/portfolio-priority/research', payload);
      const url = new URL(location.href); url.searchParams.set('research_task', active.task_id); url.searchParams.delete('revision'); history.replaceState(null, '', url);
      window.dispatchEvent(new PopStateEvent('popstate'));
      await readTask();
      const saved = document.getElementById('desk-read-saved');
      if (saved && !document.getElementById('desk-task').hidden && active.status === 'COMPLETE') {
        submittedCase = {taskId: active.task_id, revision: active.revision};
        saved.click();
      }
    } catch (error) { status.textContent = `研究未提交：${error.message}。读取保存状态后再决定是否重试。`; }
    finally { sending = false; start.disabled = !account.value || account.disabled; deep.disabled = false; retry.disabled = false; }
  }

  function financialReceipts(delta) {
    const values = delta.numeric_receipts || [];
    if (!values.length) return null;
    const section = el('section', undefined, 'pp-numeric-receipts'); section.style.overflowWrap = 'anywhere';
    section.setAttribute('aria-label', '同期间财务数值收据');
    section.append(el('h3', '同期间财务 · 8 份可核对收据'));
    const rows = new Map(values.map(row => [row.field, row]));
    const metrics = [['revenue', '收入'], ['operating_cash_flow', '经营现金流']];
    const fields = metrics.flatMap(([metric]) => ['previous', 'current', 'change', 'yoy_pct'].map(field => `comparison.${metric}.${field}`));
    if (values.length !== 8 || rows.size !== 8 || fields.some(field => !rows.has(field))) {
      section.append(el('p', '本版本数值收据不完整，暂不展示比较；请核对下方原始证据。')); return section;
    }
    const table = el('table', undefined, 'pp-table pp-facts');
    const header = el('tr'); ['指标', '上年同期 · USD', '本期 · USD', '差额 · USD', '同比', '准确期间'].forEach(text => header.append(el('th', text))); table.append(header);
    for (const [metric, label] of metrics) {
      const get = field => rows.get(`comparison.${metric}.${field}`);
      const period = get('change').period;
      const row = el('tr');
      [label, get('previous').value, get('current').value, get('change').value, get('yoy_pct').value + '%',
        `${period.previous.start} → ${period.previous.end} / ${period.current.start} → ${period.current.end}`]
        .forEach(text => row.append(el('td', text)));
      table.append(row);
    }
    const wrap = el('div', undefined, 'pp-table-wrap'); wrap.append(table); section.append(wrap);
    section.append(el('p', '冻结 PUBLIC_SEC · Python Decimal。实体范围 ENTIRE_FILING_ENTITY；不证明持续经营业务可比，不是实时行情或模型新增结论。', 'small'));
    const details = el('details'); details.append(el('summary', '核对 8 份收据的 ID、单位、期间与来源'));
    const list = el('ul');
    for (const field of fields) {
      const row = rows.get(field);
      list.append(el('li', `${row.receipt_id} · ${row.field} · ${row.value} ${row.unit} · ${JSON.stringify(row.period)} · ${stamp(row.as_of)} · ${row.basis} · ${row.evidence_ref} / ${row.evidence_id}`));
    }
    details.append(list, el('p', 'Packet ' + delta.numeric_receipt_packet_hash, 'small'));
    for (const source of delta.numeric_receipt_sources || []) details.append(el('p', `${source.ref} / ${source.evidence_id} · ${source.source_hash}`, 'small'));
    section.append(details); return section;
  }

  async function readTask() {
    const generation = ++readGeneration;
    const params = new URL(location.href).searchParams;
    const id = params.get('research_task') || active?.task_id, revision = params.get('revision');
    if (!overview || !id) return;
    receipt.hidden = true;
    read.hidden = false;
    try {
      // GET-only recovery and a finite polling window. No implicit research retry.
      for (let attempt = 0; attempt < 60; attempt++) {
        const current = await api('/api/research/tasks/' + encodeURIComponent(id) + (revision ? '/revisions/' + encodeURIComponent(revision) : ''));
        if (generation !== readGeneration) return;
        active = current;
        status.textContent = `${active.focus_symbol || '研究'} · ${active.account_id} · 第 ${active.revision} 版 · ${active.status} · SCRIPTED / NOT_LIVE_MODEL`;
        if (active.status !== 'RUNNING') break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      deep.hidden = Boolean(revision) || active.status !== 'COMPLETE' || (active.mode || 'DEEP') !== 'FAST';
      retry.hidden = Boolean(revision) || active.status !== 'FAILED';
      if (revision) status.textContent += ' · 历史版本只读';
      if (active.status === 'FAILED') status.textContent += ` · ${active.error || 'RESEARCH_FAILED'}。保留失败版；重试会创建下一版。`;
      if (active.status === 'RUNNING') { status.textContent += '；等待上限已到，可只读恢复。'; return; }
      const current = active;
      const taskPath = '/api/research/tasks/' + encodeURIComponent(id);
      const [delta, first] = await Promise.all([
        api(`/api/portfolio-priority/tasks/${encodeURIComponent(id)}/delta?revision=${current.revision}`),
        current.revision === 1 ? current : api(taskPath + '/revisions/1'),
      ]);
      if (generation !== readGeneration) return;
      const header = el('header', undefined, 'pp-case-header');
      header.append(el('p', 'INVESTMENT CASE FILE', 'pp-case-kicker'),
        el('h2', `${current.focus_symbol || '研究'} · 投资案卷`),
        el('p', `${current.account_id} · 快照 ${stamp(overview.snapshot_as_of)} · PIT ${stamp(current.cutoff)}`, 'pp-case-meta'),
        el('p', `版本 ${current.revision} · ${current.status} · SCRIPTED / NOT_LIVE_MODEL`, 'pp-case-meta'));
      const comparison = el('div', undefined, 'pp-case-comparison');
      function revisionCard(task, label) {
        const card = el('section', undefined, 'pp-case-version');
        card.dataset.revision = task.revision;
        card.append(el('h3', label), el('p', task.action_decision?.decision_status || task.assessment?.disposition || 'UNKNOWN', 'pp-case-decision'));
        const fields = el('dl');
        for (const [name, statement] of [
          ['判断', task.assessment?.core_judgment],
          ['关键反证', task.assessment?.counterevidence?.[0]],
          ['下一步核查', task.assessment?.next_checks?.[0]],
        ]) {
          fields.append(el('dt', name), el('dd', statement?.text || '此版本未记录'));
        }
        card.append(fields);
        return card;
      }
      if (current.revision !== 1) comparison.append(revisionCard(first, '初始 Fast · 1'));
      comparison.append(revisionCard(current, current.revision === 1 ? '初始 Fast · 1' : `所选版本 · ${current.revision}`));
      const actions = el('nav', undefined, 'pp-case-actions');
      actions.setAttribute('aria-label', '投资案卷操作');
      if (current.assessment) {
        const evidenceJump = el('a', '前往本版依据'); evidenceJump.href = '#desk-evidence-open'; actions.append(evidenceJump);
      }
      if (current.action_decision) {
        const whatIfJump = el('a', '前往单账户 What-if'); whatIfJump.href = '#desk-case-whatif'; actions.append(whatIfJump);
      }
      const evidenceDetails = el('details', undefined, 'pp-case-evidence');
      evidenceDetails.append(el('summary', '查看新增证据、财务期间与来源'));
      receipt.replaceChildren(header, actions, comparison,
        el('p', `${delta.information_gain === 'NEW_PUBLIC_SEC_FACTS' ? 'Deep 补入可比期间公开财务事实' : delta.information_gain === 'NEW_METADATA_ONLY' ? '仅补入其他申报元数据，没有可比财务增量' : 'NO_NEW_EVIDENCE · 尚未增加公司事实'}；${delta.decision_status}。`, 'pp-case-gain'));
      const numbers = financialReceipts(delta); if (numbers) receipt.append(numbers);
      evidenceDetails.append(el('p', `${delta.prior_thesis}；${delta.prior_decision}。`, 'small'));
      for (const evidence of delta.new_evidence) {
        const d = evidence.structured_data;
        evidenceDetails.append(el('p', `证据 ${evidence.evidence_id} · ${d.cik} / ${evidence.symbol} · accession ${d.accession}`, 'small'));
        evidenceDetails.append(el('p', `Accepted ${stamp(d.accepted_at)}；捕获 ${stamp(d.capture_as_of)}；PIT ${stamp(d.pit_cutoff)}。`, 'small'));
        const source = el('a', '在 SEC 核对公开来源'); source.href = evidence.source_uri; source.target = '_blank'; source.rel = 'noopener noreferrer'; evidenceDetails.append(source);
        if (!d.metrics) { evidenceDetails.append(el('p', '仅有元数据，报告期间与财务单位不适用；不能据此形成经营结论。', 'small')); continue; }
        const table = el('table', undefined, 'pp-table pp-facts');
        const header = el('tr'); ['事实', '期间', '报告值 / 单位'].forEach(text => header.append(el('th', text))); table.append(header);
        for (const fact of d.metrics) { const row = el('tr'); row.append(el('td', fact.metric), el('td', `${fact.start} → ${fact.end}`), el('td', `${fact.value} ${fact.unit}`)); table.append(row); }
        const wrap = el('div', undefined, 'pp-table-wrap'); wrap.append(table); evidenceDetails.append(wrap);
        evidenceDetails.append(el('p', `Python Decimal：收入同比 ${d.comparison.revenue.yoy_pct}%；经营现金流同比 ${d.comparison.operating_cash_flow.yoy_pct}%。这不是单季增速或当前盈利预测。`, 'small'));
      }
      for (const check of delta.next_check) evidenceDetails.append(el('p', check));
      receipt.append(evidenceDetails);
      const trace = el('details'); trace.append(el('summary', '查看实际工具选择与执行 Trace'));
      const reasons = el('ol', undefined, 'pp-trace-list');
      for (const step of delta.tool_selection) reasons.append(el('li', `${step.tool} [${step.status}] — ${step.reason}`));
      const traceResult = el('div'); traceResult.setAttribute('aria-live', 'polite');
      trace.append(reasons, traceResult);
      let tracePending = false;
      trace.addEventListener('toggle', async () => {
        if (!trace.open || tracePending || trace.dataset.loaded) return;
        tracePending = true;
        traceResult.textContent = '正在读取本版 Trace。';
        try {
          const result = await api(taskPath + '/execution-traces');
          if (generation !== readGeneration) return;
          const list = el('ul', undefined, 'pp-trace-list');
          for (const row of result.receipts) {
            if (row.trace.revision_id !== String(current.revision)) continue;
            const tools = row.trace.spans.filter(span => span.kind === 'tool').map(span => span.name);
            const item = el('li', `版本 ${row.trace.revision_id} · ${row.trace.execution_mode} · ${tools.join(' → ') || row.trace.status} `);
            const link = el('a', `读取 Trace ${row.trace_id}`); link.href = '/api/execution-traces/' + encodeURIComponent(row.trace_id);
            link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link); list.append(item);
          }
          traceResult.replaceChildren(list.childElementCount ? list : el('p', '本版没有保存的执行 Trace。'));
          trace.dataset.loaded = '1';
        } catch (error) {
          traceResult.textContent = `Trace 读取失败：${error.message}。关闭后可重新打开。`;
        } finally { tracePending = false; }
      });
      receipt.append(trace); receipt.hidden = false;
    } catch (error) { if (generation === readGeneration) status.textContent = `保存状态读取失败：${error.message}。可再次读取；不会自动重跑研究。`; }
  }

  async function init() {
    try {
      overview = await api('/api/portfolio-priority');
      const introduction = el('div', undefined, 'pp-introduction');
      introduction.append(el('h1', '先研究谁，再决定资金放在哪里。'),
        el('p', '从四个账户的已投入资本开始，核对公开证据，再看单个账户能否承受你的假设。'));
      const notice = el('p', `PUBLIC_SEC 冻结捕获 / SYNTHETIC 账户与价格 / SCRIPTED / NOT_LIVE_MODEL。数据截止 ${stamp(overview.as_of)}；不是最新全仓判断。无密钥、无模型调用、无订单。`, 'pp-notice');
      const c = overview.coverage;
      const coverage = el('p', `${c.held_positions} 个持仓行 / ${c.held_symbols} 个证券；合成价格 ${c.synthetic_priced_symbols}/${c.held_symbols}，SEC 元数据 ${c.sec_metadata_symbols}/${c.held_symbols}，可比财务 ${c.comparable_financial_symbols}/${c.held_symbols}，当前报价 ${c.current_quote_symbols}/${c.held_symbols}。缺价：${c.missing_price_symbols.join(', ')}，当前 NAV 为 UNKNOWN。`, 'pp-coverage');
      const layout = el('div', undefined, 'pp-layout'), ranking = el('div');
      ranking.append(el('h2', '我应该优先研究哪个持仓？'), el('p', '按累计投入成本降序排列；同额按代码排序。排序覆盖所有合成持仓，来源覆盖另计。高成本不是高收益预期，也不是买卖建议。', 'small'));
      const table = el('table', undefined, 'pp-table');
      const tr = el('tr'); ['顺序 / 证券', '累计投入成本', '公开来源', '研究'].forEach(text => tr.append(el('th', text))); table.append(tr);
      for (const candidate of overview.candidates) {
        const row = el('tr'); row.dataset.symbol = candidate.symbol;
        const symbol = el('td', `${candidate.rank}. ${candidate.symbol}`); symbol.append(el('small', `${candidate.account_ids.length} 个账户`));
        const cost = el('td', dollar(candidate.committed_cost)); cost.append(el('small', candidate.mark_status === 'UNKNOWN' ? '价格 UNKNOWN' : '仅合成标记'));
        const source = el('td', candidate.sec_status === 'FROZEN_METADATA' ? '历史 SEC' : '未覆盖'); source.append(el('small', candidate.financial_status === 'FROZEN_COMPARABLE_YTD' ? '可比 YTD' : '财务缺失'));
        const control = el('td'); control.append(button(`选择 ${candidate.symbol}`, () => choose(candidate)));
        row.append(symbol, cost, source, control); table.append(row);
      }
      const wrap = el('div', undefined, 'pp-table-wrap'); wrap.append(table); ranking.append(wrap);
      const selection = el('div', undefined, 'pp-selection'); selection.id = 'pp-selection';
      const label = el('label', '账户范围'); label.htmlFor = 'pp-account'; label.append(account);
      const actions = el('div', undefined, 'pp-actions'); actions.append(start, deep, retry, read);
      selection.append(el('h2', '研究一个持仓'), chosen, label, actions, status,
        el('p', 'Fast 定位申报；Deep 补读可比现金流事实。结果保留判断、支持、反证、来源、版本和账户预演。', 'small'));
      layout.append(ranking, selection);
      const ledger = el('details', undefined, 'pp-ledger'); ledger.id = 'pp-ledger'; ledger.append(el('summary', '查看四账户、现金、全部持仓与 as-of'));
      ledger.append(el('p', `持仓 ${stamp(overview.snapshot_as_of)}；合成标记 ${stamp(overview.marks_as_of)}。现金仅属于各自账户。`, 'small'));
      for (const a of overview.accounts) {
        const section = el('section', undefined, 'pp-account');
        section.append(el('h3', `${a.name} (${a.account_id})`), el('p', `现金 ${dollar(a.cash)} · 合成 NAV ${dollar(a.synthetic_nav)} · 当前 NAV UNKNOWN`));
        for (const p of a.positions) section.append(el('p', `${p.symbol} · ${p.shares} 股 · 平均成本 ${dollar(p.avg_cost)} · 累计成本 ${dollar(p.committed_cost)} · 合成价格 ${dollar(p.synthetic_mark)}`));
        ledger.append(section);
      }
      root.replaceChildren(introduction, notice, receipt, coverage, layout, ledger);
      const skip = document.querySelector('.skip-link'); skip.href = '#portfolio-priority'; skip.textContent = '跳到研究优先级';
      document.querySelector('.topbar .tag').textContent = '合成组合 · 无密钥';
      const holdings = document.querySelector('.rail nav a[href="#portfolio"]');
      if (holdings) { holdings.href = '#pp-ledger'; holdings.addEventListener('click', () => { ledger.open = true; }); }
      const universe = document.querySelector('.rail nav a[href="#universe"]'); if (universe) universe.hidden = true;
      await readTask();
    } catch (error) { root.replaceChildren(el('p', `合成旅程读取失败：${error.message}`, 'pp-error'), button('重新读取旅程', init)); }
  }
  // Keep the existing result card's Fast→Deep CTA on this same typed boundary.
  document.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (target && target.textContent === '继续为 Deep · 保留同一条记录') {
      event.preventDefault(); event.stopImmediatePropagation(); void submit('DEEP');
    }
  }, true);
  let navigationKey = new URL(location.href).searchParams.toString();
  function syncReadingTask() {
    const params = new URL(location.href).searchParams, key = params.toString();
    if (key === navigationKey) return;
    navigationKey = key;
    if (params.has('research_task')) void readTask();
    else { ++readGeneration; active = null; deep.hidden = true; retry.hidden = true; read.hidden = true; receipt.hidden = true; }
  }
  // The existing reader uses pushState for history entries and replaceState for
  // version selection. Observe its actual rendered task, not only full page loads.
  new MutationObserver(syncReadingTask).observe(document.getElementById('desk-task-meta'), {childList: true, subtree: true});
  window.addEventListener('popstate', syncReadingTask);
  void init();
})();
