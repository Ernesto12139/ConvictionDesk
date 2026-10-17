"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  snapshot: null,
  overview: null,
  evidence: [],
  busy: false,
  runId: null,
  pendingImport: null,
  allocation: "symbols",
  diagnostics: false,
  researchMode: "cockpit",
  loadingTimer: null,
};
const labels = {
  portfolio_snapshot: "持仓快照",
  market_data: "行情",
  news: "新闻",
  sec_filing: "SEC 文件",
  thesis: "投资逻辑",
  calculation: "确定性计算",
  semantic_judgment: "语义评估",
  daily_portfolio: "组合集中风险",
  stock_event_thesis: "BB 事件与投资逻辑",
  unsupported: "暂不支持的问题",
  event_materiality: "事件重要性",
  direction: "影响方向",
  persistence: "持续性",
  thesis_impact: "投资逻辑影响",
  execution_risk: "执行风险",
  confidence: "判断置信度",
  none: "无",
  low: "低",
  medium: "中",
  high: "高",
  insufficient: "证据不足",
  strengthens: "强化",
  weakens: "削弱",
  mixed: "影响混合",
  neutral: "中性",
  transient: "短期",
  uncertain: "尚不确定",
  persistent: "持续",
  no_material_change: "未构成实质改变",
  not_affected: "未受影响",
  core_driver: "核心驱动",
  key_assumption: "关键假设",
  invalidation_condition: "失效条件",
  success: "完成",
  error: "失败",
  running: "进行中",
  "portfolio.get_snapshot": "读取持仓快照",
  "market.get_eod": "获取日终行情",
  "portfolio.compute_analytics": "计算组合集中度",
  "news.search": "检索公司新闻",
  "sec.search": "检索 SEC 文件",
  "thesis.get": "读取 / 创建投资逻辑",
  "semantic.assess_thesis_impact": "评估事件对投资逻辑的影响",
  ai_thesis_draft: "生成 AI 投资逻辑草稿",
  thesis_impact_semantic_assessment: "语义评估",
  thesis_impact_semantic_assessment_comparison: "第二模型对照评估",
  grounded_event_answer: "生成有据可查的事件结论",
  grounded_concentration_synthesis: "组织组合集中度结论",
};
const label = (value) => labels[value] || value || "未提供";
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function date(value) {
  if (!value) return "未提供";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间不可用" :
    new Intl.DateTimeFormat("zh-CN", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(parsed) + " UTC";
}
function marketDay(value) {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", year: "numeric", month: "numeric", day: "numeric" }).format(new Date(value));
}
function sourceLabel(item) {
  const names = { calculation: "组合指标计算", portfolio_snapshot: "本次持仓", thesis: "投资逻辑依据", semantic_judgment: "事件影响评估" };
  const stamp = item.published_at || item.as_of;
  const day = stamp ? new Intl.DateTimeFormat("zh-CN", { timeZone: "UTC", month: "numeric", day: "numeric" }).format(new Date(stamp)) : "";
  const title = item.evidence_type === "sec_filing" ? `${item.symbol || "公司"} ${item.structured_data?.form || "SEC 披露"}` :
    item.evidence_type === "market_data" ? `${item.symbol || "股票"} ${item.structured_data?.basis ? "报价参考" : "日终行情"}` :
    item.evidence_type === "news" ? `${item.symbol || "公司"} 新闻` :
    item.structured_data?.decision_context ? "判断价与持仓计算" : names[item.evidence_type] || "研究来源";
  return title + (day ? ` · ${day}` : "");
}
function friendlyText(text) {
  let value = String(text || "").replaceAll("AI_DRAFT", "AI 草稿");
  state.evidence.forEach((item, index) => { value = value.replaceAll(item.evidence_id, `[${index + 1}]`); });
  return value;
}
function message(id, text, error = false) {
  $(id).textContent = text;
  $(id).classList.toggle("error", error);
}
function busy(value) {
  state.busy = value;
  document
    .querySelectorAll(
      "#manage-portfolio, #import-button, #choose-file, #confirm-import, #query-button, #tdb-button, [data-query], button[data-research-mode], #restore-form button, #entry-mode-manual, #entry-mode-csv, #csv-choose, #csv-preview-button, #csv-confirm",
    )
    .forEach((button) => {
      button.disabled = value;
    });
  $("csv-confirm").disabled = value || $("csv-preview").hidden;
  $("research").setAttribute("aria-busy", String(value));
}
function setResearchMode(mode) {
  if (state.busy) return;
  if (state.researchMode !== mode) {
    clearInspection();
    $("answer-state").textContent = "等待研究";
    message("query-status", "");
    $("result").replaceChildren(el("p", mode === "decision"
      ? "选择已有持仓并输入判断价格，开始新的持仓决策研究。"
      : "输入问题或选择研究主题，开始新的事件与快速研究。", "muted"));
  }
  state.researchMode = mode;
  $("cockpit-composer").open=true;
  document.body.dataset.researchMode = mode;
  $("result-title").textContent = mode === "decision" ? "持仓决策结论" : "研究结论";
  $("query-form").hidden = mode === "decision";
  document.querySelector(".quick-actions").hidden = mode === "decision";
  $("tdb-form").hidden = mode !== "decision";
  $("cockpit-correction").hidden = mode !== "cockpit";
  $("query-button").textContent = mode === "cockpit" ? "形成判断" : "开始研究";
  if (mode === "cockpit") $("result-title").textContent = "当前判断";
  document.querySelectorAll("button[data-research-mode]").forEach((button) =>
    button.setAttribute("aria-pressed", String(button.dataset.researchMode === mode)));
}
document.querySelectorAll("button[data-research-mode]").forEach((button) =>
  button.addEventListener("click", () => setResearchMode(button.dataset.researchMode)));
function showLoading(title, detail, price = null) {
  const started = Date.now();
  const box = el("div", null, "loading-state");
  const pulse = el("span", null, "loading-pulse");
  pulse.setAttribute("aria-hidden", "true");
  const elapsed = el("p", "", "small muted");
  box.append(pulse, el("h3", title), el("p", detail));
  if (price !== null) box.append(el("p", `你输入的判断价格：$${price} USD`, "loading-price"));
  box.append(elapsed);
  $("result").replaceChildren(box);
  state.loadingTimer = setInterval(() => {
    if (Date.now() - started > 45000) elapsed.textContent = "研究仍在进行。可先查看已建立的价格与持仓上下文。";
  }, 1000);
}
function stopLoading() {
  if (state.loadingTimer !== null) clearInterval(state.loadingTimer);
  state.loadingTimer = null;
}
async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error("Request failed");
    error.status = response.status;
    error.detail = data?.detail;
    throw error;
  }
  return data;
}
function errorMessage(detail, status) {
  if (detail?.code === "DECISION_HISTORY_INVALID")
    return "已有决策记录未通过完整性校验，本次已停止生成判断。持仓与历史仍保留，请检查历史记录兼容性；不要清空数据库。";
  if (detail?.code === "DECISION_INTERNAL_ERROR")
    return "持仓决策流程发生内部错误，本次没有生成行动判断。持仓仍已保存，请查看服务终端的任务诊断记录。";
  if (detail?.code === "DECISION_OBSERVATION_LOST")
    return "暂时无法读取研究进度。已有任务可能仍在运行，可继续查看同一次研究。";
  if (detail?.code === "DECISION_JOB_NOT_FOUND")
    return "本次研究进度已不可用，可能是服务重启。已保存的决策和冻结证据仍保留，请重新发起研究。";
  if (detail?.code === "DECISION_JUDGE_UNAVAILABLE")
    return "持仓决策模型尚未配置。请检查服务端模型配置（默认模型需要 OPENAI_API_KEY）并重启；本次没有生成行动判断。";
  if (detail?.code === "DECISION_JUDGE_FAILED")
    return "持仓决策模型本次未能返回通过校验的结果，未生成行动判断。请稍后重试。";
  if (detail?.code === "THESIS_BOOTSTRAP_FAILED")
    return "本次未能生成通过证据校验的投资逻辑草稿，未生成行动判断。请稍后重试。";
  const text = typeof detail === "string" ? detail : detail?.error || detail?.code || "";
  if (["MARKET_DATA_UNAVAILABLE", "MARKET_DATA_INVALID"].includes(text)) {
    const cause = detail?.provider_error || detail?.code;
    if (cause === "configuration")
      return "尚未配置日终行情源，请在服务端配置 Massive 后重启；本次没有生成研究结论。";
    if (cause === "rate_limit")
      return "日终行情源请求已达上限，请等待后再试，连续点击不会加快恢复。本次没有生成新的行动判断，持仓仍已保存。";
    if (text === "MARKET_DATA_INVALID" || ["invalid_data", "invalid_input"].includes(cause))
      return "日终行情数据未通过校验，系统已停止生成判断。持仓仍已保存；请先检查行情源问题再重试。";
    return "日终行情源暂时不可用，本次没有生成新的行动判断。持仓仍已保存，请稍后再试。";
  }
  if (text.includes("INSUFFICIENT_HISTORY"))
    return "还没有可用的持仓。请先建立组合，再运行集中风险研究。";
  if (/INSUFFICIENT_HISTORICAL_EVIDENCE|point-in-time|cutoff/.test(text))
    return "数据的时间条件不满足本次分析要求，系统已停止生成结论。稍后可再次尝试。";
  if (
    /configuration|authentication|key is required|not configured|User-Agent|unauthorized|authentication failed/i.test(
      text,
    )
  )
    return "数据源或模型尚未配置，或凭据无效。请在启动服务的终端配置所需环境变量后重启。不要在页面输入密钥。";
  if (/rate_limit|rate limit/i.test(text))
    return "数据源或模型请求次数已达上限，请稍后重试。";
  if (
    /LLM|SemanticJudge|schema_validation|invalid_data|unsupported_claim|unsupported_numeric_fact|invalid_evidence_reference|malformed|empty_content/.test(
      text,
    )
  )
    return "模型或数据源未返回符合校验要求的结果，本次未生成可信结论。请稍后重试。";
  if (/timeout|transport|retryable|both_failed/.test(text))
    return "数据源或模型暂时不可用。请稍后重试。";
  if (status === 404) return "未找到这条本地记录，请核对研究记录编号。";
  if (status === 422) return "输入格式不符合要求，请检查后重试。";
  return "服务暂时无法完成请求。请确认本地服务仍在运行，然后重试。";
}
function safeSource(value) {
  // Reject, rather than rewrite, signed/query-bearing links or embedded credentials.
  // The UI does not need arbitrary provider URLs to be clickable.
  try {
    const url = new URL(value);
    if (
      !["https:", "http:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      return null;
    if (
      /token|secret|api[_-]?key|authorization|bearer/i.test(
        decodeURIComponent(url.pathname),
      )
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}
function evidenceLinks(ids, evidence = state.evidence) {
  const group = el("div", null, "evidence-links");
  for (const id of ids || []) {
    const item = evidence.find((entry) => entry.evidence_id === id);
    const number = evidence.indexOf(item) + 1;
    const title = item ? `[${number}] ${sourceLabel(item)}` : "来源暂不可用";
    const button = el("button", state.diagnostics ? id : title, "evidence-link");
    button.type = "button";
    button.setAttribute("aria-label", `查看来源 ${title}`);
    button.addEventListener("click", () => {
      state.evidence = evidence;
      $("evidence-count").textContent = String(evidence.length);
      $("evidence-filter").value = "all";
      renderEvidence();
      $("evidence-details").open = true;
      const target = [...$("evidence-list").children].find(
        (node) => node.dataset.evidenceId === id,
      );
      if (target) {
        target.focus();
        target.scrollIntoView({ block: "center" });
      } else
        message(
          "restore-status",
          "该引用不在本次返回的证据中；未将其当作可检查的来源。",
          true,
        );
    });
    group.append(button);
  }
  return group;
}
function renderPortfolio(snapshot) {
  state.snapshot = snapshot;
  const tdbSelect = $("tdb-symbol");
  const selectedSymbol = tdbSelect.value;
  tdbSelect.replaceChildren(el("option", snapshot ? "选择已有持仓" : "先建立组合"));
  tdbSelect.firstChild.value = "";
  if (snapshot) {
    for (const symbol of [...new Set(snapshot.positions.map((position) => position.symbol))].sort()) {
      const option = el("option", symbol);
      option.value = symbol;
      tdbSelect.append(option);
    }
    if ([...tdbSelect.options].some((option) => option.value === selectedSymbol))
      tdbSelect.value = selectedSymbol;
  }
  $("portfolio-summary").replaceChildren();
  $("holdings").hidden = !snapshot;
  if (!snapshot) {
    $("portfolio-state").textContent = "尚未建立";
    $("manage-portfolio").textContent = "建立组合";
    $("portfolio-summary").append(
      el("h3", "从第一笔持仓开始。"),
      el(
        "p",
        "填入账户、股数和每股成本，就能看清组合。无需准备文件，也不需要连接券商。",
        "muted small",
      ),
    );
    const start = el("button", "建立我的组合", "primary onboarding-button");
    start.addEventListener("click", () => $("manage-portfolio").click());
    $("portfolio-summary").append(start);
    return;
  }
  $("manage-portfolio").textContent = "管理持仓";
  $("portfolio-state").textContent = "已保存";
  const overview = el("div", null, "portfolio-overview");
  for (const [count, title] of [
    [snapshot.accounts.length, "账户"],
    [snapshot.positions.length, "账户持仓记录"],
  ]) {
    const node = el("div");
    node.append(el("strong", count), el("p", title, "small muted"));
    overview.append(node);
  }
  overview.append(el("span", `更新于 ${date(snapshot.as_of)}`, "small muted"));
  $("portfolio-summary").append(overview);
  $("accounts").replaceChildren();
  $("positions").replaceChildren();
  const accountNames = new Map(
    snapshot.accounts.map((account) => [account.account_id, account.name]),
  );
  for (const account of snapshot.accounts) {
    const card = el("div", null, "account-card");
    card.append(
      el("span", account.name),
      el("strong", `$${account.cash}`),
      el("span", "账户现金 · USD", "muted"),
    );
    $("accounts").append(card);
  }
  for (const position of snapshot.positions) {
    const row = el("tr");
    for (const value of [
      position.symbol,
      accountNames.get(position.account_id),
      position.quantity,
      position.avg_cost,
    ])
      row.append(el("td", value));
    $("positions").append(row);
  }
}
function renderAllocation(kind = state.allocation) {
  state.allocation = kind;
  const allocation = state.overview?.allocation;
  if (!allocation) return;
  document.querySelectorAll("[data-allocation]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.allocation === kind));
    const names = allocation.basis === "market"
      ? { symbols: "个股与现金", sectors: "行业集中度", themes: "主题集中度" }
      : { symbols: "标的与现金成本", sectors: "行业成本敞口", themes: "主题成本敞口" };
    button.textContent = names[button.dataset.allocation];
  });
  const rows = allocation[kind] || [];
  const donut = $("allocation-donut");
  donut.style.background = allocation[`${kind}_gradient`] === "none"
    ? "#e7ebe3"
    : allocation[`${kind}_gradient`];
  donut.setAttribute(
    "aria-label",
    rows.length ? rows.map((row) => `${row.label} ${row.weight}`).join("；") : "暂无可展示分布",
  );
  const basisLabels = allocation.basis === "market"
    ? {
        symbols: ["当前分布", "占组合净值 · 含现金"],
        sectors: ["行业集中度", "已定价权益口径"],
        themes: ["主题集中度", "已定价权益口径"],
      }
    : {
        symbols: ["成本敞口", "持仓成本 + 现金"],
        sectors: ["行业成本", "持仓成本口径"],
        themes: ["主题成本", "持仓成本口径"],
      };
  $("allocation-center").textContent = kind === "symbols" ? "组合" : kind === "sectors" ? "行业" : "主题";
  $("allocation-basis").replaceChildren(el("strong", basisLabels[kind][0]),
    el("span", basisLabels[kind][1]));
  $("allocation-legend").replaceChildren();
  if (!rows.length) {
    $("allocation-legend").append(el("p", "当前行情不足，行业和主题权重暂不可计算。", "muted small"));
  } else {
    for (const row of rows) {
      const item = el("div", null, "legend-row");
      const swatch = el("span", null, "legend-swatch");
      swatch.style.background = row.color;
      item.append(swatch, el("span", row.label), el("strong", row.weight));
      if (row.members?.length) {
        const details = el("details", null, "allocation-other");
        const summary = el("summary");
        summary.append(el("span", `${row.label} · ${row.members.length} 项`), el("strong", row.weight));
        const members = el("div", null, "other-members");
        for (const member of row.members) {
          const memberRow = el("div", null, "other-member");
          memberRow.append(el("span", member.label), el("strong", member.weight)); members.append(memberRow);
        }
        details.append(summary, members);
        $("allocation-legend").append(details);
      } else $("allocation-legend").append(item);
    }
  }
}
function renderOverview(overview) {
  state.overview = overview;
  $("portfolio-intelligence").hidden = false;
  $("portfolio-cards").replaceChildren();
  for (const card of overview.cards) {
    const node = el("div", null, "portfolio-card");
    node.append(el("span", card.label), el("strong", card.value), el("small", card.note));
    $("portfolio-cards").append(node);
  }
  const notice = $("valuation-notice");
  const missing = overview.missing_price_symbol_count;
  const marketDays = [...new Set(overview.positions.filter((row) => row.price_as_of)
    .map((row) => marketDay(row.price_as_of)))];
  if (overview.valuation_status === "complete") {
    notice.textContent = `已覆盖全部 ${overview.positions.length} 个标的 · ${marketDays.length === 1 ? `${marketDays[0]} 收盘` : "日终行情"}。分布口径见图内说明。`;
    notice.classList.remove("warning");
  } else {
    const cause = overview.provider_error === "configuration" ? "尚未配置日终行情源；" :
      overview.provider_error ? "日终行情服务暂时不可用；" : "";
    notice.textContent = `${cause}${missing} 个标的缺少有效行情；相应市值与盈亏保持未知。图表显示成本敞口，非实时仓位权重。`;
    notice.classList.add("warning");
  }
  $("position-count").textContent = `${overview.positions.length} 个标的`;
  $("aggregate-positions").replaceChildren();
  for (const position of overview.positions) {
    const row = el("tr");
    const symbol = el("td");
    symbol.append(el("strong", position.symbol));
    if (position.account_count > 1) symbol.append(el("span", `跨 ${position.account_count} 个账户`, "duplicate-badge"));
    const accounts = el("td");
    const details = el("details", null, "account-breakdown");
    details.append(el("summary", `${position.account_count} 个账户`));
    for (const account of position.accounts)
      details.append(el("p", `${account.name} · ${account.quantity} 股 @ ${account.average_cost}`));
    accounts.append(details);
    const close = el("td", position.price);
    if (position.price_as_of) close.append(el("small", marketDay(position.price_as_of), "price-day"));
    for (const cell of [
      symbol,
      el("td", position.quantity),
      el("td", position.average_cost),
      close,
      el("td", `${position.market_value} · ${position.weight}`),
      el("td", `${position.unrealized_pnl} · ${position.unrealized_pnl_percent}`),
      accounts,
    ]) row.append(cell);
    $("aggregate-positions").append(row);
  }
  renderAllocation();
}
async function loadPortfolio() {
  try {
    const snapshot = await api("/api/portfolio/current");
    if (state.snapshot?.snapshot_id !== snapshot.snapshot_id) $("holding-search").value = "";
    renderPortfolio(snapshot);
    window.dispatchEvent(new Event("mia-portfolio-changed"));
    renderOverview(await api("/api/console/portfolio-overview"));
  } catch (error) {
    if (error.status === 404) {
      renderPortfolio(null);
      $("portfolio-intelligence").hidden = true;
    }
    else {
      $("portfolio-state").textContent = "读取失败";
      $("portfolio-summary").replaceChildren(
        el(
          "p",
          "无法读取持仓。请确认服务可用后刷新页面；当前状态未知。",
          "status-line error",
        ),
      );
    }
  }
}
function clearInspection() {
  state.evidence = [];
  state.runId = null;
  state.answer = null;
  $("evidence-count").textContent = "0";
  $("evidence-list").replaceChildren(el("p", "尚无本次分析证据。", "muted"));
  $("trace").replaceChildren(el("p", "等待本次执行记录。", "muted"));
  $("run-label").textContent = "等待分析完成";
  $("evidence-details").open = false;
  $("trace-details").open = false;
}
function setRunId(id) {
  state.runId = id;
  $("run-label").textContent = state.diagnostics ? `Run · ${id}` : "分析依据可检查";
  $("run-input").value = id;
}
function renderEvidence() {
  const filter = $("evidence-filter").value;
  $("evidence-list").replaceChildren();
  const items = state.evidence.filter(
    (item) => filter === "all" || item.evidence_type === filter,
  );
  if (!items.length)
    $("evidence-list").append(el("p", "本次没有此类证据。", "muted"));
  for (const item of items) {
    const card = el("article", null, "evidence-card");
    card.tabIndex = -1;
    card.dataset.evidenceId = item.evidence_id;
    card.append(
      el("span", label(item.evidence_type), "tag"),
      el("h3", `[${state.evidence.indexOf(item) + 1}] ${item.title || sourceLabel(item)}`),
    );
    const meta = el("div", null, "evidence-meta");
    for (const text of [
      `提供方 · ${item.provider}`,
      item.structured_data?.source
        ? `来源 · ${item.structured_data.source}`
        : null,
      item.symbol,
      item.structured_data?.form ? `表单 · ${item.structured_data.form}` : null,
      `发布 / 接收 · ${date(item.published_at)}`,
      `数据时点 · ${date(item.as_of)}`,
      `获取时间 · ${date(item.retrieved_at)}`,
    ])
      if (text) meta.append(el("span", text));
    card.append(meta);
    if (item.excerpt) card.append(el("p", item.excerpt, "excerpt"));
    const url = safeSource(item.source_uri);
    if (url) {
      const link = el("a", "查看原始来源 ↗");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      card.append(link);
    } else if (item.source_uri)
      card.append(
        el(
          "p",
          "来源链接未开放：仅支持不含查询参数、凭据或片段的 HTTP(S) 链接。",
          "small muted",
        ),
      );
    if (item.evidence_type === "calculation")
      card.append(
        el("p", "根据本次持仓与有效股价计算，可在分析详情中核对。", "small muted"),
      );
    if (
      item.evidence_type === "thesis" ||
      item.evidence_type === "semantic_judgment"
    )
      card.append(
        el("p", "结构化内容见研究结果中的投资逻辑与语义评估。", "small muted"),
      );
    if (state.diagnostics) card.append(el("code", item.evidence_id));
    $("evidence-list").append(card);
  }
}
function renderThesis(item) {
  const profile = item.structured_data;
  const details = el("details", null, "subsection");
  details.append(
    el(
      "summary",
      "当前投资逻辑 · " +
        (profile.status === "AI_DRAFT" ? "AI 草稿，未经你确认" : "用户已确认"),
    ),
  );
  details.append(
    el(
      "p",
      state.diagnostics ? `版本 ${profile.thesis_version_id} · ${date(profile.as_of)} · ${profile.generation?.model || "未提供模型"}` : `依据截至 ${date(profile.as_of)} 的公开材料`,
      "small muted",
    ),
  );
  for (const [key, title] of [
    ["core_drivers", "核心驱动"],
    ["key_assumptions", "关键假设"],
    ["execution_risks", "执行风险"],
    ["invalidation_conditions", "失效条件"],
  ]) {
    details.append(el("h3", title));
    for (const statement of profile[key] || []) {
      const row = el("div", null, "thesis-statement");
      row.append(
        el("p", friendlyText(statement.text)),
        evidenceLinks(statement.evidence_ids),
      );
      details.append(row);
    }
  }
  return details;
}
function assessmentFields(assessment) {
  const grid = el("div", null, "assessment-grid");
  for (const key of [
    "thesis_impact",
    "event_materiality",
    "direction",
    "persistence",
    "execution_risk",
    "confidence",
  ]) {
    const cell = el("div");
    cell.append(el("span", label(key)), el("b", label(assessment[key])));
    grid.append(cell);
  }
  return grid;
}
function renderSemantic(items) {
  const section = el("section", null, "subsection");
  section.append(el("h3", "事件影响评估"));
  const assessment = items[0].structured_data.assessment;
  section.append(
    el(
      "p",
      state.diagnostics ? `${assessment.model} · ${date(assessment.created_at)}` : "主要评估 · 仅依据本次材料",
      "small muted",
    ),
    assessmentFields(assessment),
    el(
      "p",
      "置信度仅描述对所给证据的判断把握，不代表未来收益概率。",
      "small muted",
    ),
  );
  for (const [title, ids] of [
    ["支持证据", assessment.supporting_evidence_ids],
    ["反向 / 反证", assessment.counter_evidence_ids],
  ]) {
    section.append(el("h3", title, "subsection"));
    section.append(
      ids?.length
        ? evidenceLinks(ids)
        : el("p", "未识别到独立证据；这不代表不存在。", "small muted"),
    );
  }
  const components = el("details", null, "subsection");
  components.append(el("summary", "逐项投资逻辑影响"));
  for (const item of assessment.component_impacts || []) {
    const row = el("div", null, "claim");
    row.append(
      el(
        "p",
        `${label(item.component_type)} ${item.component_index + 1} · ${label(item.impact)}`,
      ),
      evidenceLinks(item.evidence_ids),
    );
    components.append(row);
  }
  section.append(components);
  if (items.length > 1) {
    const compare = el("details", null, "subsection");
    compare.append(
      el("summary", "模型判断对照"),
      el(
        "p",
        "并列呈现返回的判断，不选出赢家。可检查每个模型引用的输入证据。",
        "small muted",
      ),
    );
    const wrap = el("div", null, "table-wrap subsection");
    wrap.tabIndex = 0;
    const table = el("table", null, "comparison-table");
    const head = el("tr");
    head.append(el("th", "评估维度"));
    for (const item of items) {
      const cell = el("th", item.structured_data.assessment.model);
      cell.scope = "col";
      head.append(cell);
    }
    const thead = el("thead");
    thead.append(head);
    table.append(thead);
    const tbody = el("tbody");
    for (const key of [
      "thesis_impact",
      "event_materiality",
      "direction",
      "persistence",
      "execution_risk",
      "confidence",
    ]) {
      const row = el("tr");
      row.append(el("td", label(key)));
      for (const item of items)
        row.append(el("td", label(item.structured_data.assessment[key])));
      tbody.append(row);
    }
    table.append(tbody);
    wrap.append(table);
    compare.append(wrap);
    for (const item of items)
      compare.append(
        el("p", item.structured_data.assessment.model, "small subsection"),
        evidenceLinks(item.structured_data.input_evidence_ids),
      );
    section.append(compare);
  }
  return section;
}
function renderAnswer(answer, query) {
  state.answer = answer;
  setRunId(answer.run_id);
  state.evidence = answer.evidence;
  $("evidence-count").textContent = String(state.evidence.length);
  $("evidence-filter").value = "all";
  renderEvidence();
  $("answer-state").textContent = answer.claims.length ? "分析完成" : "证据不足";
  $("result").replaceChildren(el("p", "正在整理研究摘要…", "muted"));
}
function sourceDossier(ids) {
  const group = el("div", null, "source-dossier");
  const items = [...new Set(ids)].map((id) => state.evidence.find((item) => item.evidence_id === id)).filter(Boolean);
  if (!items.length) return el("p", "本次未找到独立证据；不代表不存在。", "small muted");
  const more = el("details", null, "more-sources");
  more.append(el("summary", `查看其余 ${Math.max(0, items.length - 2)} 项依据`));
  items.forEach((item, index) => {
    const card = el("article", null, "source-preview");
    card.append(el("p", item.title || sourceLabel(item)), evidenceLinks([item.evidence_id]));
    if (index < 2) group.append(card); else more.append(card);
  });
  if (items.length > 2) group.append(more);
  return group;
}
function renderModelInspection(brief) {
  if (!brief.primary_model) return null;
  const section = el("section", null, "model-inspection");
  section.append(el("h3", "第二视角 · 事件语义对照"),
    el("p", `主分析 · ${brief.primary_model}`, "small muted"));
  const comparison = brief.comparison;
  if (!comparison) return section;
  if (comparison.status === "not_configured") {
    section.append(el("p", "事件对照未配置；本次仅由主模型评估。", "small muted"));
    return section;
  }
  if (comparison.status === "unavailable") {
    section.append(el("p", "事件对照已尝试，但本次未完成；主结论仍来自主模型。可在本地诊断中查看失败记录。", "small muted"));
    return section;
  }
  section.append(el("p", `${comparison.comparison_judge === "jev" ? "Jev 事件对照" : "事件对照评估"}已完成 · ${comparison.comparison_model}`, "small muted"));
  const summary = comparison.status === "aligned" ? "两项评估在核心判断维度一致。" :
    comparison.status === "insufficient" ? "对照评估认为部分维度证据不足；请留意下方差异。" :
      "两项评估在部分判断维度存在差异。";
  section.append(el("p", summary));
  if (comparison.aligned_dimensions?.length)
    section.append(el("p", `一致：${comparison.aligned_dimensions.join("、")}`, "small muted"));
  if (comparison.latency_ms !== null && comparison.latency_ms !== undefined)
    section.append(el("p", `对照用时 ${(comparison.latency_ms / 1000).toFixed(1)} 秒 · 基于同一组输入材料`, "small muted"));
  if (comparison.differences.length) {
    const details = el("details", null, "model-differences");
    details.append(el("summary", `查看 ${comparison.differences.length} 项判断差异`));
    for (const item of comparison.differences) {
      const row = el("p");
      row.append(el("strong", item.dimension),
        el("span", `主分析：${label(item.primary)} · 对照：${label(item.comparison)}`));
      details.append(row);
    }
    section.append(details);
  }
  section.append(evidenceLinks([comparison.primary_evidence_id, comparison.comparison_evidence_id].filter(Boolean)));
  if (comparison.comparison_judge === "jev")
    section.append(el("p", "Jev 用于核查事件对投资逻辑的语义影响；它不提供股价、组合计算或未来收益预测。", "small muted"));
  return section;
}
function renderBrief(brief) {
  const answer = state.answer;
  if (!answer) return;
  const result = $("result"); result.replaceChildren();
  result.append(el("p", brief.kind === "daily" ? "组合集中风险" : brief.kind === "event" ? "事件与投资逻辑" : "目前可用能力", "eyebrow"),
    el("h3", brief.headline, "conclusion"), el("p", brief.deck, "brief-deck"), evidenceLinks(brief.evidence_ids));
  if (brief.kind === "unsupported") $("answer-state").textContent = "暂不支持";
  const model = renderModelInspection(brief);
  if (brief.kind === "event" && model) {
    const details = el("details", null, "model-disclosure");
    details.append(el("summary", "查看事件语义评估对照"), model); result.append(details);
  }
  if (brief.facts.length) {
    const grid = el("div", null, "brief-facts");
    for (const fact of brief.facts) {
      const card = el("div"); card.append(el("span", fact.label), el("strong", fact.value), el("small", fact.note)); grid.append(card);
    }
    result.append(grid);
  }
  if (brief.caution) result.append(el("p", brief.caution, brief.partial ? "notice warning" : "brief-caution"));
  if (brief.kind !== "event" && model) result.append(model);
  const semantic = answer.evidence.filter((item) => item.evidence_type === "semantic_judgment" && item.structured_data?.assessment);
  if (semantic.length) {
    const assessment = semantic[0].structured_data.assessment;
    const rationale = el("div", null, "rationale-grid");
    for (const [title, ids] of [["支持这一判断", assessment.supporting_evidence_ids], ["反向证据与不确定性", assessment.counter_evidence_ids]]) {
      const column = el("section"); column.append(el("h3", title), sourceDossier(ids || [])); rationale.append(column);
    }
    result.append(rationale);
    const timeline = el("details", null, "subsection");
    const events = answer.evidence.filter((item) => ["news", "sec_filing"].includes(item.evidence_type));
    timeline.append(el("summary", `事件材料 · ${events.length} 项新闻与公司披露`));
    for (const item of events) {
      const row = el("article", null, "timeline-entry");
      row.append(el("p", `${date(item.published_at)} · ${item.title || sourceLabel(item)}`), evidenceLinks([item.evidence_id]));
      if (item.excerpt) { const excerpt = el("details"); excerpt.append(el("summary", "阅读来源摘录"), el("p", item.excerpt, "excerpt")); row.append(excerpt); }
      timeline.append(row);
    }
    result.append(timeline);
    const detail = el("details", null, "subsection");
    detail.append(el("summary", "投资逻辑与评估详情"), renderSemantic(semantic));
    for (const item of answer.evidence.filter((item) => item.evidence_type === "thesis" && item.structured_data)) detail.append(renderThesis(item));
    result.append(detail);
  }
  if (answer.limitations.length) {
    const box = el("details", null, "limitations"); box.append(el("summary", `数据限制与判断边界 · ${answer.limitations.length}`));
    const list = el("ul");
    for (const value of answer.limitations) {
      const text = value.startsWith("Stale EOD") ? "过期行情已从计算中排除。" :
        value.startsWith("Missing EOD") ? "部分持仓缺少有效价格，不能将已定价部分视作完整组合。" :
        value.startsWith("INSUFFICIENT_EVIDENCE") ? "缺少相互独立的投资逻辑基线与新事件证据。" :
        value === "Semantic confidence is limited to the supplied evidence." ? "判断把握仅针对本次提供的证据，不代表未来收益概率。" : friendlyText(value);
      list.append(el("li", text));
    }
    box.append(list); result.append(box);
  }
  if (brief.kind === "daily") {
    const details = el("details", null, "subsection"); details.append(el("summary", "查看估值、计算口径与行情时间"));
    const metrics = el("div"); metrics.id = "run-metrics"; details.append(metrics); result.append(details);
  }
  if (state.diagnostics) {
    const full = el("details", null, "subsection"); full.append(el("summary", "诊断：完整权威答复"), el("p", answer.answer));
    for (const claim of answer.claims) full.append(el("p", claim.text), evidenceLinks(claim.evidence_ids));
    result.append(full);
  }
}
function renderMetrics(data) {
  const target = $("run-metrics");
  if (!target || !data.cards.length) return;
  target.replaceChildren();
  if (data.partial)
    target.append(
      el(
        "p",
        "部分估值 · 缺失价格仍为未知，请结合下方局限阅读。",
        "notice warning subsection",
      ),
    );
  if (state.snapshot && state.snapshot.snapshot_id !== data.snapshot_id)
    target.append(
      el(
        "p",
        "此记录使用的是历史快照，与页面上方当前持仓不同。",
        "notice warning subsection",
      ),
    );
  const grid = el("div", null, "metric-grid");
  for (const card of data.cards) {
    const node = el("div", null, "metric");
    node.append(
      el("span", card.label),
      el("strong", card.value),
      el("small", card.note),
    );
    grid.append(node);
  }
  target.append(grid, evidenceLinks([data.evidence_id]));
  const observationTimes = [
    ...new Set(Object.values(data.market_observation_as_of)),
  ];
  target.append(
    el(
      "p",
      observationTimes.length
        ? `本次行情时间 · ${observationTimes.slice(0, 2).map(date).join(" / ")}${observationTimes.length > 2 ? " · 其余见明细" : ""}`
        : "本次无有效行情；缺失价格仍为未知。",
      "small muted subsection",
    ),
  );
  const details = el("details", null, "subsection");
  details.append(
    el("summary", "估值明细与行情时间"),
    el("p", `分析持仓快照 · ${date(data.portfolio_as_of)}`, "small muted"),
    el("p", `市场证据截止 · ${date(data.market_cutoff)}`, "small muted"),
  );
  const entries = Object.entries(data.market_observation_as_of);
  for (const [symbol, time] of entries)
    details.append(
      el("p", `${symbol} 行情时间 · ${date(time)}`, "small muted"),
    );
  if (!entries.length)
    details.append(el("p", "没有通过时效检查的行情。", "small muted"));
  const wrap = el("div", null, "table-wrap subsection");
  wrap.tabIndex = 0;
  const table = el("table");
  const head = el("tr");
  for (const title of ["持仓记录", "市值 · 美元", "占组合净值", "浮动盈亏 · 美元"]) {
    const cell = el("th", title);
    cell.scope = "col";
    head.append(cell);
  }
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const tbody = el("tbody");
  for (const row of data.positions) {
    const tr = el("tr");
    for (const value of [state.diagnostics ? row.position_id : row.symbol, row.value, row.weight, row.pnl])
      tr.append(el("td", value));
    tbody.append(tr);
  }
  table.append(tbody);
  wrap.append(table);
  details.append(wrap);
  target.append(details);
}
function renderTrace(record) {
  const target = $("trace");
  target.replaceChildren();
  const trace = record.trace;
  if (!trace) {
    target.append(el("p", "执行记录尚未完成，请稍后重新载入。", "muted"));
    return;
  }
  if (
    record.answer &&
    trace.model_events?.some(
      (event) =>
        event.purpose.endsWith("comparison") && event.status === "error",
    )
  ) {
    $("result").append(
      el(
        "p",
        "对照模型本次未完成，主模型结论仍可阅读。失败步骤见执行记录。",
        "notice warning subsection",
      ),
    );
  }
  target.append(
    el(
      "p",
      `${label(record.status)} · ${label(trace.route_plan.intent)} · ${trace.query}`,
      "trace-summary",
    ),
  );
  const route = el("div", null, "trace-step");
  route.append(
    el("h3", "路由与工具计划"),
    el(
      "p",
      trace.route_plan.required_tools.map(label).join(" → ") || "无工具执行",
    ),
    el("p", `指定截止时间 · ${date(trace.requested_as_of)}`, "trace-meta"),
  );
  target.append(route);
  for (const call of trace.tool_calls) {
    const step = el("div", null, `trace-step ${call.status}`);
    step.append(
      el("h3", `${call.sequence}. ${label(call.name)} · ${label(call.status)}`),
      el("p", call.name, "trace-meta"),
      el(
        "p",
        `${date(call.started_at)} → ${date(call.ended_at)}`,
        "trace-meta",
      ),
    );
    if (call.error) step.append(el("p", errorMessage(call.error)));
    step.append(
      evidenceLinks(
        call.output_refs.filter((id) =>
          state.evidence.some((item) => item.evidence_id === id),
        ),
      ),
    );
    target.append(step);
  }
  for (const event of [
    ...(trace.model_events || []),
    ...(trace.synthesis ? [trace.synthesis] : []),
  ]) {
    const step = el("div", null, `trace-step ${event.status}`);
    step.append(
      el("h3", `${label(event.purpose)} · ${label(event.status)}`),
      el("p", `模型 / 执行器 · ${event.model}`, "trace-meta"),
      el(
        "p",
        `${date(event.started_at)} → ${date(event.ended_at)}`,
        "trace-meta",
      ),
    );
    if (event.error) step.append(el("p", errorMessage(event.error)));
    step.append(evidenceLinks(event.evidence_ids));
    target.append(step);
  }
  const final = el("div", null, "trace-step");
  final.append(el("h3", "最终判断 → 证据"));
  for (const [claim, ids] of Object.entries(trace.final_claim_links))
    final.append(el("p", claim), evidenceLinks(ids));
  if (!Object.keys(trace.final_claim_links).length)
    final.append(el("p", "本次没有最终实质判断。", "muted"));
  target.append(final);
}
async function loadInspection(runId) {
  const responses = await Promise.allSettled([
    api(`/api/console/runs/${encodeURIComponent(runId)}/presentation`),
    api(`/api/console/runs/${encodeURIComponent(runId)}/metrics`),
  ]);
  if (state.answer?.run_id === runId) {
    if (responses[0].status === "fulfilled") renderBrief(responses[0].value);
    else $("result").replaceChildren(el("p", "摘要暂时无法读取。分析已保存，请稍后重试。", "notice warning"));
    if (responses[1].status === "fulfilled") renderMetrics(responses[1].value);
  }
  if (!state.diagnostics) return;
  try { renderTrace(await api(`/api/runs/${encodeURIComponent(runId)}`)); }
  catch { $("trace").replaceChildren(el("p", "执行记录读取失败。", "status-line error")); }
}
function failure(detail, status) {
  $("answer-state").textContent = "未完成";
  const box = el("div", null, "error-box");
  box.append(
    el("h3", "这次分析未能完成"),
    el("p", errorMessage(detail, status)),
    el("p", detail?.code === "DECISION_OBSERVATION_LOST"
      ? "可继续查看同一次研究，不需要重新生成。"
      : detail?.code === "DECISION_JUDGE_FAILED" && state.researchMode === "decision"
      ? "本次研究输入已保存，可直接复用证据重新生成判断。"
      : `修正问题后，可再次点击「${state.researchMode === "decision" ? "生成决策简报" : "开始研究"}」。`, "muted small"),
  );
  $("result").replaceChildren(box);
}
$("import-button").addEventListener("click", () => {
  state.pendingImport = null;
  $("confirm-import").disabled = true;
  $("import-preview").replaceChildren(
    el("strong", "尚未选择文件"), el("span", "支持 JSON v1，最大 10 MB。"),
  );
  $("import-dialog").showModal();
});
$("choose-file").addEventListener("click", () => $("portfolio-file").click());
$("portfolio-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file || state.busy) return;
  try {
    if (file.size > 10 * 1024 * 1024) throw new Error("invalid_file");
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      throw new Error("invalid_file");
    }
    if (payload?.version !== 1 || !Array.isArray(payload.accounts) || !Array.isArray(payload.holdings))
      throw new Error("invalid_file");
    state.pendingImport = payload;
    $("confirm-import").disabled = false;
    const distinct = new Set(payload.holdings.map((row) => row?.symbol).filter(Boolean)).size;
    $("import-preview").replaceChildren(
      el("strong", file.name),
      el("span", `${payload.accounts.length} 个账户 · ${payload.holdings.length} 条持仓记录 · ${distinct} 个标的`),
      el("span", "点击确认后才会写入新的本地快照。", "muted small"),
    );
  } catch {
    state.pendingImport = null;
    $("confirm-import").disabled = true;
    $("import-preview").replaceChildren(
      el("strong", "无法读取此文件"),
      el("span", "请选择有效的 Capital Desk JSON v1 文件（不超过 10 MB）。", "status-line error"),
    );
  } finally {
    event.target.value = "";
  }
});
$("confirm-import").addEventListener("click", async () => {
  if (!state.pendingImport || state.busy) return;
  busy(true);
  message("import-status", "正在校验并导入持仓…");
  try {
    await api("/api/portfolio/import/capital-desk-v1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.pendingImport),
    });
    await loadPortfolio();
    $("import-dialog").close();
    state.pendingImport = null;
    clearInspection();
    $("answer-state").textContent = "等待研究";
    const ready = el("div", null, "empty-state");
    ready.append(
      el("span", "◎", "empty-mark"),
      el("h3", "持仓已更新，可以开始研究。"),
      el("p", "选择左侧的组合集中风险问题，\n查看当前持仓结构与数据局限。"),
    );
    $("result").replaceChildren(ready);
    message("query-status", "");
    message(
      "import-status",
      "导入成功。新快照已保存在本机，原有快照保持不变。",
    );
  } catch (error) {
    $("import-preview").replaceChildren(
      el("strong", "导入未完成"),
      el("span", error.status === 422
        ? "文件内容未通过 Capital Desk JSON v1 校验，原有持仓未被替换。"
        : "服务暂时未确认导入结果，请关闭窗口并刷新页面核对。", "status-line error"),
    );
    message(
      "import-status",
      error.message === "invalid_file" || error.status === 422
        ? "导入失败：请选择有效的 Capital Desk JSON v1 文件（不超过 10 MB）。原有持仓未被替换。"
        : "导入结果未确认。请刷新页面核对当前快照后再重试。",
      true,
    );
  } finally {
    busy(false);
  }
});
document.querySelectorAll("[data-allocation]").forEach((button) =>
  button.addEventListener("click", () => renderAllocation(button.dataset.allocation)),
);
document.querySelectorAll("[data-query]").forEach((button) =>
  button.addEventListener("click", () => {
    $("query-input").value = button.dataset.query;
    document.querySelectorAll("[data-query]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.query === $("query-input").value)));
    $("query-input").focus();
  }),
);
$("query-input").addEventListener("input", () => document.querySelectorAll("[data-query]").forEach(
  (button) => button.setAttribute("aria-pressed", String(button.dataset.query === $("query-input").value.trim()))));
$("query-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault(); $("query-form").requestSubmit();
  }
});
$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  if (state.researchMode === "cockpit") { await submitCockpit(); return; }
  const query = $("query-input").value.trim();
  if (!query) {
    $("query-input").focus();
    return;
  }
  busy(true);
  clearInspection();
  message("restore-status", "");
  $("answer-state").textContent = "分析中";
  message("query-status", "请求已提交，正在等待服务端分析结果。");
  showLoading("正在检索与校验", "公开数据和模型评估可能需要几十秒；完成后会展示结论与证据。");
  try {
    const answer = await api("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    renderAnswer(answer, query);
    message("query-status", "");
    await loadInspection(answer.run_id);
  } catch (error) {
    failure(error.detail, error.status);
    message("query-status", "分析未完成，请查看提示。", true);
    if (error.detail?.run_id) {
      setRunId(error.detail.run_id);
      await loadInspection(error.detail.run_id);
    }
  } finally {
    stopLoading();
    busy(false);
  }
});
function renderLegacyTdb(record, evidence) {
  state.evidence = evidence;
  setRunId(record.run_id);
  $("evidence-count").textContent = String(evidence.length);
  $("evidence-filter").value = "all";
  renderEvidence();
  $("answer-state").textContent = record.record_kind === "QUALIFIED_REVIEW" ? "决策评估完成" : "需要补充研究";
  const semantic = record.semantic;
  const result = $("result");
  result.replaceChildren();
  result.append(el("h3", `${record.symbol} · ${semantic.what_changed_mode === "FIRST_REVIEW" ? "首次持仓评估" : "相对上次有效复核"}`));
  const priceContext = el("div", null, "tdb-price-context");
  priceContext.append(el("span", "你输入的判断价格 · 非实时行情"),
    el("strong", `$${record.decision_price} USD`),
    el("small", `上次有效收盘价：${record.previous_close ? `$${record.previous_close.price} USD（${date(record.previous_close.as_of)}）` : "未知"} · 价差：${record.price_difference === null ? "未知" : `$${record.price_difference} USD${record.price_difference_pct === null ? "" : `（${Number(record.price_difference_pct) > 0 ? "+" : ""}${record.price_difference_pct}%）`}`}`));
  result.append(priceContext);
  if (record.manifest.thesis?.status === "AI_DRAFT")
    result.append(el("p", "本次复核使用 AI 起草的投资逻辑，尚未经你确认。", "notice warning"));
  if (semantic.action_support) {
    const ranked = ["ADD", "HOLD", "REDUCE", "EXIT"].sort((a, b) => Number(semantic.action_support[b]) - Number(semantic.action_support[a]));
    const top = ranked[0];
    const tied = Number(semantic.action_support[ranked[1]]) === Number(semantic.action_support[top]);
    result.append(el("h4", tied ? "行动假设支持度并列" : `${top} 获得最多相对语义支持`, "tdb-lead"));
    const rationale = semantic.assessment.rationales.find((item) => item.action === top);
    if (!tied && rationale) result.append(el("p", friendlyText(rationale.rationale), "brief-deck"), evidenceLinks(rationale.evidence_ids));
  } else {
    result.append(el("h4", "当前需要补充研究", "tdb-lead"));
  }
  result.append(el("p", friendlyText(semantic.explanation)));
  const changed = semantic.what_changed_mode === "FIRST_REVIEW"
    ? semantic.current_evidence_ids : semantic.new_evidence_ids;
  result.append(el("h4", "发生了什么变化"));
  result.append(el("p", semantic.what_changed_mode === "FIRST_REVIEW"
    ? "这是首次语义复核。以下是当前可用证据，并非“上次以来的新变化”。"
    : changed.length ? "以下证据在上次有效复核后发布，且未在上次冻结证据中出现。" : "本次选取的证据中没有可证明的新材料。"));
  result.append(el("p", semantic.evidence_scope === "CURRENT_30_DAY_NEWS_PLUS_SEC"
    ? "检索范围：最近 30 天公司新闻与相关 SEC 文件。" : semantic.evidence_scope === "HISTORICAL_PERSISTED"
      ? "历史分析仅使用截止时已保存的证据版本。" : "当前没有可用的投资逻辑。", "small muted"));
  result.append(sourceDossier(changed));
  result.append(el("h4", "投资逻辑变化"));
  result.append(el("p", friendlyText(semantic.thesis_delta.rationale || "暂无可依据证据判断的变化。")));
  if (semantic.thesis_delta.evidence_ids.length)
    result.append(evidenceLinks(semantic.thesis_delta.evidence_ids));
  if (semantic.action_support) {
    result.append(el("h4", "Relative Semantic Action Support · 建议支持度"));
    const scores = el("div", null, "tdb-scores");
    for (const action of ["ADD", "HOLD", "REDUCE", "EXIT"])
      scores.append(el("div", `${action} ${semantic.action_support[action]}%`));
    result.append(scores);
    result.append(el("p", "这些百分比是相对语义支持，不是收益、盈利或判断正确的概率，也不是仓位建议。", "small muted"));
    for (const rationale of semantic.assessment.rationales) {
      const detail = el("details");
      detail.append(el("summary", `${rationale.action} · 查看理由`), el("p", friendlyText(rationale.rationale)),
        evidenceLinks(rationale.evidence_ids));
      result.append(detail);
    }
  } else {
    result.append(el("h4", semantic.assessment.assessment_status));
    result.append(el("p", friendlyText(semantic.assessment.abstention_reason)));
  }
  const confidence = {HIGH: "高", MEDIUM: "中", LOW: "低", INSUFFICIENT: "不足"};
  result.append(el("p", `证据把握：${confidence[semantic.assessment.evidence_confidence]} · 推断把握：${confidence[semantic.assessment.inference_confidence]}`, "small muted"));
}
$("back-to-brief").addEventListener("click", () => {
  if (state.researchMode === "cockpit") $("evidence-details").open = false;
  const target = $("result").querySelector(".decision-conclusion") || $("result").querySelector(".conclusion") ||
    $("result").querySelector("h3") || $("result-title");
  target.tabIndex = -1;
  target.focus({preventScroll:true});
  target.scrollIntoView({block:"start"});
});
$("tdb-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const symbol = $("tdb-symbol").value;
  const price = $("tdb-price").value;
  if (!symbol || !price || Number(price) <= 0 || !Number.isFinite(Number(price))) return;
  busy(true);
  clearInspection();
  $("answer-state").textContent = "分析中";
  message("query-status", "正在生成持仓决策简报…");
  showLoading("正在形成持仓决策简报", "正在检索合资格证据并评估行动假设，过程可能需要几十秒。", price);
  try {
    const intent = $("tdb-intent").value;
    const payload = {symbol, decision_price: price, intent};
    if (intent === "HYPOTHETICAL") {
      payload.scenario_cause = $("tdb-cause").value;
      const breaks = payload.scenario_cause === "THESIS_BREAK";
      payload.assumptions = [{assumption_id:"A:user-scenario", kind:breaks ? "ADVERSE_THESIS_PREMISE" : "CAUSE",
        statement:$("tdb-assumption").value.trim(), invalidation_condition_index:null}];
    }
    const {record, evidence} = await executeDecisionJob("/api/decisions/context/jobs", payload);
    renderTdb(record, evidence);
    message("query-status", "");
    if (state.diagnostics) await loadInspection(record.run_id);
  } catch (error) {
    failure(error.detail, error.status);
    message("query-status", "决策简报未完成，请查看提示。", true);
    addDecisionRecovery(error.detail);
    if (error.detail?.run_id && state.diagnostics)
      await loadInspection(error.detail.run_id);
  } finally { stopLoading(); busy(false); }
});
$("evidence-filter").addEventListener("change", renderEvidence);
$("restore-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const id = $("run-input").value.trim();
  if (!id) return;
  busy(true);
  message("restore-status", "正在读取本地分析记录…");
  try {
    const record = await api(`/api/runs/${encodeURIComponent(id)}`);
    clearInspection();
    setRunId(id);
    if (record.answer)
      renderAnswer(record.answer, record.trace?.query || "已保存的研究");
    else {
      if (record.status === "running") {
        $("result").replaceChildren(
          el(
            "p",
            "此记录仍标记为进行中。请稍后重新载入；如果服务曾中断，需重新提交问题。",
          ),
        );
        $("answer-state").textContent = "进行中";
      } else {
        const errors = [
          ...(record.trace?.tool_calls || []),
          ...(record.trace?.model_events || []),
          ...(record.trace?.synthesis ? [record.trace.synthesis] : []),
        ];
        failure(errors.find((entry) => entry.error)?.error);
      }
    }
    await loadInspection(id);
    message("query-status", "");
    message("restore-status", "已载入本地记录。");
  } catch (error) {
    message("restore-status", errorMessage(error.detail, error.status), true);
  } finally {
    busy(false);
  }
});
async function initialize() {
  await Promise.all([
    loadPortfolio(),
    (async () => {
      try {
        const config = await api("/api/console/status");
        const acceptance = $("local-acceptance-status");
        if (acceptance && config.local_acceptance) {
          acceptance.hidden = false;
          acceptance.textContent = `本地早期验收版本 · ${config.local_acceptance.status} · 代码 ${config.local_acceptance.head.slice(0, 8)}。真实账户与历史已保留；工程与产品验收尚未完成。`;
        }
        state.diagnostics = config.diagnostics_enabled;
        $("admin-diagnostics").hidden = !state.diagnostics;
        const modelLabel = config.synthesis_mode === "mock"
          ? "模拟研究"
            : config.synthesis_mode === "unconfigured" ? "尚未配置" : config.model;
        const comparisonLabel = config.comparison_mode === "jev"
          ? `Jev (${config.comparison_model})`
          : config.comparison_mode === "configured"
            ? config.comparison_model
            : "未启用";
        const marketSummary = config.market_mode === "fixture"
          ? "FIXTURE · 示例日终行情" : config.market_mode === "unconfigured"
            ? "未配置日终行情源" : config.market_provider === "massive"
              ? "LIVE · Massive 日终行情" : "已配置日终行情源";
          $("environment").textContent = marketSummary + (config.synthesis_mode === "mock" ? " · MOCK 研究"
            : config.synthesis_mode === "unconfigured" ? " · 未配置研究模型" : "");
        $("environment-detail").textContent = `结论模型：${modelLabel} · 对照评估：${comparisonLabel}。所有时间均显示为 UTC。持仓保存在本机，分析时会向已配置服务发送所需信息。`;
          if(config.public_demo){
            $("environment").textContent="PUBLIC_DEMO · OFFLINE_REPLAY · 共享只读";
            $("environment-detail").textContent="全部资料来自仓库内固定的合成数据集；不发送给金融或模型服务，不保存访客输入。邀请实时研究尚未开放。";
          }
        $("environment").classList.toggle(
          "warning",
            config.market_mode !== "provider" || config.synthesis_mode !== "model",
        );
      } catch {
        $("environment").textContent =
          "运行模式读取失败，请检查本地服务后刷新页面。";
        $("environment").classList.add("warning");
      }
    })(),
  ]);
}
initialize();

$("holding-search").addEventListener("input", () => {
  const term = $("holding-search").value.trim().toUpperCase();
  for (const row of $("aggregate-positions").children) row.hidden = !row.firstElementChild.textContent.toUpperCase().includes(term);
});
document.querySelectorAll(".rail nav a").forEach((link) => link.addEventListener("click", () => {
  document.querySelectorAll(".rail nav a").forEach((item) => item.classList.toggle("active", item === link));
}));
