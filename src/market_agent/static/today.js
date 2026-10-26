"use strict";
// Today, first screen: live Market Pulse, scope tabs and the latest grouped
// headlines for holdings / watch list / market. Data-provider snapshot only:
// nothing here calls a model, writes the database or claims importance.
// Model-screened Attention cards (attention.js) stay below as a second layer.
(() => {
  const $ = id => document.getElementById(id);
  const runtime = document.body.dataset.runtimeMode;
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const button = (text, className) => { const b = el("button", text, className); b.type = "button"; return b; };
  const pct = v => v == null ? "—" : `${Number(v) > 0 ? "+" : ""}${v}%`;
  const direction = v => v == null ? "unknown" : Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "flat";
  const price = v => v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const clock = iso => new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const ago = iso => {
    const minutes = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    return hours < 24 ? `${hours} 小时前` : `${Math.round(hours / 24)} 天前`;
  };
  const state = { data: null, scope: "ALL", symbol: null };
  const SCOPES = [["ALL", "全部"], ["HOLDING", "持仓"], ["WATCHING", "关注"], ["MARKET", "市场"]];

  const attention = $("attention");
  const heading = attention.querySelector(".section-heading");
  const pulse = el("section", null, "today-pulse"); pulse.id = "today-pulse"; pulse.setAttribute("aria-label", "市场脉搏");
  const tabs = el("div", null, "today-scope"); tabs.id = "today-scope"; tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "关注范围");
  const filter = el("p", null, "today-filter"); filter.hidden = true;
  const headlines = el("section", null, "today-headlines"); headlines.id = "today-headlines"; headlines.setAttribute("aria-live", "polite");
  const screened = el("h3", "模型已筛选事项", "today-layer-title"); screened.id = "today-screened-title";
  // Pulse spans the page above Today and Ask; scope tabs and headlines live in Today.
  document.querySelector(".desk-entry-grid").before(pulse);
  heading.after(tabs, filter, headlines, screened);
  // Backlog counts are secondary: keep them, but inside the coverage disclosure.
  const audit = $("attention-audit");
  audit.querySelector("summary").after($("attention-summary"), $("attention-continue").parentElement);
  // The paid model scan is a deliberate second action, not the page's first CTA.
  $("attention-scan").classList.remove("primary");
  heading.querySelector("h2").after(el("span", "持仓 · 关注 · 市场的最新变化", "today-subtitle"));

  function rangeText(r) {
    if (!r || !["AVAILABLE", "PARTIAL"].includes(r.status)) return "";
    const parts = [];
    if (r.change_5d_pct != null) parts.push(`5 日 ${Number(r.change_5d_pct) > 0 ? "+" : ""}${r.change_5d_pct}%`);
    if (r.change_20d_pct != null) parts.push(`20 日 ${Number(r.change_20d_pct) > 0 ? "+" : ""}${r.change_20d_pct}%`);
    if (r.realized_vol_20d_pct != null) parts.push(`波动 ${r.realized_vol_20d_pct}%`);
    return parts.join(" · ");
  }

  function quoteTile(q, compact = false) {
    const known = q.status === "AVAILABLE" || q.status === "STALE";
    const tile = el(compact ? "button" : "div", null, `today-tile today-${direction(q.change_pct)}`);
    if (compact) tile.type = "button";
    tile.append(el("span", q.label || q.symbol, "today-tile-label"),
      el("strong", known ? price(q.price) : "未知", "today-tile-price"),
      el("span", known ? pct(q.change_pct) : q.status === "NOT_ENTITLED" ? "当前计划不含此标的" : q.status === "STALE" ? "过期报价" : "暂无报价", "today-tile-change"));
    const range = rangeText(q.range);
    if (range) tile.append(el("span", range, "today-range"));
    // Every tile states its own provenance instead of borrowing the page stamp.
    const bits = [`${q.symbol} · ${q.instrument_kind || "未知类型"}`];
    if (q.proxy_of) bits.push(`跟踪 ${q.proxy_of}，不是指数点位`);
    if (q.as_of) bits.push(`报价 ${clock(q.as_of)} · 滞后 ${Math.round((q.observed_lag_seconds || 0) / 60)} 分钟`);
    if (q.delay_status) bits.push(q.delay_status);
    if (q.entitlement) bits.push(q.entitlement);
    if (q.unit && q.unit !== "USD") bits.push(`单位 ${q.unit}`);
    tile.title = bits.join(" · ");
    return tile;
  }

  function bars(rows, onPick) {
    // Diverging bars scaled to the largest absolute move; unknown stays unknown.
    const known = rows.filter(r => r.change_pct != null);
    const scale = Math.max(0.5, ...known.map(r => Math.abs(Number(r.change_pct))));
    const list = el("ol", null, "today-bars");
    [...known].sort((a, b) => Number(b.change_pct) - Number(a.change_pct)).concat(rows.filter(r => r.change_pct == null)).forEach(r => {
      const item = el("li", null, `today-bar today-${direction(r.change_pct)}`);
      const name = onPick ? button(r.label ? `${r.label}` : r.symbol, "today-bar-name") : el("span", r.label || r.symbol, "today-bar-name");
      if (onPick) { name.addEventListener("click", () => onPick(r.symbol)); name.setAttribute("aria-pressed", String(state.symbol === r.symbol)); }
      const track = el("span", null, "today-bar-track"), fill = el("span", null, "today-bar-fill");
      if (r.change_pct != null) { fill.style.width = `${Math.min(50, Math.abs(Number(r.change_pct)) / scale * 50)}%`; }
      track.append(fill);
      item.append(name, track, el("span", r.change_pct != null ? pct(r.change_pct) : "未知", "today-bar-value"));
      if (r.cost_exposure_pct != null) item.append(el("span", `成本 ${Number(r.cost_exposure_pct).toFixed(1)}%`, "today-bar-meta"));
      if (onPick && runtime === "OWNER") { const sim = button("预演", "today-bar-action"); sim.title = `${r.symbol} · 单账户调仓预演（不下单）`; sim.addEventListener("click", () => openWhatIf(r.symbol)); item.append(sim); }
      list.append(item);
    });
    return list;
  }

  const SESSION_LABEL = { PRE_MARKET: "盘前", REGULAR: "盘中", AFTER_HOURS: "盘后",
    CLOSED_OVERNIGHT: "已收盘", CLOSED_WEEKEND: "周末休市" };

  function refreshLine(data) {
    const state = data.refresh_state || {};
    if (!state.last_attempt_at) return "尚未刷新";
    const result = { OK: "全部标的都已答复", PARTIAL: "部分标的未知", FAILED: "本次刷新未取得任何答复" }[state.last_result] || "结果未知";
    const parts = [`上次刷新 ${clock(state.last_attempt_at)} · ${result}`];
    if (state.last_success_at && state.last_success_at !== state.last_attempt_at) parts.push(`上次成功 ${clock(state.last_success_at)}`);
    if (state.last_uncovered?.length) parts.push(`未覆盖：${state.last_uncovered.join(" / ")}`);
    if (state.last_duration_ms != null) parts.push(`${state.last_duration_ms} ms`);
    return parts.join(" · ");
  }

  function renderPulse(data) {
    pulse.replaceChildren();
    const head = el("div", null, "today-pulse-head");
    const refresh = button(state.loading ? "取消刷新" : "刷新行情与报道", state.loading ? "" : "primary");
    refresh.id = "today-refresh";
    refresh.addEventListener("click", () => state.loading ? cancelLoad() : load(true));
    const session = SESSION_LABEL[data.market_session] || "时段未知";
    const stamp = el("span", data.status === "AVAILABLE"
      ? `美股${session} · ${clock(data.generated_at)} · Finnhub · 未保存、未调用模型`
      : "", "today-stamp");
    head.append(el("h3", "市场脉搏"), stamp, refresh);
    pulse.append(head);
    if (data.status !== "AVAILABLE") {
      const text = data.status === "NOT_CONFIGURED" ? "尚未配置 Finnhub 数据源，无法获取今日行情与报道。可在「模型与数据源」中添加。" :
        data.status === "OFFLINE_REPLAY" ? "公开演示使用离线合成数据，不获取实时行情。" : "行情与报道暂不可用。";
      pulse.append(el("p", text, "today-empty"));
      if (data.status === "NOT_CONFIGURED") { const go = button("配置数据源"); go.addEventListener("click", () => window.dispatchEvent(new CustomEvent("mia-open-providers"))); pulse.append(go); }
      refresh.hidden = data.status !== "NOT_CONFIGURED" ? true : refresh.hidden;
      return;
    }
    const indices = el("div", null, "today-tiles");
    data.indices.forEach(q => indices.append(quoteTile(q)));
    const macro = el("div", null, "today-tiles today-macro");
    (data.macro || []).forEach(q => macro.append(quoteTile(q)));
    const grid = el("div", null, "today-pulse-grid");
    const watched = new Set((data.theme_scope || {}).watched_sectors || []);
    const sectors = el("div", null, "today-pulse-col");
    sectors.append(el("h4", watched.size ? `行业 ETF · 今日涨跌（先看我的 ${watched.size} 个行业）` : "行业 ETF · 今日涨跌"));
    sectors.append(bars([...data.sectors].sort((a, b) => Number(watched.has(b.symbol)) - Number(watched.has(a.symbol)))));
    if (!watched.size && data.theme_scope?.how_to_add) sectors.append(el("p", data.theme_scope.how_to_add, "small muted"));
    const holdings = el("div", null, "today-pulse-col");
    holdings.append(el("h4", state.allHoldings ? `我的持仓 · 今日涨跌（${data.holdings.length}）` : "我的持仓 · 今日波动最大"));
    const movers = state.allHoldings ? data.holdings : [...data.holdings].sort((a, b) => Math.abs(Number(b.change_pct ?? 0)) - Math.abs(Number(a.change_pct ?? 0))).slice(0, 8);
    holdings.append(data.holdings.length ? bars(movers, pick) : el("p", "尚无持仓快照。", "small muted"));
    if (data.holdings.length > 8) { const all = button(state.allHoldings ? "只看波动最大的 8 个" : `显示全部 ${data.holdings.length} 个持仓`, "today-more"); all.addEventListener("click", () => { state.allHoldings = !state.allHoldings; renderPulse(state.data); }); holdings.append(all); }
    grid.append(holdings, sectors);
    pulse.append(indices);
    if (macro.children.length) pulse.append(macro);
    pulse.append(grid);
    // What could not be answered is part of the picture, not a footnote.
    const gaps = (data.gaps || []).filter(g => g.reason !== "NO_PRICE");
    if (gaps.length) {
      const line = el("p", null, "today-gaps");
      line.append(el("strong", `缺口 ${gaps.length} 项：`));
      line.append(el("span", gaps.slice(0, 8).map(g => `${g.key}（${g.detail}）`).join("、") + (gaps.length > 8 ? ` 等 ${gaps.length} 项` : "")));
      pulse.append(line);
    }
    if (state.notice) pulse.append(el("p", state.notice, "today-warning"));
    pulse.append(el("p", refreshLine(data), "today-refresh-state"));
    pulse.append(el("p", "报价为数据源最新价，可能延迟；ETF 代理不等于指数点位；缺失值保持未知，不写 0。", "small muted"));
  }

  function counts(data) {
    const scopes = data.scopes || {};
    return { ALL: ["HOLDING", "WATCHING", "MARKET"].reduce((n, k) => n + (scopes[k]?.groups.length || 0), 0),
      HOLDING: scopes.HOLDING?.groups.length || 0, WATCHING: scopes.WATCHING?.groups.length || 0, MARKET: scopes.MARKET?.groups.length || 0 };
  }

  function renderTabs() {
    tabs.replaceChildren();
    const n = state.data ? counts(state.data) : {};
    SCOPES.forEach(([key, label]) => {
      const tab = button(n[key] != null ? `${label} ${n[key]}` : label, "today-tab");
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(state.scope === key)); tab.dataset.scope = key;
      tab.addEventListener("click", () => { state.scope = key; if (key === "MARKET") state.symbol = null; render(); });
      tabs.append(tab);
    });
    filter.hidden = !state.symbol;
    filter.replaceChildren(el("span", `只看 ${state.symbol || ""}`), (() => { const clear = button("显示全部", "today-clear"); clear.addEventListener("click", () => { state.symbol = null; render(); }); return clear; })());
    // The model-screened sections follow the same scope and symbol filter.
    document.querySelectorAll("#attention-queue .attention-section[data-scope]").forEach(section => {
      const scope = section.dataset.scope === "CANDIDATE" ? "WATCHING" : section.dataset.scope;
      section.hidden = !["ALL", scope].includes(state.scope) || state.scope === "MARKET";
    });
    document.querySelectorAll("#attention-queue [data-symbol]").forEach(card => { card.hidden = Boolean(state.symbol && card.dataset.symbol !== state.symbol); });
  }

  function ask(group) {
    const symbol = group.symbol;
    const subject = symbol ? `${symbol}：` : "市场：";
    const question = symbol
      ? `${subject}「${group.title}」（${group.source}，${clock(group.published_at)}）这件事会改变我对 ${symbol} 的持仓判断吗？最强的反证是什么？`
      : `${subject}「${group.title}」（${group.source}，${clock(group.published_at)}）会影响我的组合吗？`;
    window.dispatchEvent(new CustomEvent("mia-ask-draft", { detail: { question } }));
  }

  function headlineCard(group) {
    const card = el("article", null, "today-headline");
    const fresh = Date.now() - Date.parse(group.published_at) < 24 * 3600 * 1000;
    const meta = el("p", null, "today-headline-meta");
    if (group.symbol) meta.append(el("span", group.symbol, "today-symbol"));
    meta.append(el("span", fresh ? "24 小时内" : "1–2 天前", fresh ? "today-chip today-chip-fresh" : "today-chip"),
      el("span", `${group.source || "未知来源"} · ${ago(group.published_at)}`, "today-headline-source"));
    if (group.identity?.digest) meta.append(el("span", `线索 ${group.identity.digest.slice(0, 8)}`, "today-chip today-chip-digest"));
    if (group.reports > 1) meta.append(el("span", `${group.reports} 篇报道`, "today-chip today-chip-reports"));
    const title = el("h4", group.title);
    card.append(meta, title);
    if (group.summary) card.append(el("p", group.summary, "today-headline-summary"));
    const actions = el("div", null, "today-headline-actions");
    if (runtime === "OWNER") { const research = button("研究此事", "primary"); research.addEventListener("click", () => ask(group)); actions.append(research); }
    if (group.url) { const link = el("a", "原文"); link.href = group.url; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    if (group.reposts?.length) {
      const more = el("details", null, "today-reposts");
      more.append(el("summary", `同一报道的其他 ${group.reports - 1} 篇`));
      const list = el("ul");
      group.reposts.forEach(r => { const li = el("li"); li.append(el("span", `${r.source} · ${clock(r.published_at)} · `), r.url ? Object.assign(el("a", r.title), { href: r.url, target: "_blank", rel: "noopener noreferrer" }) : el("span", r.title)); list.append(li); });
      more.append(list, el("p", "按标题与时间相近合并，仅用于去重显示；不代表事实已被多方证实。", "small muted"));
      actions.append(more);
    }
    card.append(actions);
    return card;
  }

  function renderHeadlines() {
    headlines.replaceChildren();
    const data = state.data;
    if (!data || data.status !== "AVAILABLE") return;
    const keys = state.scope === "ALL" ? ["HOLDING", "WATCHING", "MARKET"] : [state.scope];
    const label = { HOLDING: "持仓", WATCHING: "关注", MARKET: "市场要闻" };
    let shown = 0;
    keys.forEach(key => {
      const scope = data.scopes[key];
      if (!scope) return;
      let groups = scope.groups.filter(g => !state.symbol || g.symbol === state.symbol);
      if (state.scope === "ALL") groups = groups.slice(0, key === "MARKET" ? 3 : 6);
      const block = el("section", null, "today-headline-block"); block.dataset.scope = key;
      const title = el("div", null, "today-headline-head");
      title.append(el("h3", label[key]),
        el("span", "未经模型筛选的线索", "today-unscreened"),
        el("span", key === "MARKET" ? "Finnhub 综合新闻" : `${scope.symbols.length} 个标的 · 过去 48 小时`, "small muted"));
      block.append(title);
      if (scope.not_checked_symbols?.length) block.append(el("p", `按成本权重检查前 ${scope.symbols.length} 个；未检查：${scope.not_checked_symbols.join(" / ")}（可点上方持仓单独查看行情）。`, "small muted"));
      // A refusal and a failure are different facts. Only one of them permits the
      // sentence "no news", so the sentence is chosen from coverage_status, not from
      // an empty list.
      const refused = scope.not_entitled_symbols || [];
      const failed = scope.failed_symbols || [];
      if (refused.length) block.append(el("p", `无新闻权限：${refused.join(" / ")}（当前订阅不含；不代表没有报道）`, "small today-warning"));
      if (failed.length) block.append(el("p", `暂时不可用：${failed.join(" / ")}（网络或限流；不代表没有报道）`, "small today-warning"));
      if (!groups.length) {
        const who = key === "MARKET" ? "综合新闻源" : (refused.length ? refused.join(" / ") : failed.join(" / "));
        const sentence = scope.coverage_status === "REFUSED" ? `${who} 被数据源拒绝：当前订阅不含该权限。这不代表没有报道，只代表我们读不到。`
          : scope.coverage_status === "UNKNOWN" ? `${who} 暂时不可用，无法确认是否有报道。`
          : state.symbol ? `${state.symbol} 过去 48 小时没有新报道。`
          : key === "WATCHING" && !scope.symbols.length ? "还没有关注标的；在「研究范围」中添加。"
          : "过去 48 小时没有新报道。";
        block.append(el("p", sentence, "small muted"));
      }
      groups.forEach(group => block.append(headlineCard(group)));
      shown += groups.length;
      if (state.scope === "ALL" && scope.groups.length > groups.length) { const all = button(`查看全部 ${scope.groups.length} 条${label[key]}报道`, "today-more"); all.addEventListener("click", () => { state.scope = key; render(); }); block.append(all); }
      headlines.append(block);
    });
    headlines.dataset.shown = String(shown);
    headlines.append(el("p", "以上是数据源原始线索，未经过模型筛选，也没有重要性与真伪判断；相近标题合并只为减少转载干扰。下方「模型已筛选事项」才是通过筛选、可进入深入研究的证据事件。", "small muted today-footnote"));
  }

  function pick(symbol) {
    state.symbol = state.symbol === symbol ? null : symbol;
    if (state.symbol && state.scope === "MARKET") state.scope = "ALL";
    render();
    if (state.symbol) { const draft = $("desk-question"); if (draft && !draft.value.trim()) draft.placeholder = `例如：${symbol} 今天的变化会影响我的持仓逻辑吗？`; }
  }

  function render() { if (state.data) renderPulse(state.data); renderTabs(); renderHeadlines(); }

  let inflight = null;
  const REFRESH_TIMEOUT_MS = 20000;

  async function load(refresh = false) {
    if (inflight) inflight.abort();
    const controller = new AbortController();
    inflight = controller;
    state.loading = true;
    state.notice = null;
    if (state.data) renderPulse(state.data);
    else { pulse.replaceChildren(el("p", "正在获取今日行情与报道（数据源，不调用模型）…", "today-empty")); }
    headlines.setAttribute("aria-busy", "true");
    const timer = setTimeout(() => controller.abort(), REFRESH_TIMEOUT_MS);
    try {
      const response = await fetch(`/api/today/pulse${refresh ? "?refresh=1" : ""}`,
        { headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok) throw new Error(String(response.status));
      state.data = await response.json();
    } catch (error) {
      if (controller.signal.aborted) {
        // A cancelled or timed-out refresh keeps the last snapshot visible and says
        // it is old, instead of replacing the page with an error or an empty state.
        state.notice = state.data
          ? "本次刷新已取消或超时；下面显示的是上一次结果，不代表当前行情。"
          : "本次刷新已取消或超时，尚未取得任何行情。";
      } else {
        state.data = { status: runtime === "PUBLIC_DEMO" ? "OFFLINE_REPLAY" : "UNAVAILABLE", scopes: {} };
        state.notice = null;
      }
    } finally {
      clearTimeout(timer);
      if (inflight === controller) inflight = null;
      state.loading = false;
    }
    headlines.removeAttribute("aria-busy");
    render();
  }

  function cancelLoad() { if (inflight) inflight.abort(); }

  // Attention cards render asynchronously; re-apply the scope filter when they land.
  new MutationObserver(() => renderTabs()).observe($("attention-queue"), { childList: true });

  // ---------- Portfolio What-if (no saved Action required) ----------
  const dialog = el("dialog", null, "today-whatif"); dialog.id = "today-whatif"; dialog.setAttribute("aria-labelledby", "today-whatif-title");
  document.body.append(dialog);
  async function openWhatIf(symbol) {
    dialog.replaceChildren(el("h2", `调仓预演 · ${symbol}`), el("p", "正在读取当前组合与最新报价…", "small muted"));
    dialog.querySelector("h2").id = "today-whatif-title";
    dialog.showModal();
    let context;
    try {
      const response = await fetch(`/api/portfolio/what-if/context?symbol=${encodeURIComponent(symbol)}`);
      if (!response.ok) throw new Error((await response.json()).detail || response.status);
      context = await response.json();
    } catch (error) { dialog.append(el("p", "没有可用的组合快照，先在「我的组合」导入持仓。", "desk-error"), closer()); return; }
    renderWhatIf(symbol, context);
  }
  function closer() { const close = button("关闭"); close.addEventListener("click", () => dialog.close()); return close; }
  function renderWhatIf(symbol, context) {
    const held = a => a.positions.find(p => p.symbol === symbol)?.shares;
    const holders = context.accounts.filter(held);
    const form = el("form", null, "today-whatif-form");
    const field = (label, input) => { const wrap = el("label", label); wrap.append(input); form.append(wrap); return input; };
    const text = (value = "") => { const input = el("input"); input.type = "text"; input.inputMode = "decimal"; input.value = value; return input; };
    const account = field("账户", el("select"));
    context.accounts.forEach(a => { const option = el("option", `${a.name} · ${held(a) ? `持有 ${held(a)} 股` : `未持有 ${symbol}`} · 现金 ${price(a.cash)}`); option.value = a.account_id; account.append(option); });
    if (holders.length) account.value = holders[0].account_id;
    const side = field("方向", el("select"));
    [["BUY", "加仓"], ["SELL", "减仓"]].forEach(([v, t]) => { const o = el("option", t); o.value = v; side.append(o); });
    const shares = field("股数", text()); shares.required = true; shares.placeholder = "例如 10";
    const suggested = context.suggested_prices[symbol];
    const fill = field(`假设成交价 · USD/股`, text(suggested || "")); fill.required = true;
    const basis = context.quote_basis[symbol];
    form.append(el("p", suggested ? `已预填 Finnhub 最新报价 ${clock(basis.as_of)}（可能延迟），可修改。` : "暂无报价，请输入假设价格。", "small muted"));
    const advanced = el("details", null, "today-whatif-advanced"); advanced.append(el("summary", "高级：费用、日期与其他持仓计价"));
    const fee = text("0"), day = el("input"); day.type = "date"; day.value = context.scenario_date; day.min = context.snapshot_as_of.slice(0, 10);
    const feeLabel = el("label", "交易费用 · USD"); feeLabel.append(fee);
    const dayLabel = el("label", "情景日期"); dayLabel.append(day);
    advanced.append(feeLabel, dayLabel);
    const marks = new Map();
    const renderMarks = () => {
      marks.clear(); advanced.querySelectorAll(".today-mark").forEach(n => n.remove());
      const current = context.accounts.find(a => a.account_id === account.value);
      (current?.positions || []).filter(p => p.symbol !== symbol).forEach(p => {
        const wrap = el("label", `${p.symbol} · ${p.shares} 股 · 计价 USD/股`, "today-mark"), input = text(context.suggested_prices[p.symbol] || "");
        wrap.append(input); advanced.append(wrap); marks.set(p.symbol, input);
      });
    };
    account.addEventListener("change", renderMarks); renderMarks();
    form.append(advanced);
    const attest = el("input"); attest.type = "checkbox"; attest.required = true;
    const attestLabel = el("label", null, "today-attest"); attestLabel.append(attest, el("span", `确认快照后 ${symbol} 没有拆股或股本类别变化`));
    form.append(attestLabel);
    const result = el("section", null, "today-whatif-result"); result.setAttribute("aria-live", "polite");
    const actions = el("div", null, "today-whatif-actions"); const submit = el("button", "计算预演", "primary"); submit.type = "submit"; actions.append(closer(), submit);
    form.append(actions);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const other = {}; marks.forEach((input, key) => { if (input.value.trim()) other[key] = input.value.trim(); });
      const payload = { revision: 1, account_id: account.value, symbol, side: side.value, shares: shares.value.trim(), fill_price: fill.value.trim(),
        fee: fee.value.trim() || "0", scenario_date: day.value, unchanged_share_basis_attested: attest.checked, other_prices: other };
      result.replaceChildren(el("p", "正在计算…", "small muted"));
      try {
        const response = await fetch("/api/portfolio/what-if", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const value = await response.json();
        if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "INVALID_REQUEST");
        showResult(result, value);
      } catch (error) { result.replaceChildren(el("p", whatIfError(error.message), "desk-error")); }
    });
    dialog.replaceChildren(el("h2", `调仓预演 · ${symbol}`), el("p", `基于 ${clock(context.snapshot_as_of)} 的组合快照；只做计算，不下单，不改持仓。`, "small muted"), form, result);
    dialog.querySelector("h2").id = "today-whatif-title";
  }
  function whatIfError(code) {
    return ({ INSUFFICIENT_CASH_INCLUDING_FEES: "现金不足（含费用）。", OVERSELL_NOT_ALLOWED: "减仓股数超过该账户持有股数。",
      SHARE_BASIS_CONTINUITY_ATTESTATION_REQUIRED: "请先确认没有拆股或股本变化。", POSITIVE_SHARES_AND_PRICES_REQUIRED: "股数和价格必须为正数。",
      SCENARIO_PREDATES_SNAPSHOT: "情景日期不能早于组合快照。", INVALID_REQUEST: "请用普通小数填写股数和价格。" })[code] || "无法计算这次预演。";
  }
  function showResult(container, value) {
    const weight = v => v == null ? "未知" : `${(Number(v) * 100).toFixed(2)}%`;
    const shares = v => v == null ? "—" : String(Number(v));
    const rows = [["股数", "shares", shares], ["现金 USD", "cash", price], ["持仓市值 USD", "focus_market_value", price], ["占该账户", "focus_weight", weight], ["账户最大单一持仓", "largest_weight", weight]];
    const list = el("dl", null, "today-whatif-deltas");
    rows.forEach(([label, key, format]) => { const cell = el("div"); cell.append(el("dt", label), el("dd", `${format(value.before[key])} → ${format(value.after[key])}`)); list.append(cell); });
    container.replaceChildren(list);
    if (value.after.missing_price_symbols?.length) container.append(el("p", `缺少 ${value.after.missing_price_symbols.join(" / ")} 的计价，组合权重保持未知；可在高级设置中填写。`, "small today-warning"));
    if (value.modeled_realized_pnl != null) container.append(el("p", `按平均成本估算的已实现盈亏 ${price(value.modeled_realized_pnl)} USD（非税务口径）。`, "small"));
    container.append(el("p", "假设性计算 · 未下单 · 未保存", "small muted"));
  }
  window.miaOpenWhatIf = openWhatIf;

  // ---------- Research route preview (before any cost) ----------
  const question = $("desk-question");
  const route = el("div", null, "today-route"); route.id = "desk-route"; route.hidden = true; route.setAttribute("aria-live", "polite");
  question.after(route);
  let timer = null, generation = 0;
  async function previewRoute() {
    const text = question.value.trim(), mine = ++generation;
    if (runtime !== "OWNER" || text.length < 4) { route.hidden = true; return; }
    try {
      const response = await fetch("/api/research/route-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: text }) });
      if (!response.ok || mine !== generation) return;
      renderRoute(await response.json());
    } catch (_) { route.hidden = true; }
  }
  function renderRoute(value) {
    const chips = el("div", null, "today-route-chips");
    const chip = (label, text, tone = "") => { const c = el("span", null, `today-route-chip ${tone}`); c.append(el("b", label), el("span", text)); chips.append(c); };
    chip("意图", value.objective.label);
    value.symbols.forEach(s => chip(s.symbol, s.label, s.scope === "HOLDING" ? "is-holding" : s.scope === "OUTSIDE_UNIVERSE" ? "is-outside" : ""));
    if (value.prices.length) chip("价格", value.prices.map(p => `$${p}`).join(" / "));
    if (value.quantities.length) chip("股数", value.quantities.join(" / "));
    chip("建议", value.suggested_mode === "DEEP" ? "深度研究" : "快速研究", "is-mode");
    const detail = el("details", null, "today-route-detail");
    detail.append(el("summary", `研究路线 · ${value.mode_reason}`));
    const list = el("ul"); value.tools.forEach(t => list.append(el("li", `${t.label} — ${t.when}`))); detail.append(list);
    detail.append(el("p", `Jev：${value.jev.route === "SHADOW" ? "影子评估 SEC 补查路由，不影响结果" : "关闭；SEC 补查使用确定性规则"} · 意图为关键词预判，提交后由研究模型确认。`, "small muted"));
    if (value.literal_error) detail.append(el("p", "问题中的价格或股数格式无法识别，请用普通数字。", "small today-warning"));
    route.replaceChildren(chips, detail); route.hidden = false;
    // Route preview is explanatory only; the explicit Auto/Fast/Deep control owns selection.
  }
  question.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(previewRoute, 450); });
  window.addEventListener("mia-ask-draft", () => setTimeout(previewRoute, 50));

  load();
})();
