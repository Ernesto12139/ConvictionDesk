'use strict';
(() => {
  const host = document.getElementById('inspector');
  if (!host) return;
  const box = document.createElement('details');
  const title = document.createElement('summary'); title.textContent = '实际执行 Trace';
  const button = document.createElement('button'); button.type = 'button'; button.textContent = '读取最近研究 Trace';
  const output = document.createElement('pre'); output.style.whiteSpace = 'pre-wrap'; output.style.overflowWrap = 'anywhere';
  output.textContent = '按实际执行记录工具、来源、拒绝与修订；历史记录没有新 Trace 时如实显示。';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/api/research/tasks', {cache: 'no-store'});
      if (!response.ok) throw Error('TASKS_UNAVAILABLE');
      const data = await response.json();
      const task = (Array.isArray(data) ? data : data.tasks || [])[0];
      if (!task) { output.textContent = '当前数据范围尚无研究任务。'; return; }
      const result = await fetch('/api/research/tasks/' + encodeURIComponent(task.task_id) + '/execution-traces', {cache: 'no-store'});
      if (!result.ok) throw Error('TRACE_UNAVAILABLE');
      const traces = await result.json();
      output.textContent = traces.receipts.length ? JSON.stringify(traces, null, 2) : '此历史任务尚无新执行 Trace；未从旧记录伪造。';
    } catch { output.textContent = '无法读取当前范围的 Trace；请刷新或检查本机权限。'; }
    finally { button.disabled = false; }
  });
  box.append(title, button, output); host.append(box);
})();
