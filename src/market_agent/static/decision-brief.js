"use strict";

const actionNames = {ADD: "增持", HOLD: "继续持有", REDUCE: "减持", EXIT: "退出"};
const moneyValue = (value) => value === null || value === undefined ? "未知" :
  new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", maximumFractionDigits: 2}).format(value);
const numberValue = (value, suffix = "") => value === null || value === undefined ? "未知" :
  new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(value) + suffix;

function renderDecisionContext(context) {
  const section = el("section", null, "decision-context tdb-price-context");
  section.setAttribute("aria-label", "价格与持仓上下文");
  const grid = el("div", null, "decision-prices");
  const reference = context.market_reference;
  const sessions = {OPEN:"交易中",CLOSED:"休市",AFTER_HOURS:"盘后",PRE_MARKET:"盘前",UNKNOWN:"交易状态未知"};
  for (const [name, value, note] of [
    ["判断价格", moneyValue(context.decision_price_usd), "你输入的决策情景 · 非市场报价"],
    ["最新市场参考", reference ? moneyValue(reference.price) : "暂不可用",
      reference ? `${reference.provider} · ${date(reference.as_of)} · ${sessions[reference.session] || "交易状态未知"} · 可能延迟` :
        context.market_reference_status === "NOT_ENTITLED" ? "当前行情套餐无盘中权限" :
          context.market_reference_status === "HISTORICAL_DISABLED" ? "历史分析不调用当前行情" : "未取得带时间戳的盘中报价"],
    ["历史收盘参考", moneyValue(context.previous_close_usd),
      context.previous_close_as_of ? `${date(context.previous_close_as_of)} · 不是实时市场价` : "缺少有效历史收盘价"],
    ["持仓平均成本", moneyValue(context.average_cost_usd), "跨账户加权成本 · 不代表合理价值"],
  ]) {
    const cell = el("div");
    cell.append(el("span", name), el("strong", value), el("small", note));
    grid.append(cell);
  }
  section.append(grid);
  const facts = el("div", null, "position-context");
  for (const [name, value] of [
    ["判断价相对市场参考", numberValue(context.market_reference_difference_pct, "%")],
    ["判断价相对历史收盘", numberValue(context.price_difference_pct, "%")],
    ["持仓数量", numberValue(context.quantity, " 股")],
    ["判断价下持仓价值", moneyValue(context.scenario_value_usd)],
    ["判断价下浮动盈亏", `${moneyValue(context.scenario_unrealized_pnl_usd)} / ${numberValue(context.scenario_unrealized_pnl_pct, "%")}`],
    ["该股占股票总成本", numberValue(context.portfolio_cost_exposure_pct, "%")],
    ["情景净值权重", numberValue(context.scenario_nav_weight_pct, "%")],
  ]) {
    const cell = el("div"); cell.append(el("span", name), el("b", value)); facts.append(cell);
  }
  section.append(facts, el("p", "按你输入的判断价确定性计算。成本占比不是市值仓位；完整组合缺价时，净值权重保持未知。", "small muted"));
  return section;
}

function groundedParagraph(statement, className) {
  const group = el("div", null, className || "decision-statement");
  group.append(el("p", statement.text), evidenceLinks(statement.evidence_ids));
  return group;
}

const scenarioCauses = {PRICE_ONLY_NO_NEW_DAMAGE:"仅价格变化，未新增公司损害", MARKET_OR_SECTOR_RISK_OFF:"市场或行业风险偏好下降",
  VALUATION_COMPRESSION:"估值压缩假设",COMPANY_SPECIFIC_DAMAGE:"公司经营损害假设",THESIS_BREAK:"投资逻辑失效假设",OTHER_EXPLICIT_ASSUMPTION:"明确自定义假设"};

function conditionedParagraph(statement, className) {
  const group = groundedParagraph(statement, className);
  if (statement.assumption_ids.length) {
    const link = el("a", "所用情景假设", "assumption-link"); link.href = "#scenario-assumptions";
    group.append(link);
  }
  return group;
}

function renderScenario(packet, judgment) {
  const section = el("section", null, "scenario-context");
  section.id = "scenario-assumptions"; section.tabIndex = -1;
  section.append(el("h4", packet.intent === "HYPOTHETICAL" ? "本次假设" : "本次事实边界"));
  if (packet.intent === "HYPOTHETICAL") {
    section.append(el("p", scenarioCauses[packet.scenario_cause], "scenario-cause"),
      el("p", "情景假设，不是已发生事实。只用于本次条件式判断，不更新投资逻辑或当前决策历史。", "scenario-truth-note"));
    const list = el("ul", null, "scenario-assumptions");
    for (const assumption of packet.assumptions) {
      const row = el("li"); row.append(el("span", "情景假设", "assumption-tag"), el("p", assumption.statement));
      if (assumption.invalidation_condition_index !== null) {
        const condition = packet.invalidation_conditions.find(c=>c.index===assumption.invalidation_condition_index);
        if (condition) row.append(el("small", `假设触发的已有条件：${condition.text}`));
      }
      list.append(row);
    }
    section.append(list);
  } else {
    section.append(el("p", "基于截止时合资格的实际公司材料。判断价由你输入，价格变化不等于公司逻辑变化。", "small muted"));
  }
  section.append(el("span", "模型判断", "judgment-label"), conditionedParagraph(judgment.scenario_reasoning));
  return section;
}

function renderBindingConstraints(packet, judgment) {
  const section = el("section", null, "binding-constraints");
  section.append(el("h4", "哪些约束仍在起作用"));
  for (const statement of judgment.binding_constraints) section.append(conditionedParagraph(statement));
  const table = el("dl", null, "constraint-metrics");
  for (const metric of packet.portfolio_constraints) {
    const row = el("div");
    row.append(el("dt", metric.metric === "POSITION_NAV_WEIGHT_PCT" ? "判断价下净值权重" : "股票成本占比"),
      el("dd", numberValue(metric.value, "%")));
    row.append(el("small", metric.metric === "POSITION_NAV_WEIGHT_PCT" ?
      "分子：该股判断价下的持仓价值。分母：该股情景价值、其他有效收盘价下的持仓价值及现金。" :
      "分子：该股全部购入成本。分母：组合全部股票购入成本，不含现金。成本占比不是市值权重。"));
    if (metric.missing_symbols.length) row.append(el("small", `组合缺价：${metric.missing_symbols.join("、")}。净值分母与权重保持未知。`));
    row.append(el("small", `不可变持仓快照 · ${date(metric.as_of)} · 确定性计算`), evidenceLinks(metric.evidence_ids));
    table.append(row);
  }
  section.append(table);
  section.append(el("p", packet.owner_policy.status === "NOT_CONFIGURED" ?
    "投资政策尚未配置：不默认投资周期、仓位上限或补仓偏好。" : "仅使用本次明确输入的投资偏好，未填写的项目仍未知。", "small muted policy-state"));
  if (packet.owner_policy.single_name_nav_limit_pct !== null) section.append(el("p", `明确输入的单一标的净值权重上限：${numberValue(packet.owner_policy.single_name_nav_limit_pct, "%")}`, "small"));
  return section;
}

function renderMarketStructure(packet) {
  const section = el("section", null, "market-structure");
  section.append(el("h4", "判断价的历史收盘位置"));
  const details = el("details");
  details.append(el("summary", "价格基准、时间与来源"));
  const reasons = {HISTORY_NOT_ENTITLED:"当前行情权限不支持这段历史",HISTORY_CONFIGURATION:"历史行情来源未配置",
    HISTORY_INVALID_DATA:"来源未通过价格基准、交易日或证据校验",HISTORY_TIMEOUT:"历史行情请求超时",
    HISTORY_TRANSPORT:"历史行情连接失败",HISTORY_RATE_LIMIT:"历史行情请求达到限额",
    STALE_COMPLETED_HISTORY:"最近完成收盘数据已过期",INSUFFICIENT_COMPLETED_SESSIONS:"已提供的完成交易日数量不足",
    HISTORICAL_VINTAGE_UNAVAILABLE:"无法证明历史分析时点的数据版本"};
  for (const window of packet.market_structure.windows) {
    const row = el("div", null, "market-window");
    row.append(el("strong", `${window.sessions} 日收盘区间`));
    if (window.status === "AVAILABLE") {
      const location = window.decision_price_range_position_pct === null ? "区间收盘价相同，位置比例未知" : Number(window.decision_price_range_position_pct) < 0 ? "低于区间最低收盘价" : Number(window.decision_price_range_position_pct) > 100 ? "高于区间最高收盘价" : "位于区间内";
      const highLabel = Number(window.decision_price_drawdown_from_close_high_pct) > 0 ? "高于区间收盘高点" : "距区间收盘高点回撤";
      row.append(el("p", `收盘区间 ${moneyValue(window.close_low)} — ${moneyValue(window.close_high)}；判断价${location}，区间位置 ${numberValue(window.decision_price_range_position_pct, "%")}；${highLabel} ${numberValue(window.decision_price_drawdown_from_close_high_pct, "%")}`),
        el("small", `区间首尾收盘变化 ${numberValue(window.window_close_change_pct, "%")} · 确定性计算`));
      const source = el("div"); source.append(el("small", `${window.sessions} 日：${window.start} 至 ${window.end}`), evidenceLinks(window.evidence_ids)); details.append(source);
    } else row.append(el("p", `未知：${reasons[window.reason] || "完成收盘数据不足，或价格基准与公司行动尚未安全验证"}。`, "small muted"));
    section.append(row);
  }
  details.append(el("p", `来源：${packet.market_structure.source}。价格基准：${packet.market_structure.price_basis === "SPLIT_ADJUSTED" ? "拆股调整收盘价；不含股息调整" : packet.market_structure.price_basis === "UNADJUSTED_NO_SPLITS" ? "未复权；已验证无拆股" : "尚未验证"}。排除当前纽约日期，不保证交易所日历完整或历史数据版本。`, "small muted"));
  section.append(el("p", "仅描述已提供的完成收盘区间，不代表支撑、阻力或合理价值。不前填缺价；其他公司行动与股息总回报不在本模型内。基准表现未知。", "small muted"),details);
  return section;
}

function renderTdb(record, evidence) {
  if (record.semantic.version === 1) return renderLegacyTdb(record, evidence);
  state.evidence = evidence; setRunId(record.run_id);
  $("evidence-count").textContent = String(evidence.length);
  $("evidence-filter").value = "all"; renderEvidence();
  const semantic = record.semantic, depth = semantic.depth;
  const packet = record.investment_context, conditioned = record.conditional_assessment;
  const hypothetical = packet?.intent === "HYPOTHETICAL";
  const context = evidence.find((i) => i.evidence_id === semantic.context_evidence_id)?.structured_data?.decision_context;
  const result = $("result"); result.replaceChildren();
  const assessed = !!semantic.action_support;
  $("answer-state").textContent = assessed ? "决策评估完成" : "需要补充研究";
  const header = el("div", null, "decision-heading");
  header.append(el("h3", record.symbol), el("span", packet ? hypothetical ? "HYPOTHETICAL · 情景预演" : "CURRENT · 当前决策" :
    semantic.what_changed_mode === "FIRST_REVIEW" ? "首次持仓评估" : "持仓变化复核", hypothetical ? "tag assumption-tag" : "tag"));
  result.append(header);
  if (assessed) {
    const ranked = ["ADD", "HOLD", "REDUCE", "EXIT"].sort((a, b) => Number(semantic.action_support[b]) - Number(semantic.action_support[a]));
    const tied = semantic.action_support[ranked[0]] === semantic.action_support[ranked[1]];
    if (conditioned) result.append(conditionedParagraph(conditioned.headline, "decision-conclusion tdb-lead"));
    else {
      result.append(el("h4", tied ? "行动支持并列，需结合证据权衡" : `当前更偏向${actionNames[ranked[0]]}`, "decision-conclusion tdb-lead"));
      if (depth) result.append(groundedParagraph(depth.headline, "decision-headline"));
    }
  } else {
    result.append(el("h4", "先验证关键证据，再决定行动", "decision-conclusion tdb-lead"),
      el("p", semantic.assessment.abstention_reason, "brief-deck"));
  }
  if (packet && conditioned) result.append(renderScenario(packet, conditioned));
  if (context) result.append(renderDecisionContext(context));
  if (depth) {
    const price = el("section", null, "price-meaning");
    price.append(el("h4", "为什么这个价格重要"), groundedParagraph(depth.price_implication)); result.append(price);
  }
  if (packet) result.append(renderMarketStructure(packet));
  if (packet && conditioned) result.append(renderBindingConstraints(packet, conditioned));
  if (assessed) {
    const section = el("section", null, "action-comparison");
    section.append(el("h4", "四种行动，如何权衡"),
      el("p", "相对建议支持度 · 不是上涨概率，也不是建议仓位", "small muted support-boundary"));
    for (const rationale of semantic.assessment.rationales) {
      const row = el("div", null, "action-row");
      const labelNode = el("div", null, "action-label");
      labelNode.append(el("strong", actionNames[rationale.action]));
      if (!packet) labelNode.append(el("span", `${semantic.action_support[rationale.action]}%`, "action-score"));
      row.append(labelNode, groundedParagraph({text: rationale.rationale, evidence_ids: rationale.evidence_ids}));
      const effect = semantic.price_assessment?.effects.find(e=>e.action===rationale.action);
      if (effect) {
        const direction = {FAVORS:"提供支持",DISFAVORS:"削弱支持",NOT_DECISIVE:"不足以定论",UNKNOWN:"影响未知"};
        const strength = {LIMITED:"影响有限",MEANINGFUL:"影响较明显",NONE:"无明显影响",UNKNOWN:"强弱未知"};
        row.append(el("p", `价格贡献：${direction[effect.direction]} · ${strength[effect.strength]}`, "small muted price-action-effect"));
      }
      section.append(row);
    }
    section.append(el("p", "建议支持度是相对语义支持，不是收益、盈利或判断正确的概率，也不是建议仓位比例。", "small muted"));
    result.append(section);
    if (packet) {
      const ranked = ["ADD", "HOLD", "REDUCE", "EXIT"].sort((a,b)=>Number(semantic.action_support[b])-Number(semantic.action_support[a]));
      const tied = semantic.action_support[ranked[0]] === semantic.action_support[ranked[1]];
      section.prepend(el("p", tied ? "最高行动支持并列，需继续权衡依据。" :
        `${hypothetical ? "假设成立时" : "当前证据下"}更偏向${actionNames[ranked[0]]}；备选${actionNames[ranked[1]]}。`, "primary-action-summary"));
      const disclosure = el("details", null, "support-disclosure");
      disclosure.append(el("summary", "查看相对建议支持度"));
      for (const action of ranked) disclosure.append(el("p", `${actionNames[action]} ${semantic.action_support[action]}%`));
      disclosure.append(el("p", "相对语义支持，不是收益、盈利或判断正确的概率，也不是仓位。", "small muted"));
      result.append(disclosure);
    }
    if (semantic.price_assessment?.invariance_rationale) {
      const stable = el("section", null, "price-invariance");
      stable.append(el("h4", "价格变化后，哪些约束仍在"), groundedParagraph(semantic.price_assessment.invariance_rationale));
      result.append(stable);
    }
  }
  if (depth) {
    const columns = el("div", null, "decision-columns");
    const thesis = el("section"), risks = el("section");
    thesis.append(el("h4", hypothetical ? "本次条件下的投资逻辑" : "当前投资逻辑"), groundedParagraph(depth.baseline_thesis),
      el("h4", "经营质量与预期"), groundedParagraph(depth.business_quality));
    risks.append(el("h4", "风险与反向证据"));
    for (const risk of depth.key_risks) risks.append(groundedParagraph(risk));
    columns.append(thesis, risks); result.append(columns);
    const conditions = el("section", null, "decision-conditions");
    conditions.append(el("h4", "什么会改变这个判断"));
    if (conditioned) conditions.append(conditionedParagraph(conditioned.change_conditions));
    for (const condition of depth.change_my_mind) conditions.append(groundedParagraph(condition));
    conditions.append(el("h4", "下一次最值得验证"), groundedParagraph(depth.next_verification)); result.append(conditions);
  }
  const research = semantic.research;
  if (research?.completion_attempted || research?.remaining_gaps?.length || depth?.missing_context?.length) {
    const section = el("section", null, "research-boundary");
    section.append(el("h4", assessed ? "研究边界" : "已查什么，还缺什么"),
      el("p", research?.completion_attempted ? "已检查公司新闻与官方披露，并针对证据缺口完成一轮有目标的补充检索。" : "已检查本次可用公司新闻与官方披露。"));
    if (research?.provider_errors?.length) section.append(el("p", "部分数据源没有完成请求；以下判断已保留这一限制。", "small muted"));
    for (const gap of new Set([...(research?.remaining_gaps || []).map(g=>g.description), ...(depth?.missing_context || [])]))
      section.append(el("p", gap));
    if (research?.supplemental_evidence_ids?.length) section.append(evidenceLinks(research.supplemental_evidence_ids));
    result.append(section);
  }
  const finance = evidence.filter((i) => i.structured_data?.metrics);
  if (finance.length) {
    const details = el("details", null, "decision-financials"); details.append(el("summary", "查看官方财务事实与报告期间"));
    const names = {revenue: "收入", net_income: "净利润", operating_cash_flow: "经营现金流", cash: "现金及等价物"};
    for (const item of finance) {
      for (const metric of item.structured_data.metrics) {
        details.append(el("p", `${names[metric.metric]} · ${moneyValue(metric.value)} · ${metric.start || "时点"} 至 ${metric.end}`));
        if (metric.accession_number) details.append(el("small", `原始报告 ${metric.accession_number} · 可用时点 ${date(metric.available_at)}`));
      }
      details.append(el("p", "报告期间可能不同，不直接混算利润率、增长率或估值。", "small muted"), evidenceLinks([item.evidence_id]));
    }
    result.append(details);
  }
  if (packet) {
    result.append(el("p", `实际投资逻辑状态：${{UNKNOWN:"未知",NOT_TRIGGERED:"本次实际证据未支持失效",INVALIDATION_SUPPORTED:"实际证据支持命名条件失效"}[conditioned.actual_thesis_state]}。${hypothetical ? "假设触发与实际状态分开记录。" : ""}`, "small muted actual-thesis-state"));
  }
  const changed = el("details", null, "decision-history");
  changed.append(el("summary", semantic.what_changed_mode === "FIRST_REVIEW" ? "基线与证据记录" : "上次以来，发生了什么变化"),
    el("p", semantic.thesis_delta.rationale));
  if (semantic.what_changed_mode === "FIRST_REVIEW") changed.append(el("p", "这是首次决策基线，没有历史判断差异。AI 投资逻辑尚未经你确认。", "small muted"));
  changed.append(sourceDossier(semantic.what_changed_mode === "FIRST_REVIEW" ? semantic.current_evidence_ids : semantic.new_evidence_ids)); result.append(changed);
  const confidence = {HIGH: "高", MEDIUM: "中", LOW: "低", INSUFFICIENT: "不足"};
  result.append(el("p", `证据把握 ${confidence[semantic.assessment.evidence_confidence]} · 推断把握 ${confidence[semantic.assessment.inference_confidence]} · 截至 ${date(record.analysis_cutoff)}`, "decision-footer small muted"));
}

async function updateScenarioForm() {
  const hypothetical = $("tdb-intent").value === "HYPOTHETICAL";
  const breaks = hypothetical && $("tdb-cause").value === "THESIS_BREAK";
  $("tdb-scenario").hidden = !hypothetical;
  $("tdb-assumption").required = hypothetical;
  $("tdb-invalidation").hidden = !breaks;
  $("tdb-condition").required = false;
  $("tdb-condition").disabled = true;
  $("tdb-assumption").placeholder = breaks ? "例如：假设广告变现机制持续失效，原现金创造机制遭到破坏。请独立描述负面业务变化。" : "例如：市场压缩，但公司经营没有新增损害。";
  if (!breaks || !$("tdb-symbol").value) return;
  const selectedSymbol = $("tdb-symbol").value;
  $("tdb-condition").replaceChildren(el("option", "正在读取已有失效条件…"));
  $("tdb-condition").firstElementChild.value = "";
  try {
    const options = await api(`/api/decisions/context/options/${encodeURIComponent(selectedSymbol)}`);
    if ($("tdb-symbol").value !== selectedSymbol) return;
    $("tdb-condition").replaceChildren();
    if (!options.invalidation_conditions.length) {
      const option = el("option", "当前未保存明确的负面失效条件"); option.value = "";
      $("tdb-condition").append(option);
    }
    for (const condition of options.invalidation_conditions) {
      const option = el("option", condition.text); option.value = String(condition.index); $("tdb-condition").append(option);
    }
  } catch (error) {
    const option = el("option", "条件读取失败，请重新选择标的。"); option.value = ""; $("tdb-condition").replaceChildren(option);
  }
}
for (const id of ["tdb-intent", "tdb-cause", "tdb-symbol"]) $(id).addEventListener("change", updateScenarioForm);

const stageNames = {queued: "等待研究开始", portfolio: "读取持仓与判断价", context: "建立价格与持仓上下文",
  research: "检查公司证据", completion: "补充关键证据", judge: "正在比较四种行动与价格条件", brief: "保存决策简报"};

function renderDecisionProgress(job) {
  let shell = $("result").querySelector(".decision-progress");
  if (!shell) {
    shell = el("div", null, "decision-progress loading-state");
    shell.append(el("h3", "正在形成持仓决策简报"), el("div", null, "progress-context"),
      el("ol", null, "decision-stages"), el("div", null, "decision-skeleton"));
    $("result").replaceChildren(shell);
  }
  if (job.context && !shell.querySelector(".decision-context"))
    shell.querySelector(".progress-context").append(renderDecisionContext(job.context));
  const stages = shell.querySelector(".decision-stages"); stages.replaceChildren();
  for (const stage of [...new Set([...job.completed_stages, job.stage])]) {
    if (!stageNames[stage]) continue;
    const done = job.completed_stages.includes(stage);
    const row = el("li", stageNames[stage], done ? "stage-done" : "stage-active");
    if (!done) row.setAttribute("aria-current", "step");
    stages.append(row);
  }
  if (job.evidence_count !== null && job.evidence_count !== undefined)
    stages.append(el("li", `已冻结 ${job.evidence_count} 项公司证据`, "stage-note"));
}

async function executeDecisionJob(path, payload) {
  const start = await api(path, {method: "POST", headers: {"Content-Type": "application/json"},
    ...(payload ? {body: JSON.stringify(payload)} : {})});
  return observeDecisionJob(start.job_id);
}

async function observeDecisionJob(jobId) {
  let lastContext = null;
  async function read(path) {
    for (let attempt=0; attempt<2; attempt++) {
      try { return await api(path); }
      catch (error) {
        const transient = !error.status || [502,503,504].includes(error.status);
        if (transient && attempt===0) {
          message("query-status", "连接短暂中断，正在继续查看同一次研究；不会重新检索。");
          await new Promise(resolve=>setTimeout(resolve,650));
          continue;
        }
        if (error.status!==404) error.detail = {code:"DECISION_OBSERVATION_LOST",job_id:jobId,context:lastContext};
        throw error;
      }
    }
  }
  while (true) {
    const job = await read(`/api/decisions/jobs/${encodeURIComponent(jobId)}`);
    if (!job || !["running","error","success"].includes(job.status)) {
      const error=new Error("Invalid job observation");
      error.detail={code:"DECISION_OBSERVATION_LOST",job_id:jobId,context:lastContext}; throw error;
    }
    lastContext = job.context || lastContext;
    if (job.status === "error") {
      const error = new Error("Decision stage failed"); error.status = 503;
      error.detail = {...job.error, context: job.context}; throw error;
    }
    if (job.status === "success") {
      const record = await read(`/api/decisions/${encodeURIComponent(job.decision_id)}`);
      const evidence = await read(`/api/decisions/${encodeURIComponent(job.decision_id)}/evidence`);
      return {record, evidence};
    }
    renderDecisionProgress(job);
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
}

function addDecisionRecovery(detail) {
  const observing = detail?.code === "DECISION_OBSERVATION_LOST" && detail?.job_id;
  if (!observing && (detail?.code !== "DECISION_JUDGE_FAILED" || !detail?.run_id)) return;
  if (detail.context) $("result").append(renderDecisionContext(detail.context));
  const button = el("button", observing ? "继续查看本次研究" : "复用证据，重新生成判断", "primary recovery-button");
  const note = el("p", observing ? "连接中断不代表研究失败。继续查看原任务，不会创建新的研究或模型请求。" :
    "持仓、判断价和证据已冻结。仅重试判断阶段，不重新检索；结果仍对应原分析时点。", "small muted");
  $("result").append(note, button);
  button.addEventListener("click", async () => {
    if (state.busy) return;
    busy(true);
    try {
      const {record, evidence} = observing ? await observeDecisionJob(detail.job_id) :
        await executeDecisionJob(`/api/decisions/recover/${encodeURIComponent(detail.run_id)}`);
      renderTdb(record, evidence);
      message("query-status", "");
    } catch (error) { failure(error.detail, error.status); addDecisionRecovery(error.detail); }
    finally { busy(false); }
  });
}
