"use strict";
// 市场 (D104): the last completed US session and up to 20 completed daily closes.
// A line is drawn only from bars the server returns (GET /api/market/eod); with fewer
// than two bars the view says so and draws nothing. No intraday points, no gap
// filling, no NAV. Closes and percentages are shown exactly as the server sends them
// (Decimal strings); the polyline converts to numbers only to place pixels.
// Without /api/market/eod the view falls back to the local last-session capture
// (Owner runtime only) and says that the 20-day series is unavailable.
(() => {
  const ds = document.body.dataset;
  if (ds.portfolioPriority) return;
  const main = document.querySelector("main");
  if (!main) return;
  const owner = ds.runtimeMode === "OWNER" && !ds.ivsPreview && !ds.publicIvs;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const svg = (tag, attrs) => { const n = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v)); return n; };
  const DECIMAL = /^-?\d{1,12}(\.\d{1,10})?$/;
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  const ROLE = { BENCHMARK: "大盘基准", SECTOR: "行业代理", HOLDING: "持仓", WATCHING: "关注" };
  const pct = v => v == null ? "—" : DECIMAL.test(String(v)) ? `${String(v).startsWith("-") ? "" : "+"}${v}%` : "—";
  const utc = iso => { const m = typeof iso === "string" && iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/); return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : "时间未知"; };

  const section = el("section", null, "market-view");
  section.id = "market"; section.setAttribute("aria-labelledby", "market-title");
  const head = el("div", null, "market-head");
  const title = el("h2", "上一完整交易日", null); title.id = "market-title";
  const session = el("p", "正在读取收盘数据…", "market-session"); session.setAttribute("role", "status");
  head.append(title, session);
  const grid = el("div", null, "market-grid");
  const foot = el("p", "收盘价为已收盘交易日的参考价，不是盘中报价、成交价或账户净值。下方「市场脉搏」的报价可能延迟，单独列出，不进入走势图。", "small muted market-foot");
  section.append(head, grid, foot);
  const grid0 = main.querySelector(":scope > .desk-entry-grid");
  if (grid0) grid0.after(section); else main.append(section);

  function sparkline(series) {
    const bars = (series.bars || []).filter(b => DATE.test(b.session_date || "") && DECIMAL.test(String(b.close || "")));
    if (bars.length < 2) return null;
    const values = bars.map(b => Number(b.close));
    const min = Math.min(...values), max = Math.max(...values), w = 320, h = 88, pad = 6;
    const x = i => pad + (i * (w - 2 * pad)) / (bars.length - 1);
    const y = v => max === min ? h / 2 : pad + ((max - v) * (h - 2 * pad)) / (max - min);
    const figure = el("figure", null, "market-chart");
    const chart = svg("svg", { viewBox: `0 0 ${w} ${h}`, width: "100%", height: h, role: "img", preserveAspectRatio: "none",
      "aria-label": `${series.symbol} 最近 ${bars.length} 个已收盘交易日收盘价，从 ${bars[0].session_date} 的 ${bars[0].close} 到 ${bars.at(-1).session_date} 的 ${bars.at(-1).close}` });
    chart.append(svg("polyline", { points: values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "), fill: "none", stroke: "currentColor", "stroke-width": 2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
    chart.append(svg("circle", { cx: x(bars.length - 1).toFixed(1), cy: y(values.at(-1)).toFixed(1), r: 3, fill: "currentColor" }));
    const caption = el("figcaption", `${bars[0].session_date} → ${bars.at(-1).session_date} · ${bars.length} 个交易日 · 区间 ${bars[values.indexOf(min)].close}–${bars[values.indexOf(max)].close}`, "small muted");
    figure.append(chart, caption);
    const table = el("details", null, "market-bars");
    table.append(el("summary", "查看收盘数据"));
    const t = el("table"); const tb = el("tbody");
    for (const b of bars.slice().reverse()) { const tr = el("tr"); tr.append(el("th", b.session_date), el("td", b.close, "market-num")); tb.append(tr); }
    t.append(tb); table.append(t);
    figure.append(table);
    figure.dataset.points = String(bars.length);
    return figure;
  }

  function card(series, meta) {
    const c = el("article", null, "market-card");
    c.dataset.symbol = series.symbol; c.dataset.status = series.status || "UNAVAILABLE";
    const top = el("div", null, "market-card-top");
    top.append(el("h3", series.symbol), el("span", ROLE[series.role] || "", "market-role"));
    c.append(top);
    const last = (series.bars || []).at(-1);
    const close = last && DECIMAL.test(String(last.close)) ? `${last.close}` : series.last_close && DECIMAL.test(String(series.last_close)) ? series.last_close : null;
    const row = el("p", null, "market-close");
    row.append(el("strong", close ? `${close} USD` : "收盘价不可用", "market-num"), el("span", ` 收盘 ${last?.session_date || series.session_date || ""}`, "small muted"));
    c.append(row);
    const changes = el("p", `1 日 ${pct(series.change_1d_pct)} · 5 日 ${pct(series.change_5d_pct)} · 20 日 ${pct(series.change_20d_pct)}`, "small market-changes");
    c.append(changes);
    const chart = sparkline(series);
    if (chart) c.append(chart);
    else c.append(el("p", `暂无 20 日收盘走势：${series.reason || "没有足够的已收盘交易日数据"}。不绘制估算走势。`, "small market-unavailable"));
    // Licensed close values (text, chart label, caption and bar table) are local-only: mark for screenshot masking.
    c.querySelectorAll(".market-close strong, .market-chart").forEach(n => { n.dataset.privateLocal = "price"; });
    c.append(el("p", [meta.provider && `来源 ${({ massive: "Massive", finnhub: "Finnhub", longbridge: "Longbridge" })[String(meta.provider).toLowerCase()] || meta.provider}`, meta.price_basis && `口径 ${meta.price_basis === "SPLIT_ADJUSTED_CLOSE" ? "拆股调整收盘" : meta.price_basis}`, meta.rights && `权限 ${meta.rights === "LOCAL_DISPLAY_ONLY" || meta.rights === "LOCAL_PERSONAL_DISPLAY_ONLY" ? "仅本机展示" : meta.rights}`, meta.retrieved_at && `获取 ${utc(meta.retrieved_at)}`].filter(Boolean).join(" · "), "small muted market-meta"));
    return c;
  }

  function renderServer(data) {
    grid.replaceChildren();
    section.dataset.source = "SERVER";
    session.textContent = data.last_completed_session ? `${data.last_completed_session} · 美股常规交易时段收盘` : "上一完整交易日未知";
    const order = { BENCHMARK: 0, SECTOR: 1, HOLDING: 2, WATCHING: 3 };
    (data.series || []).slice().sort((a, b) => (order[a.role] ?? 9) - (order[b.role] ?? 9)).forEach(s => grid.append(card(s, data)));
    if (!(data.series || []).length) grid.append(el("p", "服务器没有返回任何标的的收盘数据。", "market-empty"));
  }

  function renderFallback(local, reason) {
    grid.replaceChildren();
    section.dataset.source = "FALLBACK";
    const quotes = local && local.status === "READY" && Array.isArray(local.observations) ? local.observations : [];
    const eod = new Map();
    for (const q of quotes) if (q.kind === "EOD" || !eod.has(q.symbol)) eod.set(q.symbol, q);
    const sessionDate = [...eod.values()].map(q => q.session_date).find(d => DATE.test(d || ""));
    session.textContent = sessionDate ? `${sessionDate} · 来自本机收盘捕获` : "上一完整交易日：服务器暂未提供";
    const notice = el("p", `近 20 个交易日走势暂不可用：${reason}。不绘制估算或盘中走势。`, "market-unavailable market-notice");
    notice.setAttribute("role", "note");
    grid.append(notice);
    for (const q of eod.values()) {
      grid.append(card({ symbol: q.symbol, role: "HOLDING", status: "PARTIAL", bars: [], last_close: q.price, session_date: q.session_date, reason: "只有上一交易日收盘" },
        { provider: q.provider === "massive" ? "Massive" : q.provider === "finnhub" ? "Finnhub" : q.provider, rights: q.rights, retrieved_at: q.retrieved_at }));
    }
  }

  let loaded = false;
  async function load() {
    if (loaded) return; loaded = true;
    try {
      const r = await fetch("/api/market/eod?sessions=20", { cache: "no-store", headers: { Accept: "application/json" } });
      if (r.ok) { const data = await r.json(); if (data && Array.isArray(data.series)) return renderServer(data); }
      const reason = r.status === 404 ? "服务器还没有日线接口（/api/market/eod）" : `日线接口返回 ${r.status}`;
      const local = owner ? await fetch("/api/local-observations", { cache: "no-store", headers: { Accept: "application/json" } }).then(x => x.ok ? x.json() : null).catch(() => null) : null;
      renderFallback(local, reason);
    } catch {
      renderFallback(null, "无法读取日线数据");
    }
  }
  // Load when the market view is first opened (or if the page starts on it).
  window.addEventListener("mia-view", event => { if (event.detail?.view === "market") void load(); });
  if (document.body.dataset.consoleView === "market") void load();
})();
