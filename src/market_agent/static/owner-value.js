'use strict';
(() => {
  const status = document.getElementById('owner-value-status');
  const content = document.getElementById('owner-value-content');
  if (!status || !content) return;
  const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
  function show(data) {
    const v = data.view;
    status.textContent = `${v.symbol} · OWNER_LOCAL · SEC 原始留存 · ${data.qualification.body}`;
    content.replaceChildren();
    content.append(node('p', `${v.holding.account_ref} · 持仓截至 ${v.holding.as_of} · ${v.holding.freshness}`));
    content.append(node('p', `价格资格：${data.qualification.price} · 市值：${v.valuation.value_text || 'UNKNOWN（没有合格报价）'}`));
    content.append(node('p', `SEC ${v.event.form || ''} · ${v.event.accession || ''} · 接受于 ${v.event.accepted_at || 'UNKNOWN'}`));
    if (v.event.source_uri) {
      const link = node('a', '打开 SEC 原文');
      const url = new URL(v.event.source_uri);
      if (url.protocol === 'https:' && ['www.sec.gov', 'data.sec.gov', 'sec.gov'].includes(url.hostname)) {
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link);
      }
    }
    content.append(node('p', v.event.excerpt || '正文缺口：尚未取得符合来源资格的正文。'));
    content.append(node('p', `事前投资逻辑：${v.thesis.status} · ${v.thesis.next_check || '先补齐事前依据，不能倒写成当时已知。'}`));
    content.append(node('p', 'Fast / Deep 使用下方现有研究入口。真实请求仍需服务器外传同意、来源权限和预算预约；保存在本机的 Key 不等于调用已获准。'));
    const ask = node('button', '将这份披露带入研究'); ask.type = 'button';
    ask.addEventListener('click', () => window.dispatchEvent(new CustomEvent('mia-ask-draft', {
      detail: {symbol: v.symbol, question: `${v.symbol} 的 SEC ${v.event.accession} 原文显示了什么变化？事前投资逻辑与报价缺口有哪些？`}})));
    content.append(ask);
    const form = document.createElement('form');
    const side = document.createElement('select'); side.setAttribute('aria-label', '预演方向');
    for (const value of ['BUY', 'SELL']) { const opt = node('option', value); opt.value = value; side.append(opt); }
    const inputs = {};
    for (const [name, label] of [['shares', '预演股数'], ['price', '假设成交价']]) {
      const wrap = node('label', label); const input = document.createElement('input');
      input.required = true; input.inputMode = 'decimal'; input.name = name; wrap.append(input); form.append(wrap); inputs[name] = input;
    }
    const submit = node('button', '计算假设调整'); submit.type = 'submit'; form.prepend(side); form.append(submit);
    form.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        const hub = await window.miaHubReady;
        const response = await fetch('/api/owner/whatif', {method: 'POST', headers: {
          'Content-Type': 'application/json', 'X-Provider-CSRF': hub?.token || ''}, body: JSON.stringify({
            side: side.value, shares: inputs.shares.value, price: inputs.price.value,
            price_basis: 'HYPOTHETICAL_OPERATOR_INPUT', rationale: 'Owner 本机显式假设预演'})});
        if (!response.ok) throw Error('WHATIF_BLOCKED');
        show(await response.json());
      } catch { status.textContent = '预演未完成；请检查输入、来源范围与本机会话，原始组合未更改。'; }
      finally { submit.disabled = false; }
    });
    content.append(form);
    if (v.whatif) content.append(node('p', `假设预演 · ${v.whatif.mode} · NAV ${v.whatif.nav_text || 'UNKNOWN'} · 无订单、未保存为真实组合`));
    const trace = node('a', '查看本次实际 Trace'); trace.href = '/api/execution-traces/' + encodeURIComponent(data.trace_id);
    trace.target = '_blank'; trace.rel = 'noopener'; content.append(trace);
  }
  fetch('/api/owner/value', {cache: 'no-store'}).then(async r => {
    if (!r.ok) throw Error('OWNER_INPUTS_NOT_QUALIFIED'); return r.json();
  }).then(show).catch(() => { status.textContent = '本机证据范围不可用或组合已更新，请重新核验本机输入；没有使用合成数据替代。'; });
})();
