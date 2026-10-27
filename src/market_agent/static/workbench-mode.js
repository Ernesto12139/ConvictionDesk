'use strict';
(() => {
  const status = document.getElementById('workbench-mode-status');
  if (!status) return;
  const buttons = [...document.querySelectorAll('[data-workbench-scope]')];
  fetch('/api/workbench/mode', {cache:'no-store'}).then(r => {
    if (!r.ok) throw Error('mode');
    return r.json();
  }).then(data => {
    document.body.dataset.workbenchScope = data.scope;
    status.textContent = data.scope === 'SYNTHETIC'
      ? '当前：合成体验 · Fast/Deep 为脚本 · 无模型费用'
      : data.scope === 'OWNER_LOCAL'
        ? '当前：Owner 本机研究 · OWNER_LOCAL · 行情、来源权限与模型调用分别核验'
        : '当前：Owner 接入预检 · 空白沙盒 · 真实持仓尚未接入 / 模型未配置 / paid dispatch BLOCKED';
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.workbenchScope === data.scope)));
  }).catch(() => { status.textContent = '无法读取模式；请刷新，不要推断真实数据已接入。'; });
  buttons.forEach(button => button.addEventListener('click', async () => {
    buttons.forEach(b => { b.disabled = true; });
    try {
      const response = await fetch('/api/workbench/mode', {method:'POST',
        headers:{'Content-Type':'application/json','X-Workbench-Mode':'select'},
        body:JSON.stringify({scope:button.dataset.workbenchScope})});
      if (!response.ok) throw Error('mode');
      location.href = '/';
    } catch {
      status.textContent = '切换未完成；原数据范围保持不变。';
      buttons.forEach(b => { b.disabled = false; });
    }
  }));
})();
