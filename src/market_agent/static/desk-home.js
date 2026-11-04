"use strict";
// Investor-first home: one status line, today's scan state, holdings in research
// order, and whether Ask can run. Read-only GETs of existing endpoints. Every button
// reuses an existing control (scan, providers panel, question form, saved task,
// What-if dialog); nothing here submits research, spends budget or edits holdings.
(() => {
  const body = document.body;
  if (body.dataset.portfolioPriority) return; // the priority journey has its own queue
  const $ = id => document.getElementById(id);
  const runtime = body.dataset.runtimeMode;
  const publicDemo = runtime === "PUBLIC_DEMO";
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const button = (text, onClick, primary = false) => { const b = el("button", text, primary ? "primary" : null); b.type = "button"; b.addEventListener("click", onClick); return b; };
  const read = async url => { const r = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } }); if (!r.ok) throw new Error(String(r.status)); return r.json(); };
  const optional = url => read(url).catch(() => null);
  const day = iso => iso ? new Date(iso).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }) : "未知";
  const stamp = iso => iso ? new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "未知";
  const ago = iso => {
    if (!iso) return "";
    const hours = (Date.now() - Date.parse(iso)) / 3600000;
    return hours < 1 ? "不到 1 小时前" : hours < 48 ? `${Math.round(hours)} 小时前` : `${Math.round(hours / 24)} 天前`;
  };
  const openProviders = () => window.dispatchEvent(new CustomEvent("mia-open-providers"));
  const scan = () => { const b = $("attention-scan"); if (b && !b.hidden && !b.disabled) b.click(); };

  // ---- placement: the work first, the complete diagnostics after it ----
  const main = document.querySelector("main");
  const heading = document.querySelector(".page-heading");
  const status = el("ul", null, "desk-status"); status.id = "desk-status"; status.setAttribute("aria-label", "数据状态");
  heading?.after(status);
  const diagnostics = el("details"); diagnostics.id = "desk-diagnostics";
  diagnostics.append(el("summary", "数据来源与运行状态"),
    el("p", "工作空间范围、价格观察、来源权限与模型状态的完整记录。首页只摘要显示，这里保留全部来源与时间。", "small muted"));
  for (const id of ["workbench-mode", "owner-market-observations", "owner-value"]) {
    const node = $(id); if (node) { node.dataset.placed = "1"; diagnostics.append(node); }
  }
  ($("research-history") || main.lastElementChild).after(diagnostics);
  const showDiagnostics = () => { diagnostics.open = true; diagnostics.scrollIntoView({ block: "start" }); diagnostics.querySelector("summary").focus(); };

  // Today block sits directly under the Today heading, before feeds and tabs.
  const attention = $("attention");
  const todayHead = attention?.querySelector(".section-heading");
  const today = el("div"); today.id = "desk-today";
  todayHead?.after(today);

  function chip(label, value, tone, action) {
    const li = el("li"); li.dataset.tone = tone || "neutral";
    li.append(el("span", null, "dot"), el("span", label + " "));
    if (action) { const b = el("button", value, "desk-linklike"); b.type = "button"; b.addEventListener("click", action); li.append(b); }
    else li.append(el("b", value));
    status.append(li);
  }

  function todayState(queue) {
    if (!queue) return { code: "UNAVAILABLE" };
    const cov = queue.coverage_state || {};
    const items = (queue.items || []).length;
    const attempt = cov.last_attempt_status || "NEVER";
    const providerFailed = queue.scan_result_status === "PROVIDER_UNAVAILABLE" || /FAIL|ERROR/.test(attempt);
    if (publicDemo) return { code: "SYNTHETIC_REPLAY", items, at: queue.latest_scan?.report?.frozen_cutoff || cov.last_successful_scan_at };
    if (cov.state === "NEVER" && attempt === "NEVER") return { code: "NOT_SCANNED" };
    if (providerFailed && cov.state !== "FRESH") return { code: "PROVIDER_ERROR", at: cov.last_successful_scan_at, attempt: cov.last_attempt_at };
    if (cov.state === "STALE" || cov.state === "OLD") return { code: "STALE_SCAN", at: cov.last_successful_scan_at };
    if (cov.state === "FRESH" && !items) return { code: "SCANNED_NO_QUALIFYING_EVENTS", at: cov.last_successful_scan_at, checked: cov.checked_symbols, active: cov.active_symbols };
    return { code: "HAS_EVENTS", items, at: cov.last_successful_scan_at, checked: cov.checked_symbols, active: cov.active_symbols };
  }

  function renderState(state) {
    body.dataset.todayState = state.code;
    const card = el("section", null, "today-state"); card.dataset.state = state.code; card.setAttribute("aria-label", "今日扫描状态");
    const actions = el("div");
    const copy = {
      NOT_SCANNED: ["今天还没有扫描", "持仓和关注公司的新闻与披露尚未检查。", [["扫描持仓变化", scan, true]]],
      PROVIDER_ERROR: ["新闻来源暂不可用", (state.attempt && state.attempt !== state.at
          ? `最近一次尝试 ${stamp(state.attempt)} 没有取得来源；上次成功扫描 ${stamp(state.at)}（${ago(state.at)}）。`
          : `最近一次扫描 ${stamp(state.attempt || state.at)}（${ago(state.attempt || state.at)}）没有取得新闻来源，之后没有再扫描。`) + "下方较早材料不算今日变化。",
        [["检查数据源", openProviders, true], ["重试扫描", scan]]],
      STALE_SCAN: ["上次扫描已过时", `最近一次成功扫描 ${stamp(state.at)}（${ago(state.at)}），之后的变化还没有检查。`, [["扫描持仓变化", scan, true]]],
      SCANNED_NO_QUALIFYING_EVENTS: ["今天没有需要优先处理的变化", `扫描于 ${stamp(state.at)}，检查了 ${state.checked ?? "?"} / ${state.active ?? "?"} 个标的。未检查的标的仍可能有变化。`, []],
      HAS_EVENTS: [`${state.items} 项变化待研究`, `扫描于 ${stamp(state.at)}，检查了 ${state.checked ?? "?"} / ${state.active ?? "?"} 个标的。逐项卡片在下方。`, []],
      SYNTHETIC_REPLAY: [`${state.items} 项合成变化 · 示例`, `冻结扫描 ${stamp(state.at)}。虚构公司与来源，不是实时新闻。`, []],
      UNAVAILABLE: ["今日关注暂时无法读取", "请刷新页面；已保存的研究记录不受影响。", [["刷新", () => location.reload()]]],
    }[state.code];
    const code = el("span", state.code, "today-state-code");
    card.append(code, el("h3", copy[0]), el("p", copy[1]));
    copy[2].forEach(([label, fn, primary]) => actions.append(button(label, fn, primary)));
    card.append(actions);
    return card;
  }

  const DISPOSITION = { ADD: "倾向加仓", HOLD: "倾向持有", REDUCE: "倾向减仓", EXIT: "倾向退出", UNDETERMINED: "方向未定", UNKNOWN: "方向未定", RESEARCH_ONLY: "仅研究" };
  const percent = text => { const n = parseFloat(String(text || "").replace(/[^0-9.\-]/g, "")); return Number.isFinite(n) ? n : 0; };

  function latestBySymbol(tasks) {
    const map = new Map();
    for (const task of tasks || []) {
      if (!task.focus_symbol) continue;
      const prior = map.get(task.focus_symbol);
      if (!prior || String(task.updated_at) > String(prior.updated_at)) map.set(task.focus_symbol, task);
    }
    return map;
  }

  function openTask(task) {
    const url = new URL(location.href); url.searchParams.set("research_task", task.task_id); url.searchParams.delete("revision");
    history.pushState(null, "", url); window.dispatchEvent(new PopStateEvent("popstate"));
  }

  function ask(symbol, question) {
    const box = $("desk-question"); if (!box || box.readOnly) return;
    box.value = question; box.dispatchEvent(new Event("input", { bubbles: true }));
    $("ask-mia")?.scrollIntoView({ block: "start" }); box.focus();
    const hint = $("desk-request-status"); if (hint) hint.textContent = `已拟定 ${symbol} 的研究问题，可修改后提交；提交前会确认账户与费用。`;
  }

  // A quote is shown only next to its own symbol, with provider, market time and delay.
  function quoteLine(record) {
    if (!record) return null;
    const kind = record.basis === "QUOTE_CURRENT" ? "报价 · 可能延迟" : "收盘价 · 非实时";
    return `${record.currency || "USD"} ${record.price} · ${record.provider} ${kind} · 市场时间 ${stamp(record.provider_timestamp)} · 取得 ${stamp(record.retrieved_at)}`;
  }

  function renderPriority(overview, tasks, queue, observations) {
    const positions = overview?.positions || [];
    if (!positions.length) return null;
    const costShare = new Map((overview.allocation?.symbols || []).map(s => [s.label, s.weight]));
    const known = text => /\d/.test(String(text || ""));
    const quotes = new Map();
    for (const r of observations?.records || []) {
      const prior = quotes.get(r.symbol);
      const better = !prior || (r.basis === "QUOTE_CURRENT" && prior.basis !== "QUOTE_CURRENT") || (r.basis === prior.basis && String(r.provider_timestamp) > String(prior.provider_timestamp));
      if (better) quotes.set(r.symbol, r);
    }
    const latest = latestBySymbol(tasks);
    const changed = new Map();
    for (const row of queue?.items || []) if (row.item?.symbol && !changed.has(row.item.symbol)) changed.set(row.item.symbol, row.item);
    const now = Date.now();
    const rows = positions.map(position => {
      const task = latest.get(position.symbol), change = changed.get(position.symbol);
      const age = task ? (now - Date.parse(task.updated_at)) / 86400000 : null;
      const kind = change ? "CHANGE" : !task ? "NEVER" : age > 14 ? "OLD" : "RECENT";
      const share = known(position.weight) ? position.weight : costShare.get(position.symbol);
      return { position, task, change, kind, age, share, weight: percent(share) };
    });
    const rank = { CHANGE: 0, NEVER: 1, OLD: 2, RECENT: 3 };
    rows.sort((a, b) => rank[a.kind] - rank[b.kind] || b.weight - a.weight || a.position.symbol.localeCompare(b.position.symbol));

    const wrap = el("section", null, "priority-list"); wrap.setAttribute("aria-labelledby", "priority-title");
    const head = el("div", null, "priority-head");
    const title = el("h3", "先研究哪些持仓"); title.id = "priority-title";
    const basis = rows.some(r => known(r.position.weight)) ? `市值占比 · 快照 ${day(overview.portfolio_as_of)}` : `成本占比（缺少合格价格，市值未知）· 快照 ${day(overview.portfolio_as_of)}`;
    head.append(title, el("p", basis));
    const list = el("ol", null, "priority-rows");
    const whatIf = typeof window.miaOpenWhatIf === "function" && !publicDemo;
    const canAsk = !$("desk-question")?.readOnly;
    const render = row => {
      const { position, task, change, kind } = row;
      const li = el("li", null, "priority-row");
      const symbol = el("div", null, "priority-symbol");
      symbol.append(el("b", position.symbol), el("span", `${row.share || "占比较小"} · ${position.account_count || 1} 个账户`));
      const why = el("div", null, "priority-why");
      const reason = el("span", { CHANGE: "新变化", NEVER: "尚未研究", OLD: `${Math.round(row.age)} 天前研究`, RECENT: `${Math.max(0, Math.round(row.age))} 天前研究` }[kind], "reason");
      reason.dataset.kind = kind; why.append(reason);
      if (change) why.append(el("span", change.what_changed?.text || "有新的公开材料"));
      else if (task) why.append(el("span", `上次：${DISPOSITION[task.assessment?.disposition] || "方向未定"} · ${task.assessment?.core_judgment?.text || task.question}`));
      else why.append(el("span", "还没有保存的判断、证据或反证。"));
      if (task) why.append(el("small", `证据 ${(task.sources || []).length} 条 · 反证 ${(task.assessment?.counterevidence || []).length} 条 · 第 ${task.revision} 轮`));
      const quote = quoteLine(quotes.get(position.symbol));
      if (quote) why.append(el("small", quote, "priority-quote"));
      const actions = el("div", null, "priority-actions");
      const question = change ? `${position.symbol}：这项新变化会改变我的持仓逻辑吗？` : task ? `${position.symbol}：上次判断之后，有什么新证据支持或推翻它？` : `${position.symbol}：我持有它的逻辑，现在有哪些公开证据支持和反对？`;
      if (canAsk) actions.append(button("研究", () => ask(position.symbol, question), kind === "CHANGE" || kind === "NEVER"));
      if (task) actions.append(button("证据与反证", () => openTask(task)));
      if (whatIf) actions.append(button("账户后果", () => window.miaOpenWhatIf(position.symbol)));
      li.append(symbol, why, actions);
      return li;
    };
    const shown = matchMedia("(max-width: 760px)").matches ? 3 : 5; // phones reach Ask sooner
    rows.slice(0, shown).forEach(row => list.append(render(row)));
    wrap.append(head, list);
    if (rows.length > shown) {
      const rest = el("ol", null, "priority-rows"); rows.slice(shown).forEach(row => rest.append(render(row)));
      const more = el("details", null, "priority-more"); more.append(el("summary", `其余 ${rows.length - shown} 个持仓`), rest);
      wrap.append(more);
    }
    wrap.append(el("p", "排序：新变化 → 尚未研究 → 研究较久 → 占比。只决定先研究什么，不是买卖建议。", "priority-note"));
    return wrap;
  }

  function renderReadiness(profiles) {
    const help = $("desk-mode-help"); if (!help || publicDemo) return;
    $("ask-readiness")?.remove();
    const box = el("div", null, "ask-readiness"); box.id = "ask-readiness"; box.setAttribute("role", "status");
    const ready = profiles && Object.values(profiles.preview_mode_availability || {}).some(Boolean);
    box.dataset.ready = String(Boolean(ready));
    if (ready) {
      box.append(el("b", "可以开始研究。"), el("span", " 提交前会确认账户范围、模型与费用上限；Fast 结果可继续 Deep，同一任务保留每一轮。"));
    } else {
      const blockers = new Set(Object.values(profiles?.dispatch_blockers || {}));
      const reason = !profiles ? "无法读取模型配置。" :
        blockers.has("AUTH_NOT_VERIFIED") ? "模型密钥已保存，但还没有验证。验证会发出一次请求，可能计费，需要你在「模型与数据源」确认。" :
        blockers.has("BUDGET_RESERVATION_REQUIRED") ? "缺少本次研究的模型预算预约。" :
        blockers.has("PRICE_CARD_EXPIRED") ? "模型价格资格已过期，需要更新后才能估算费用。" :
        "还没有配置可用的研究模型。";
      box.append(el("b", "研究暂不能运行。"), el("span", " " + reason + " 披露、已保存研究和账户预演仍可使用。"));
      if (profiles) box.append(button("打开模型与数据源", openProviders));
    }
    help.after(box);
    return ready;
  }

  async function load() {
    const [queue, overview, tasks, profiles, observations] = await Promise.all([
      optional("/api/attention/queue"), optional("/api/console/portfolio-overview"),
      optional("/api/research/tasks"), publicDemo ? null : optional("/api/research/model-profiles"),
      runtime === "OWNER" && document.getElementById("owner-value") ? optional("/api/owner/market-observations") : null]);
    const state = todayState(queue);
    today.replaceChildren(renderState(state));
    const priority = renderPriority(overview, tasks, queue, observations);
    if (priority) today.append(priority);
    if ($("ask-mia")) { const jump = el("a", "提出研究问题 ↓", "desk-ask-jump"); jump.href = "#ask-mia"; today.append(jump); }
    const ready = renderReadiness(profiles);

    status.replaceChildren();
    chip("数据", publicDemo ? "公开合成示例" : runtime === "OWNER" ? "本机组合" : "本地", publicDemo ? "warn" : "ok", showDiagnostics);
    if (overview?.portfolio_as_of) chip("持仓快照", day(overview.portfolio_as_of), "neutral");
    const scanTone = { HAS_EVENTS: "ok", SCANNED_NO_QUALIFYING_EVENTS: "ok", SYNTHETIC_REPLAY: "neutral" }[state.code] || "warn";
    chip("今日扫描", { NOT_SCANNED: "未扫描", PROVIDER_ERROR: "来源不可用", STALE_SCAN: ago(state.at), SCANNED_NO_QUALIFYING_EVENTS: stamp(state.at), HAS_EVENTS: stamp(state.at), SYNTHETIC_REPLAY: "冻结回放", UNAVAILABLE: "无法读取" }[state.code], scanTone);
    const missing = overview?.missing_price_symbol_count;
    if (overview) chip("价格", missing ? `${missing} 个缺价 · 市值未知` : overview.valuation_status === "complete" ? `快照 ${day(overview.market_cutoff)}` : "未计价", missing ? "warn" : "neutral", showDiagnostics);
    if (!publicDemo) chip("研究模型", ready ? "可用" : "未就绪", ready ? "ok" : "warn", ready ? null : openProviders);
  }

  // Market pulse is useful only when configured; keep it under Today/Ask either way.
  const grid = document.querySelector(".desk-entry-grid");
  const pulse = $("today-pulse");
  if (grid && pulse) grid.after(pulse);
  load().catch(() => { today.replaceChildren(renderState({ code: "UNAVAILABLE" })); });
  // Re-read after the existing scan control repaints the queue (scan, continue, review).
  let timer = null;
  const queueNode = $("attention-queue");
  if (queueNode) new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(() => void load().catch(() => {}), 600); })
    .observe(queueNode, { childList: true });
})();
