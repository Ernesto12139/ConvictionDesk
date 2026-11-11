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
  let noSendDeployment = false;
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
  const status = el("ul", null, "desk-status"); status.id = "desk-status"; status.setAttribute("aria-label", "研究前提：数据、来源、模型与费用");
  const readinessHome = $("ask-mia")?.querySelector(".desk-compose-actions");
  if (readinessHome) { const title = el("p", "研究前提", "desk-status-title"); title.id = "desk-status-title"; readinessHome.after(title, status); }
  else heading?.after(status);
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
  const queueHint = attention?.querySelector(".desk-queue-hint");
  (queueHint || todayHead)?.after(today);

  function chip(label, value, tone, action) {
    const li = el("li"); li.dataset.tone = tone || "neutral";
    li.append(el("span", null, "dot"), el("span", label + " "));
    if (action) { const b = el("button", value, "desk-linklike"); b.type = "button"; b.addEventListener("click", action); li.append(b); }
    else li.append(el("b", value));
    status.append(li);
  }

  function todayState(queue) {
    if (noSendDeployment) return { code: "NO_SEND_DEPLOYMENT" };
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
      NO_SEND_DEPLOYMENT: ["NO_SEND · 本部署禁止外部请求", "禁止新的模型研究、SEC读取及行情与报道刷新。可浏览已保存研究、历史快照和假设 What-if；历史内容不代表最新持仓或行情。", []],
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
    const code = el("span", { HAS_EVENTS: "有新变化", SCANNED_NO_QUALIFYING_EVENTS: "已扫描 · 无优先变化", NOT_SCANNED: "未扫描", PROVIDER_ERROR: "来源不可用", STALE_SCAN: "扫描已过时", SYNTHETIC_REPLAY: "合成示例", UNAVAILABLE: "暂时无法读取", NO_SEND_DEPLOYMENT: "部署禁止发送" }[state.code] || "状态未知", "today-state-code");
    code.dataset.code = state.code;
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

  function leadDetail(row, question) {
    const item = row.item;
    return { question, attention_item_id: item.item_id, symbol: item.symbol,
      lead: { headline: item.screen?.brief?.headline?.text || item.screen?.rationale?.text || item.what_changed?.text, next_check: item.screen?.brief?.next_check?.text || null,
        event_key: `${row.research_event?.identity_basis || "SINGLE_RECEIPT"} · 扫描 ${item.scan_id}`,
        evidence: (row.research_event?.evidence || item.packet?.evidence || []).map(e => ({ evidence_id: e.evidence_id, provider: e.provider, title: e.title })) } };
  }
  const handOff = detail => window.dispatchEvent(new CustomEvent("mia-ask-draft", { detail }));
  // Leads next to the question: the top items hand off in one tap at any width.
  function renderLeadStrip(queue, state) {
    $("desk-lead-strip")?.remove();
    const form = $("desk-question-form");
    const rows = (queue?.items || []).filter(r => r.item?.item_id).slice(0, 3);
    if (!form || !rows.length || noSendDeployment || $("desk-question")?.readOnly || publicDemo) return;
    const strip = el("div", null, "desk-lead-strip"); strip.id = "desk-lead-strip"; strip.setAttribute("role", "group"); strip.setAttribute("aria-label", "今日线索 · 带入研究问题");
    strip.append(el("p", `今日线索 · ${state.items} 项 · 选一条带入`, "desk-lead-strip-title"));
    for (const row of rows) {
      const headline = row.item.screen?.brief?.headline?.text || row.item.screen?.rationale?.text || row.item.what_changed?.text || "新的公开材料";
      const b = button("", () => handOff(leadDetail(row, `${row.item.symbol}：「${headline}」会改变我的研究判断吗？`)));
      b.append(el("b", row.item.symbol), el("span", headline));
      b.setAttribute("aria-label", `带入 ${row.item.symbol}：${headline}`);
      strip.append(b);
    }
    form.before(strip);
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
    const canAsk = !noSendDeployment && !$("desk-question")?.readOnly;
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
      const row0 = change && (queue?.items || []).find(r => r.item?.item_id === change.item_id);
      if (canAsk) actions.append(button("研究", () => row0 ? handOff(leadDetail(row0, question)) : ask(position.symbol, question), kind === "CHANGE" || kind === "NEVER"));
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

  function renderReadiness(profiles, afterProviders = false) {
    const help = $("desk-mode-help"); if (!help || publicDemo) return;
    $("ask-readiness")?.remove();
    const box = el("div", null, "ask-readiness"); box.id = "ask-readiness"; box.setAttribute("role", "status");
    const ready = profiles && Object.values(profiles.preview_mode_availability || {}).some(Boolean);
    box.dataset.ready = String(Boolean(ready));
    if (noSendDeployment) {
      box.dataset.ready = "false";
      box.append(el("b", "本部署禁止新的模型研究。"), el("span", " 已保存研究、历史快照和假设 What-if 可继续使用；更换密钥或重试不会解除部署限制。"));
      help.after(box);
      return false;
    } else if (ready) {
      box.append(el("b", "可以开始研究。"), el("span", " 提交前会确认账户范围、模型与费用上限；Fast 结果可继续 Deep，同一任务保留每一轮。"));
      // Became ready after the providers dialog closed: research-desk read the old state at load.
      if (afterProviders && $("desk-submit")?.disabled) {
        box.append(el("p", "模型刚刚通过验证；重新载入后研究按钮才会启用。", "small"), button("重新载入", () => location.reload()));
      }
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

  function enforceNoSendControls() {
    if (!noSendDeployment) return;
    for (const id of ["attention-scan", "attention-continue", "desk-submit", "desk-continue", "desk-retry", "today-refresh"]) {
      const control = $(id); if (control) { if (!control.disabled) control.disabled = true; control.title = "NO_SEND：本部署禁止新的外部请求"; }
    }
    for (const id of ["desk-question", "desk-followup"]) { const control = $(id); if (control && !control.readOnly) control.readOnly = true; }
    document.querySelectorAll("[data-research-depth]").forEach(control => { if (!control.disabled) control.disabled = true; });
    document.querySelectorAll(".desk-failure-kind button").forEach(control => { if (control.textContent.includes("模型与数据源")) control.hidden = true; });
    const providers = $("providers-dialog");
    if (providers && !$("no-send-provider-note")) { const note = el("p", "NO_SEND：本部署禁止外部请求；密钥录入与验证已停用。可继续查看已保存的来源与模型状态。", "small"); note.id = "no-send-provider-note"; providers.querySelector(".providers-head")?.after(note); }
    providers?.querySelectorAll(".provider-key input, .provider-key button, button").forEach(control => {
      if (control.matches(".provider-key input, .provider-key button") || ["添加", "更换", "验证授权"].includes(control.textContent.trim())) { if (!control.disabled) control.disabled = true; }
    });
    const help = $("desk-mode-help"), text = "NO_SEND：本部署禁止新的模型研究；可浏览已保存研究、历史快照与假设 What-if。";
    if (help && help.textContent !== text) help.textContent = text;
  }

  async function load(afterProviders = false) {
    const [queue, overview, tasks, profiles, observations, identity] = await Promise.all([
      optional("/api/attention/queue"), optional("/api/console/portfolio-overview"),
      optional("/api/research/tasks"), publicDemo ? null : optional("/api/research/model-profiles"),
      runtime === "OWNER" && document.getElementById("owner-value") ? optional("/api/owner/market-observations") : null,
      publicDemo ? null : optional("/api/workbench/identity")]);
    const sourceHealth = publicDemo ? null : await optional("/api/provider-source-health");
    noSendDeployment = noSendDeployment || identity?.deployment_mode === "NO_SEND_DEPLOYMENT";
    if (noSendDeployment) {
      body.dataset.deploymentMode = "NO_SEND_DEPLOYMENT";
      enforceNoSendControls();
      window.dispatchEvent(new CustomEvent("mia-no-send-deployment"));
    }
    const state = todayState(queue);
    today.replaceChildren(renderState(state));
    const priority = renderPriority(overview, tasks, queue, observations);
    if (priority) today.append(priority);
    const count = $("desk-lead-count"); if (count) count.textContent = state.code === "HAS_EVENTS" || state.code === "SYNTHETIC_REPLAY" ? `${state.items} 项` : { NOT_SCANNED: "未扫描", PROVIDER_ERROR: "来源不可用", STALE_SCAN: "已过时", SCANNED_NO_QUALIFYING_EVENTS: "0 项", NO_SEND_DEPLOYMENT: "已禁用", UNAVAILABLE: "无法读取" }[state.code] || "";
    const ready = renderReadiness(profiles, afterProviders);
    renderLeadStrip(queue, state);

    status.replaceChildren();
    chip("数据", publicDemo ? "公开合成示例" : runtime === "OWNER" ? "本机组合" : "本地", publicDemo ? "warn" : "ok", showDiagnostics);
    if (overview?.portfolio_as_of) chip("持仓快照", `${date(overview.portfolio_as_of)} · ${publicDemo ? "合成示例" : "HISTORICAL · 当前未确认"}`, publicDemo ? "neutral" : "warn");
    const scanTone = { HAS_EVENTS: "ok", SCANNED_NO_QUALIFYING_EVENTS: "ok", SYNTHETIC_REPLAY: "neutral" }[state.code] || "warn";
    chip("新闻扫描", { NO_SEND_DEPLOYMENT: "部署已禁止", NOT_SCANNED: "未扫描", PROVIDER_ERROR: "来源不可用", STALE_SCAN: ago(state.at), SCANNED_NO_QUALIFYING_EVENTS: stamp(state.at), HAS_EVENTS: stamp(state.at), SYNTHETIC_REPLAY: "冻结回放", UNAVAILABLE: "无法读取" }[state.code], scanTone);
    const missing = overview?.missing_price_symbol_count;
    if (overview) chip("价格", missing ? `${missing} 个缺价 · 市值未知` : overview.valuation_status === "complete" ? `快照 ${day(overview.market_cutoff)}` : "未计价", missing ? "warn" : "neutral", showDiagnostics);
    if (!publicDemo) chip("研究模型", noSendDeployment ? "本部署禁止" : ready ? "可用 · 见上方各深度" : "未就绪", ready ? "ok" : "warn", ready || noSendDeployment ? null : openProviders);
    const dataRows = (sourceHealth?.providers || []).filter(row => ["Finnhub", "Massive", "SEC", "Longbridge"].includes(row.provider) && row.configured);
    if (dataRows.length) {
      const label = row => row.states.includes("RIGHTS_BLOCKED") ? "权限阻断" : row.states.includes("RESTART_REQUIRED") ? "需重启" : row.source_client_enabled ? "合格" : "未启用";
      chip("数据 Provider", dataRows.map(row => `${row.provider} ${label(row)}`).join(" · "), dataRows.every(row => label(row) === "合格") ? "ok" : "warn", openProviders);
    } else if (sourceHealth) chip("数据 Provider", "未配置 · 只用已保存资料", "warn", openProviders);
    if (!publicDemo) {
      const blockers = new Set(Object.values(profiles?.dispatch_blockers || {}));
      chip("费用", noSendDeployment ? "不会产生" : blockers.has("PRICE_CARD_EXPIRED") ? "模型价格资格已过期" : "每次提交前确认上限 · 取消不发送", blockers.has("PRICE_CARD_EXPIRED") ? "warn" : "neutral");
    }
  }

  document.addEventListener("submit", event => {
    if (noSendDeployment && ["desk-question-form", "desk-followup-form"].includes(event.target.id)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("click", event => {
    const control = event.target.closest("button");
    if (noSendDeployment && ["attention-scan", "attention-continue", "desk-submit", "desk-continue", "desk-retry"].includes(control?.id)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  new MutationObserver(enforceNoSendControls).observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
  if ($("providers-dialog")) new MutationObserver(enforceNoSendControls).observe($("providers-dialog"), { childList: true, subtree: true });

  // Market pulse is useful only when configured; keep it under Today/Ask either way.
  const grid = document.querySelector(".desk-entry-grid");
  const pulse = $("today-pulse");
  if (grid && pulse) grid.after(pulse);
  load().catch(() => { today.replaceChildren(renderState({ code: "UNAVAILABLE" })); });
  // After the user verifies a key or source in the existing dialog, re-read readiness.
  $("providers-dialog")?.addEventListener("close", () => void load(true).catch(() => {}));
  // Re-read after the existing scan control repaints the queue (scan, continue, review).
  let timer = null;
  const queueNode = $("attention-queue");
  if (queueNode) new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(() => void load().catch(() => {}), 600); })
    .observe(queueNode, { childList: true });
})();
