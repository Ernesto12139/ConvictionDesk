"use strict";
// 今日 (D104): "can I trust today's data?" above the day's leads, plus one 今日更新 button.
// Data status prefers GET /api/data-health; otherwise it reads what the existing
// read-only endpoints report (SEC filings scan, news scan coverage, local capture,
// research readiness). 今日更新 only presses the existing, server-guarded refresh
// controls (SEC filings scan; local sources when that card offers a refresh). It never
// starts the attention scan, which may call a model, and never submits research.
(() => {
  const ds = document.body.dataset;
  if (ds.portfolioPriority) return;
  const attention = document.getElementById("attention");
  if (!attention) return;
  const synthetic = ds.runtimeMode === "PUBLIC_DEMO" || Boolean(ds.ivsPreview);
  const owner = ds.runtimeMode === "OWNER" && !ds.ivsPreview && !ds.publicIvs;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const read = url => fetch(url, { cache: "no-store", headers: { Accept: "application/json" } }).then(r => r.ok ? r.json() : null).catch(() => null);
  const utc = iso => { const m = typeof iso === "string" && iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/); return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : null; };
  const TONE_LABEL = { ok: "可用", warn: "需留意", blocked: "不可用", neutral: "未知" };

  const box = el("section", null, "today-health"); box.id = "today-health";
  box.setAttribute("aria-labelledby", "today-health-title");
  const top = el("div", null, "today-health-top");
  const title = el("h2", "数据状态", null); title.id = "today-health-title";
  const update = el("button", "今日更新", "primary"); update.type = "button"; update.id = "today-update";
  top.append(title, update);
  const list = el("ul", null, "today-health-list");
  const status = el("p", "", "small today-update-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const steps = el("ol", null, "today-update-steps"); steps.hidden = true;
  box.append(top, list, status, steps);
  attention.prepend(box);

  function row(name, tone, text) {
    const li = el("li", null, "today-health-row"); li.dataset.tone = tone; li.dataset.source = name;
    li.append(el("span", name, "today-health-name"), el("span", TONE_LABEL[tone] || tone, "today-health-state"), el("span", text, "today-health-text small"));
    return li;
  }

  async function renderHealth() {
    if (synthetic) {
      list.replaceChildren(row("数据", "neutral", "合成回放：账户、行情与资料均为冻结示例，不是实时数据。"));
      update.hidden = true;
      return;
    }
    const server = await read("/api/data-health");
    if (server && Array.isArray(server.sources)) {
      const STATUS = { OK: "ok", DEGRADED: "warn", STALE: "warn", NOT_CONFIGURED: "blocked", RIGHTS_BLOCKED: "blocked", ERROR: "blocked" };
      const NAMES = { SEC: "SEC 披露", FINNHUB_NEWS: "新闻", MASSIVE_EOD: "日线收盘", FINNHUB_QUOTE: "报价", LONGBRIDGE: "长桥", MODEL: "研究模型" };
      list.replaceChildren(...server.sources.map(s => row(NAMES[s.source] || s.source, STATUS[s.status] || "neutral",
        [s.last_success_at && `最近成功 ${utc(s.last_success_at)}`, s.as_of && `数据截至 ${String(s.as_of).slice(0, 10)}`, s.fallback_reason, s.next_action && `下一步：${s.next_action}`].filter(Boolean).join(" · ") || s.status)));
      return;
    }
    const [filings, queue, local] = await Promise.all([read("/api/today/filings"), read("/api/attention/queue"), owner ? fetch("/api/local-observations", { cache: "no-store", headers: { Accept: "application/json" } }).then(r => r.status === 404 ? { status: "NOT_CONFIGURED" } : r.ok ? r.json() : null).catch(() => null) : Promise.resolve(null)]);
    const rows = [];
    if (filings) {
      const at = utc(filings.scan_as_of || filings.as_of);
      rows.push(filings.status === "SCANNED" ? row("SEC 披露", "ok", `已扫描 ${at || "时间未知"} · ${filings.scope_count ?? "?"} 家公司`)
        : row("SEC 披露", "warn", filings.status === "NOT_SCANNED" ? "尚未扫描；点「今日更新」读取公开 SEC 披露。" : `状态 ${filings.status || "未知"}`));
    } else rows.push(row("SEC 披露", "neutral", "无法读取 SEC 披露状态。"));
    const cov = queue?.coverage_state || {};
    if (queue) {
      const last = utc(cov.last_successful_scan_at);
      rows.push(last ? row("新闻扫描", "ok", `最近成功 ${last} · 检查 ${cov.checked_symbols ?? "?"} / ${cov.active_symbols ?? "?"} 个标的`)
        : row("新闻扫描", "warn", "还没有成功的扫描。可在下方「刷新今日关注」发起（可能使用模型筛选）。"));
    } else rows.push(row("新闻扫描", "neutral", "无法读取新闻扫描状态。"));
    if (owner) {
      const m = { READY: ["ok", `本机捕获 ${utc(local?.captured_at) || "时间未知"}`], NOT_CONFIGURED: ["blocked", "未配置本机行情与新闻捕获。"],
        INVALID_CAPTURE: ["blocked", "本机捕获校验未通过，未显示任何内容。"], OWNER_ONLY: ["neutral", "仅在 Owner 本机显示。"] }[local?.status] || ["neutral", "无法读取本机来源。"];
      rows.push(row("收盘与新闻（本机）", m[0], m[1]));
    }
    const ready = window.miaReadinessState;
    rows.push(modelRow(ready));
    list.replaceChildren(...rows);
  }
  function modelRow(state) {
    const line = document.getElementById("desk-readiness");
    if (!state) return row("研究模型", "neutral", "正在读取…");
    const tone = line?.dataset.tone || "neutral";
    return row("研究模型", tone, line?.firstChild?.textContent || "见「设置」中的研究就绪。");
  }
  window.addEventListener("mia-readiness", event => {
    const old = list.querySelector('[data-source="研究模型"]');
    if (old) old.replaceWith(modelRow(event.detail));
  });

  // ---------- 今日更新: press the existing guarded refresh controls, one at a time ----------
  const busy = node => node && (node.getAttribute("aria-busy") === "true");
  const waitIdle = (node, ms = 90000) => new Promise(resolve => {
    const start = Date.now();
    const tick = () => { if (!busy(node) || Date.now() - start > ms) resolve(!busy(node)); else setTimeout(tick, 400); };
    setTimeout(tick, 300);
  });
  function availableSteps() {
    const out = [];
    const sec = document.querySelector("#ivs-filings .section-heading button, #ivs-filings button");
    const secLane = document.getElementById("ivs-filings");
    if (sec && secLane) out.push({ name: "SEC 公开披露", button: sec, lane: secLane, result: () => secLane.querySelector("[role=status], .status-line")?.textContent?.trim() });
    else out.push({ name: "SEC 公开披露", skip: "本运行没有 SEC 披露扫描。" });
    const localSection = document.getElementById("local-observations");
    const localButton = localSection?.querySelector(".desk-local-refresh");
    if (localButton && !localButton.hidden) out.push({ name: "收盘与新闻（本机授权）", button: localButton, lane: localSection, result: () => localSection.querySelector(".desk-local-state")?.textContent?.trim() });
    else out.push({ name: "收盘与新闻（本机授权）", skip: owner ? "当前没有可用的本机来源授权或捕获。" : "仅 Owner 本机可用。" });
    return out;
  }
  let running = false;
  update.addEventListener("click", async () => {
    if (running) { status.textContent = "今日更新正在进行，没有重复发起。"; return; }
    running = true; update.disabled = true; box.setAttribute("aria-busy", "true");
    const plan = availableSteps();
    steps.hidden = false; steps.replaceChildren(...plan.map(p => { const li = el("li", `${p.name}：${p.skip ? "跳过 · " + p.skip : "等待"}`); li.dataset.state = p.skip ? "SKIPPED" : "QUEUED"; return li; }));
    status.textContent = "正在更新只读数据源；不会发起研究或调用研究模型。";
    let done = 0;
    for (let i = 0; i < plan.length; i++) {
      const p = plan[i], li = steps.children[i];
      if (p.skip) continue;
      if (p.button.disabled || busy(p.lane)) { li.dataset.state = "SKIPPED"; li.textContent = `${p.name}：跳过 · 正在进行中`; continue; }
      li.dataset.state = "RUNNING"; li.textContent = `${p.name}：进行中…`;
      p.button.click();
      const finished = await waitIdle(p.lane);
      const text = p.result() || "";
      li.dataset.state = finished ? "DONE" : "FAILED";
      li.textContent = `${p.name}：${finished ? "完成" : "超时"}${text ? " · " + text.slice(0, 160) : ""}`;
      if (finished) done += 1;
    }
    status.textContent = done ? `今日更新完成 ${done} 项；线索与状态已重新读取。` : "没有可以更新的数据源；原有资料保持不变。";
    running = false; update.disabled = false; box.removeAttribute("aria-busy");
    void renderHealth();
  });

  void renderHealth();
})();
