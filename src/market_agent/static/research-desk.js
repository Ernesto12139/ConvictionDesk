"use strict";
(() => {
  const publicReplay=document.body.dataset.runtimeMode==="PUBLIC_DEMO";
  let demoManifest=null;
  let active = null, viewGeneration = 0, listGeneration = 0, sending = false;
  let accountsSnapshot = null;
  let historicalView = false;
  let attentionDraft = null;
  function clearAttentionDraft() {
    attentionDraft=null;$("desk-attention-context").hidden=true;
  }
  let renderKey = "", sourceKey = null, readingReturn = null, observedSince = Date.now();
  const identity = {VERIFIED_FACT:"已检索事实 / 确定性计算", USER_ASSERTION:"用户前提 · 未独立核实",
    AI_DRAFT:"AI 研究判断 / 草稿", HISTORICAL_DECISION:"历史决策记录", OWNER_CONFIRMED_THESIS:"已确认投资逻辑", UNKNOWN:"待核查"};
  const objectives = {CONSIDER_ADD:"分批加仓研究", CONSIDER_REDUCE:"减仓研究", EXPLAIN_CHANGE:"变化原因研究",
    REVIEW_THESIS:"投资逻辑复核", EARNINGS_CHECK:"下次财报研究", UNSUPPORTED:"待明确问题"};
  const statuses = {RUNNING:"调查中", COMPLETE:"研究完成 · 已保存", FAILED:"本轮未完成", NEEDS_INPUT:"需要补充"};
  const money = value => value == null ? "未知" : new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"}).format(Number(value));
  const percent = value => value == null ? "未知" : new Intl.NumberFormat("en-US", {style:"percent", maximumFractionDigits:2, minimumFractionDigits:2}).format(Number(value));
  const stamp = value => value ? date(value) : "时间未知";
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const local = value => {
    const parsed=new Date(value);
    return value && !Number.isNaN(parsed.getTime()) ? new Intl.DateTimeFormat("zh-CN",{timeZone:zone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(parsed) : "时间未知";
  };
  const localZone = value => value ? `${local(value)} · ${zone}` : "时间未知";
  // A completed close is labelled by its trading day only when the source records one.
  function closeTiming(task) {
    const source=task.sources.find(s=>s.evidence.evidence_id===task.premise?.completed_close_evidence_id);
    const day=source?.evidence.structured_data?.session_date;
    if(day)return {label:`交易日 ${day} · 日终收盘`,day};
    const at=task.premise?.completed_close_as_of;
    // Display in New York time (the listing market) without inferring a session.
    const ny=at ? new Intl.DateTimeFormat("zh-CN",{timeZone:"America/New_York",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(at)) : null;
    return {label:ny ? `行情时点 ${ny} 美东 · 交易日未记录` : "价格时点未记录",day:null};
  }
  const motion = () => matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
  async function readApi(path) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try { return await api(path, {signal:controller.signal}); } finally { clearTimeout(timer); }
  }
  const node = (tag, text, cls) => {const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls;return n;};
  const errorMessage = (error, task=null) => {
    const code = error?.detail?.code || error?.error || error || "";
    if(code === "configuration") return "研究模型尚未配置，本轮没有生成判断。配置后可继续同一任务。";
    if(code === "RESEARCH_INTERRUPTED") return "服务中断了这轮调查。任务与已查来源已保留，可继续研究。";
    if(code === "RESEARCH_TASK_BUSY") return "这次研究仍在进行，请先刷新任务进度。";
    if(code === "RESEARCH_MODEL_PROFILE_UNAVAILABLE") return "所选研究模型尚未配置服务端 Key；本次没有创建研究任务。";
    if(code === "RESEARCH_MODEL_PROFILE_INVALID") return "所选研究方式与服务端模型配置不匹配；请刷新可用选项后重试。";
    if(code === "RESEARCH_CONTEXT_CHANGED") return "这个任务绑定原持仓和账户范围；更换范围请从首页开始新研究。";
    if(code === "RESEARCH_TASK_INTEGRITY") return "任务记录未通过完整性检查，已停止读取。请检查服务端记录。";
    if(["ATTENTION_ITEM_NOT_FOUND","RESEARCH_ATTENTION_CONTEXT_INVALID","attention_context_mismatch"].includes(code)) return "选中的关注事项与本次研究不匹配或已不可读取，研究已停止。请返回今日关注重新选择，或取消关联后提出新问题。";
    const phase=task?.model_events?.filter(event=>event.status==="error").at(-1)?.transport_attempts?.at(-1)?.timeout_phase;
    const timeoutReason={connect:"模型连接超时",read:"模型响应读取超时",write:"模型请求发送超时",pool:"模型连接池等待超时"}[phase] || "模型请求超时（阶段未记录）";
    const reasons = {timeout:timeoutReason, RESEARCH_MODEL_TIMEOUT:"模型阶段超过执行预算", authentication:"模型认证失败", rate_limit:"服务请求受限", transport:"服务连接失败", provider_unavailable:"服务暂不可用", schema_validation:"模型结果不符合数据契约", unsupported_numeric_fact:"结果包含无法由引用核实的数字或报告期间", invalid_evidence_reference:"结果引用了未提供的证据", stale_current_claim:"较旧资料不足以支持当前事实判断", unsupported_source_identity:"来源身份与判断不一致", unauthorized_trade_quantity:"结果包含未经授权的交易股数", session_call_limit:"本轮真实模型调用已达到上限", RESEARCH_TIMEOUT:"调查超过本轮执行预算"};
    return `${reasons[code] || "本轮研究未完成"}（${code || "原因待核查"}）。任务和来源已保留，未生成新的行动判断；可在执行记录中查看原因。`;
  };
  function buttons(task) {
    const running=task?.status === "RUNNING" || historicalView;
    $("desk-continue").disabled=running || sending;
    $("desk-earnings").disabled=running || sending || !task?.focus_symbol;
    $("desk-followup").disabled=running || sending;
    $("desk-retry").disabled=running || sending;
    $("desk-retry-options").hidden=task?.status!=="FAILED" || historicalView;
    $("desk-next-draft").disabled=running || sending;
    // A failed round has one primary path: recover reading, then an explicit retry.
    const failed=task?.status==="FAILED" && !historicalView;
    $("desk-continue").classList.toggle("primary",!failed);
    $("desk-retry").classList.toggle("primary",failed);
    if(publicReplay){
      const next=demoManifest?.examples.find(e=>e.task_id===task?.task_id)?.followup_revision;
      $("desk-followup").readOnly=true;
      $("desk-followup").disabled=false;
      $("desk-followup").value=next && task.revision<next ? "那它下次财报需要重点看什么？" : "当前仅提供已保存的合成研究回放。";
      $("desk-continue").disabled=!(next && task.revision<next);
      $("desk-continue").textContent=next && task.revision<next ? "查看已保存的财报追问" : "已到本例回放最后一轮";
      $("desk-earnings").hidden=true;$("desk-next-draft").hidden=true;$("desk-retry").hidden=true;
      document.querySelector(".desk-fee-note").textContent="只读合成回放；查看追问不会创建任务或调用模型。";
      $("desk-action-hint").textContent="新研究与失败重试不在本版公开回放范围。";
    }
  }
  function closeEvidence() {
    $("desk-evidence-dialog").close();
    if(readingReturn?.isConnected)readingReturn.focus({preventScroll:true});
  }
  function openEvidence(ids=[], invoker=document.activeElement) {
    readingReturn=invoker;
    const dialog=$("desk-evidence-dialog");
    $("desk-evidence-scope").textContent=`第 ${active.revision} 轮 · 研究截止 ${localZone(active.cutoff)}。用户前提、检索事实与 AI 判断分别标记。`;
    $("desk-sources").open=true;
    if(!dialog.open)dialog.showModal();
    const target=Array.from($("desk-source-list").children).find(n=>ids.includes(n.dataset.sourceId));
    if(target){target.open=true;target.focus({preventScroll:true});target.scrollIntoView({block:"start",behavior:"instant"});}
  }
  const freshnessLabels={CURRENT:"本轮可用材料",STALE:"较旧材料",HISTORICAL:"历史背景",NOT_VERIFIED:"用户前提 · 未核实",UNKNOWN:"时效待核查",FUTURE:"晚于研究截止时间"};
  const sourceKinds={news:"公司新闻",sec_filing:"公司披露",market_data:"行情",calculation:"持仓计算",portfolio_snapshot:"持仓快照",thesis:"历史投资逻辑",semantic_judgment:"研究背景"};
  const sourceTitle=e=>e.title && !["AI_DRAFT",e.evidence_type].includes(e.title) ? e.title : sourceKinds[e.evidence_type]||"研究来源";
  function sourceChip(source){
    const chip=node("span",freshnessLabels[source.freshness]||"时效待核查","desk-fresh");chip.dataset.freshness=source.freshness||"UNKNOWN";return chip;
  }
  function timeStrip(task){
    const strip=node("dl",null,"desk-timebar");strip.setAttribute("aria-label","研究时点与资料时效");
    const company=task.sources.filter(s=>["news","sec_filing"].includes(s.evidence.evidence_type));
    const current=company.filter(s=>s.freshness==="CURRENT").length;
    const timing=closeTiming(task);
    const cells=[["研究截止",localZone(task.cutoff)],
      ["价格参考",task.premise?.completed_close ? `${money(task.premise.completed_close)} · ${timing.label}` : task.status==="COMPLETE" ? "本轮没有合格收盘价" : "未取得"],
      ["公司资料",company.length ? `${company.length} 条 · ${current} 条本轮可用${company.length-current ? ` · ${company.length-current} 条较旧/历史` : ""}` : "本轮没有公司资料"]];
    for(const [label,value] of cells){const cell=node("div");cell.append(node("dt",label),node("dd",value));strip.append(cell);}
    return strip;
  }
  function firstSentence(text) {
    const match=text.match(/^.*?[。！？](?:[”」])?/s);
    return match ? match[0] : text;
  }
  function overview(title, statement, preserveFull=false) {
    const section=node("section",null,"desk-memo-section desk-overview");section.append(node("h3",title));
    if(!statement){section.append(node("p","本轮没有合格材料。","muted"));return section;}
    section.append(node("p",preserveFull ? statement.text : firstSentence(statement.text),"desk-statement"));
    if(statement.evidence_ids.length){const button=node("button",`依据 ${statement.evidence_ids.length} 条`,"desk-source-button");button.type="button";
      button.addEventListener("click",()=>openEvidence(statement.evidence_ids,button));section.append(button);}
    if(!preserveFull && firstSentence(statement.text)!==statement.text){const details=node("details",null,"desk-full-statement");details.append(node("summary","完整说明与限定条件"),citation(statement));section.append(details);}
    return section;
  }
  function citation(statement) {
    const p=node("p",statement.text,"desk-statement");
    for(const id of statement.evidence_ids) {
      const index=active.sources.findIndex(source=>source.evidence.evidence_id===id);
      const source=active.sources[index];
      const button=node("button",`[${index+1}]`,"desk-citation");button.type="button";
      button.title=`${source?.evidence.title || id} · ${identity[statement.identity]}`;
      button.setAttribute("aria-label",`查看来源 ${index+1}：${source?.evidence.title || id}`);
      button.addEventListener("click",()=>{
        openEvidence([id],button);
      });p.append(button);
    }
    return p;
  }
  function group(title, statements) {
    const section=node("section",null,"desk-memo-section");section.append(node("h3",title));
    if(!statements.length)section.append(node("p","本轮没有合格的支持材料。","muted"));
    statements.forEach(s=>section.append(citation(s)));return section;
  }
  // Observable research depth from the saved task: budgets, tools and the one
  // Deep gap check. No model self-report; "no gain" is shown as no gain.
  function researchDepth(task) {
    const deep=task.mode!=="FAST", manifest=task.context_manifest || {}, investigation=task.investigation || {};
    const section=node("section",null,"desk-memo-section desk-depth");
    section.append(node("h3",deep ? "研究深度 · 深入研究 DEEP" : "研究深度 · 快速判断 FAST"));
    section.append(node("p",deep ? "针对一个关键证据缺口展开定向补查；纳入更大的资料包与确定性季度财务。" : "有界快速判断：较小资料包与输出预算，不做补查，不读取季度财务。","small muted"));
    const labels={NOT_NEEDED:"未选择补查缺口",SKIPPED_FAST_BUDGET:"快速判断预算内不补查",PLANNED:"已计划补查",NEW_EVIDENCE:"补查取得新证据",NO_NEW_EVIDENCE:"补查未取得新证据 · no material evidence gain",UNAVAILABLE:"补查来源不可用",SKIPPED_EXISTING_EVIDENCE:"现有证据已覆盖，未补查"};
    const values=[["资料包",manifest.max_context_bytes ? `${manifest.context_bytes} / ${manifest.max_context_bytes} 字节` : "未记录"],
      ["纳入来源",`${(manifest.selected || []).length || task.sources.length} 条 · 共保存 ${task.sources.length} 条`],
      ["工具调用",`${task.tool_calls.length} 次`],["模型调用",`${task.model_events.length} 次 · ${task.model_events.at(-1)?.model || "未记录"}`],
      ["定向补查",labels[investigation.status] || "未记录"]];
    const grid=node("dl",null,"desk-diagnostic-metrics");
    for(const [title,value] of values){const cell=node("div");cell.append(node("dt",title),node("dd",value));grid.append(cell);}
    section.append(grid);
    if(investigation.gap?.material_question)section.append(node("p","关键缺口："+investigation.gap.material_question,"small"));
    if(investigation.status==="NEW_EVIDENCE")section.append(node("p",`新增证据 ${investigation.new_evidence_ids.join("、")}；补查前判断已保存，可在完整报告中对照。`,"small muted"));
    return section;
  }
  function researchGain(task) {
    // Legacy payloads omit the schema's default DEEP value; absence is not FAST.
    if(task.mode==="FAST")return null;
    const investigation=task.investigation || {}, section=node("section",null,"desk-memo-section desk-information-gain");
    section.append(node("h3","补查新增了什么"));
    const labels={NEW_EVIDENCE:"补查取得了新证据；新增资料不等于风险已排除。",NO_NEW_EVIDENCE:"补查未取得新的合格证据，原有缺口仍需核查。",UNAVAILABLE:"补查来源不可用，没有新增可核实证据。",NOT_NEEDED:"本轮未选择额外补查，不能据此声称风险已排除。",SKIPPED_EXISTING_EVIDENCE:"本轮采用已有证据，没有额外补查。",PLANNED:"补查已计划，尚未形成新增证据。"};
    section.append(node("p",labels[investigation.status] || "本轮未记录补查结果，信息增益未确认。","small"));
    if(investigation.gap?.material_question)section.append(node("p","核查问题："+investigation.gap.material_question,"small muted"));
    for(const id of investigation.new_evidence_ids || []){
      const source=task.sources.find(s=>s.evidence.evidence_id===id);
      if(source){const button=node("button",sourceTitle(source.evidence),"desk-source-button");button.type="button";button.addEventListener("click",()=>openEvidence([id],button));section.append(button);}
    }
    return section;
  }
  function financialHistory(task) {
    const receipts=task.sources.filter(s=>s.identity==='VERIFIED_FACT' && s.evidence.provider==='python_decimal' && s.evidence.structured_data?.calculation==='sec_quarterly_v1');
    if(!receipts.length)return null;
    const section=node('section',null,'desk-memo-section');section.append(node('h3','最近已披露季度 · 确定性财务'));
    section.append(node('p','原始 USD 金额；每项均保留报告期间与计算来源。空缺表示未知；非公司指引，也不代表实时经营。','small muted'));
    const wrap=node('div',null,'desk-financial-scroll'),table=node('table');table.setAttribute('aria-label','最近披露季度财务');
    const head=node('tr');['报告期间','收入 USD','净利润 USD','经营现金流 USD','FCF USD','SBC USD','摊薄 EPS USD/股','依据'].forEach(text=>head.append(node('th',text)));table.append(head);
    for(const source of receipts.slice(0,4)){
      const q=source.evidence.structured_data.quarter,metrics=Object.fromEntries(q.metrics.map(m=>[m.metric,m]));
      const row=node('tr');row.append(node('th',`${q.start} — ${q.end}`));
      for(const name of ['revenue','net_income','operating_cash_flow','free_cash_flow','sbc','diluted_eps']){
        const metric=metrics[name];
        // Group the exact Decimal string for reading; do not round or calculate in JS.
        const raw=metric?.value, parts=typeof raw==='string' ? raw.match(/^(-?)(\d+)(\.\d+)?$/) : null;
        const display=parts ? parts[1]+'$'+parts[2].replace(/\B(?=(\d{3})+(?!\d))/g,',')+(parts[3] || '') : raw || '未知';
        const cell=node('td',display);
        if(metric)cell.title=`原始 Decimal：${raw} · `+(metric.basis==='REPORTED' ? '正式披露单季值' : metric.basis==='YTD_DIFFERENCE' ? 'Python：累计值相减；报告版本匹配' : 'Python：经营现金流减资本开支');
        row.append(cell);
      }
      const cell=node('td'),button=node('button','期间与来源','desk-source-button');button.type='button';button.addEventListener('click',()=>openEvidence([source.evidence.evidence_id],button));cell.append(button);row.append(cell);table.append(row);
    }
    wrap.append(table);section.append(wrap);return section;
  }
  function leadingReasonIndex(task) {
    const reasons=task.assessment.key_reasons;
    const company=reasons.findIndex(statement=>["VERIFIED_FACT","AI_DRAFT"].includes(statement.identity) &&
      statement.evidence_ids.some(id=>task.sources.some(source=>source.evidence.evidence_id===id &&
        source.identity==="VERIFIED_FACT" && source.freshness==="CURRENT" && ["news","sec_filing"].includes(source.evidence.evidence_type))));
    if(company>=0)return company;
    return Math.max(0,reasons.findIndex(statement=>statement.identity!=="USER_ASSERTION"));
  }
  function leadingReason(task) {
    const company=statement=>statement.identity!=="USER_ASSERTION" && statement.evidence_ids.some(id=>task.sources.some(source=>source.evidence.evidence_id===id && source.identity==="VERIFIED_FACT" && source.freshness==="CURRENT" && ["news","sec_filing"].includes(source.evidence.evidence_type)));
    return task.assessment.key_reasons.find(company) || task.assessment.counterevidence.find(company) || task.assessment.key_reasons[leadingReasonIndex(task)];
  }
  function renderWaiting(task) {
    $("desk-waiting").hidden=task.status!=="RUNNING";
    $("desk-stages").replaceChildren();
    if(task.status!=="RUNNING")return;
    const observed=name=>task.tool_calls.some(call=>call.name===name && call.status==="success");
    const failedCheck=task.tool_calls.some(call=>["market.get_eod","portfolio.get_snapshot"].includes(call.name) && call.status!=="success");
    const checked=observed("market.get_eod") && observed("portfolio.get_snapshot");
    const generating=task.progress.some(text=>text==="资料核查结束，正在生成并校验研究判断。" || text==="调查已完成，综合支持、反证与行动条件。" || text.startsWith("首次调查发现关键缺口"));
    const current=generating ? 3 : checked ? 2 : 1;
    const done=[true,checked,generating,false,false];
    ["研究问题已接收","持仓与价格前提检查","资料与来源核查","研究判断生成与校验","结果保存"].forEach((text,index)=>{
      const limited=index===1 && !checked && failedCheck;
      const row=node("li",null,limited ? "is-limited" : done[index] ? "is-done" : index===current ? "is-current" : "is-pending");
      row.append(node("span",String(index+1),"desk-stage-number"),node("span",text),node("small",limited ? "核查受限 · 有失败记录" : done[index] ? "已记录" : index===current ? "进行中" : "待执行"));
      if(index===current)row.setAttribute("aria-current","step");$("desk-stages").append(row);
    });
    $("desk-waiting-help").textContent=generating ? "正在生成或校验有来源支持的判断。无法准确预估剩余时间；通过校验并保存后才显示研究完成。" : "核查这次问题需要的持仓、价格和资料；不可用的来源会明确记录。可以返回首页，稍后重新打开同一任务。";
  }
  function observationLost(text) {
    $("desk-observation").hidden=false;$("desk-observation-note").textContent=text;
    $("desk-task").dataset.observing="false";
  }
  function render(task, historical=false) {
    const key=[task.task_id,task.revision,task.status,task.updated_at,historical,
      task.progress.length,task.tool_calls.length,task.model_events.length,task.sources.length].join(":");
    if(active?.task_id!==task.task_id || active?.revision!==task.revision){$("desk-evidence-dialog").close();sourceKey=null;readingReturn=null;observedSince=Date.now();}
    active=task;
    historicalView=historical;
    buttons(task);
    const url=new URL(location.href);url.searchParams.set("research_task",task.task_id);
    if(historical)url.searchParams.set("revision",String(task.revision));else url.searchParams.delete("revision");
    history.replaceState(null,"",url);
    if(key===renderKey)return;renderKey=key;
    $("desk-history-note").hidden=!historical;
    $("desk-latest").hidden=!historical;
    if(historical)$("desk-history-note").textContent=`历史第 ${task.revision} 轮 · 研究截止 ${localZone(task.cutoff)} · 资料与价格对应当时，当前只读。`;
    $("desk-task").hidden=false;
    document.body.dataset.deskView="task";
    document.querySelectorAll(".rail nav a").forEach(link=>{const selected=link.hash==="#research-history";link.classList.toggle("active",selected);if(selected)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
    $("desk-task-title").textContent=(task.focus_symbol ? task.focus_symbol+" · " : "") + objectives[task.objective];
    $("desk-task-meta").textContent=`第 ${task.revision} 轮 · 研究截止 ${localZone(task.cutoff)} · ${task.account_id ? "所选账户" : "全部账户"} · 绑定原持仓快照`;
    $("desk-current-question").textContent=task.question;
    $("desk-state").textContent=statuses[task.status];$("desk-state").dataset.state=task.status;
    const mobile=document.querySelector(".desk-mobile-action");
    mobile.textContent=historical ? "历史版本 · 只读查看" : {RUNNING:"查看研究进度",FAILED:"查看恢复与重试选项",NEEDS_INPUT:"补充研究问题",COMPLETE:"查看下一步核查"}[task.status];
    mobile.href=task.status==="RUNNING" ? "#desk-waiting" : "#desk-action-panel";
    $("desk-task-details").textContent=`任务 ${task.task_id} · 截止 ${stamp(task.cutoff)} · 更新 ${stamp(task.updated_at)} · ${task.account_id ? "账户 "+task.account_id : "全部账户"} · 持仓 ${task.snapshot_id || "尚无快照"}`;
    const progressText=task.status==="COMPLETE" ? "研究完成，结果已保存。" : task.status==="FAILED" ? "本轮研究未完成；任务和来源已保留。" : task.status==="NEEDS_INPUT" ? "补充问题后可继续同一任务。" : task.progress.at(-1)||"研究问题已保存。";
    if($("desk-progress").textContent!==progressText)$("desk-progress").textContent=progressText;
    $("desk-progress").classList.toggle("sr-only",task.status!=="RUNNING");
    $("desk-result").setAttribute("aria-busy",String(task.status==="RUNNING"));
    renderWaiting(task);
    const result=$("desk-result");result.replaceChildren();let accountDetails=null;
    $("desk-next-action").replaceChildren();$("desk-next-draft").hidden=true;
    $("desk-read-saved").textContent="重新读取保存状态 · 不调用模型";
    $("desk-action-hint").textContent=historical ? "历史版本只读。回到最新一轮后可继续研究。" : "沿用这次的账户与持仓快照；更换范围请开始新研究。";
    $("desk-retry-options").open=task.status==="FAILED";
    if(task.status!=="RUNNING" && task.status!=="COMPLETE")result.append(timeStrip(task));
    if(task.status==="FAILED") {result.append(node("h3","本轮没有新的研究判断"),node("p",errorMessage(task.error,task),"desk-error"));if(task.unresolved_question)result.append(node("p",task.unresolved_question));$("desk-next-action").append(node("h3","先恢复读取，再决定是否重试"),node("p",task.pending_next_action || "读取保存状态不会重新提交研究。确认原因后，再选择下方的重新研究。"));}
    if(task.status==="NEEDS_INPUT") result.append(node("p",task.unresolved_question,"desk-input-needed"));
    if(task.status==="COMPLETE" && task.assessment) {
      const dispositions={CONDITIONAL_ADD:"有条件考虑加仓",DEFER:"暂缓行动",REVIEW_REDUCE:"复核减仓条件",RESEARCH_ONLY:"继续研究",UNKNOWN:"证据不足，暂无法判断"};
      if(task.objective==="CONSIDER_ADD")dispositions.DEFER="暂缓加仓";
      if(task.objective==="CONSIDER_REDUCE")dispositions.DEFER="暂缓减仓";
      const action=task.action_decision, actionLabels={ADD:"ADD · 倾向加仓",HOLD:"HOLD · 倾向持有",REDUCE:"REDUCE · 倾向减持",EXIT:"EXIT · 倾向退出"};
      const actionHeading=action ? actionLabels[action.decision_action] || (action.decision_status==="NOT_APPLICABLE" ? "该操作不适用" : "方向未形成 · 证据不足") : null;
      const core=overview(task.objective==="EARNINGS_CHECK" ? "财报研究重点" : actionHeading || dispositions[task.assessment.disposition]||"核心判断",task.assessment.core_judgment,true);core.classList.add("desk-core");
      if(action)core.append(node("p",action.applicability_note || (action.decision_status==="SUPPORTED" ? "证据支持的研究方向" : action.decision_status==="PROVISIONAL" ? "暂定研究方向 · 新事实可能改变判断" : "尚无适用且有依据的操作方向"),"small desk-core-note"));
      if(!action)core.append(node("p","这是基于所列资料的条件研究判断，尚待你确认。","small desk-core-note"));result.append(core);
      result.append(overview("为什么这样判断",leadingReason(task),true));
      if(task.assessment.action)result.append(overview("投资逻辑变化",task.assessment.action.thesis_delta));
      const gain=researchGain(task);if(gain)result.append(gain);
      if(task.assessment.investigation_effect){const effect=overview("深入研究补查带来的变化",task.assessment.investigation_effect,true);effect.append(node("p",{CHANGED:"方向因新证据改变",UNCHANGED:"方向未变：新证据让风险更具体",UNRESOLVED:"关键问题仍未解决"}[task.assessment.judgment_change] || "方向变化未记录","small desk-core-note"));result.append(effect);}
      if(task.objective!=="EARNINGS_CHECK"){
        const p=task.portfolio_context, metrics=node("dl",null,"desk-key-metrics");
        const values=[["用户情景价 · 未核实",money(task.premise.user_price),"你提供的前提，未独立核实"],["最近合格收盘参考",money(task.premise.completed_close),closeTiming(task).label],
          ["平均持仓成本",money(p?.position?.average_cost),"来自绑定的持仓快照"],[p?.scenario ? "情景浮盈亏 · 已有持仓" : "参考浮盈亏 · 已有持仓",money(p?.scenario ? p.scenario.unrealized_pnl : p?.position?.unrealized_pnl),p?.scenario ? "按情景价重估，未模拟买入" : "按收盘参考重估"]];
        for(const [title,value,note]of values){const cell=node("div");cell.append(node("dt",title),node("dd",value),node("small",note,"desk-metric-note"));metrics.append(cell);}result.append(metrics);
      }
      const next=overview("下一步优先核查",task.assessment.next_checks[0],true);next.classList.add("desk-next-check");$("desk-next-action").append(next);
      $("desk-next-draft").hidden=!task.assessment.next_checks.length;
      if(task.portfolio_context?.position)result.append(node("p",`这次判断结合你已有的 ${task.portfolio_context.position.quantity} 股持仓；现金 ${money(task.portfolio_context.cash)} 来自冻结快照，实时可用额仍须核实。价格情景只重估已有持仓，没有模拟买入。`,"desk-personal-context small"));
    }
    const p=task.portfolio_context;
    if(p) {
      const marketSource=task.sources.find(source=>source.evidence.evidence_id===task.premise.completed_close_evidence_id);
      const snapshotSource=task.sources.find(source=>source.evidence.evidence_type==="portfolio_snapshot");
      const section=node("details",null,"desk-financial-context");section.id="desk-account-details";
      section.append(node("summary","账户、持仓与完整计算口径"),node("p",`账户：${p.account_names?.join("、") || p.account_id || "全部账户"} · 真实持仓快照 · ${p.valuation_status === "complete" ? "全部持仓已定价" : "部分持仓缺少合格价格"}`,"small muted"));
      const rows=[["已有持仓",p.position ? p.position.quantity+" 股" : "没有该标的持仓"],
        ["平均持仓成本",money(p.position?.average_cost)],["该持仓总成本",money(p.position?.cost_basis)],
        ["账户现金（快照）",money(p.cash)],["最近收盘参考价",money(p.position?.price)],
        ["收盘参考市值",money(p.position?.market_value)],["参考浮盈亏",money(p.position?.unrealized_pnl)],
        ["参考浮盈亏比例",percent(p.position?.unrealized_pnl_percent)],
        ["参考账户净值",money(p.nav)],["参考持仓集中度",percent(p.position?.nav_weight)]];
      const context=node("dl",null,"desk-portfolio-context");
      for(const [label,value] of rows){const cell=node("div");cell.append(node("dt",label),node("dd",value));context.append(cell);}
      section.append(context,node("p",`持仓快照时点：${stamp(snapshotSource?.evidence.as_of)}。收盘交易日：${marketSource?.evidence.structured_data?.session_date || "未单独记录"} · 行情可用时点 ${stamp(p.position?.price_as_of)}。缺少可核实价格的持仓：${p.missing_price_symbols.join("、") || "无"}。现金来自所选持仓快照，实时可用余额仍须核实；完整账户集中度须覆盖全部持仓。`,"small muted"));
      if(p.scenario) {
        const q=p.scenario, scenario=node("div",null,"desk-scenario");
        scenario.append(node("h4",`用户假设价格 ${money(q.price)} · 未独立核实`));
        const values=[["假设持仓市值",money(q.market_value)],["假设浮盈亏",money(q.unrealized_pnl)],
          ["假设浮盈亏比例",q.unrealized_pnl_percent_display || "未知"],["假设账户净值",money(q.nav)],
          ["假设集中度",q.nav_weight_percent_display || "未知"],["相对参考收盘变化",q.price_delta_percent_display || "未知"]];
        const grid=node("dl",null,"desk-portfolio-context");
        for(const [label,value] of values){const cell=node("div");cell.append(node("dt",label),node("dd",value));grid.append(cell);}
        scenario.append(grid,node("p","只改变研究标的价格，其他持仓采用合格收盘价，现金保持快照值；没有模拟买入。与收盘的比较不证明盘前报价，也不确定你所说“跌到”的原始参照。", "small muted"));
        if(q.unknown_reason)scenario.append(node("p","其他持仓价格缺失，假设账户净值与集中度未知。","small"));
        section.append(scenario);
      }
      // Append after the readable memo, not before its supporting evidence.
      accountDetails=section;
    }
    if(task.status==="COMPLETE" && task.assessment) {
      const memo=node("article",null,"desk-memo");

      const premise=node("div",null,"desk-premise");
      premise.append(node("p",`你的价格前提：${money(task.premise.user_price)} · 未核实`),
        node("p",`最近可用交易日收盘价：${money(task.premise.completed_close)} · ${closeTiming(task).label}`),
        node("p",task.premise.explanation,"small muted"));memo.append(premise);
      const comparison=node("div",null,"desk-evidence-comparison");
      comparison.append(overview("主要支持",task.assessment.support[0]),overview("最强反证与风险",task.assessment.counterevidence[0],true));memo.append(comparison);
      memo.append(overview("什么事实会改变判断",task.assessment.action_conditions[0],true));
      if(task.limitations.length){const gaps=node("section",null,"desk-material-gaps");gaps.append(node("h3","判断的证据边界"),node("p","这些缺口限制了相应结论；未核实的经营变化、估值或市场因素不作为本轮确认依据。","small"));task.limitations.slice(0,2).forEach(text=>gaps.append(node("p",text.replace(/^MARKET_REGIME_NOT_VERIFIED：/,""),"small")));memo.append(gaps);}
      const details=node("details",null,"desk-report-details");details.append(node("summary","完整研究报告与资料限制"),group("原始核心判断",[task.assessment.core_judgment]),premise.cloneNode(true),group("全部支持",task.assessment.support),group("全部反证",task.assessment.counterevidence),group("全部行动条件",task.assessment.action_conditions));
      details.append(group("全部理由",task.assessment.key_reasons),group("全部下一步核查",task.assessment.next_checks));
      if(task.limitations.length) details.append(group("资料限制",task.limitations.map(text=>({text:text.replace(/^MARKET_REGIME_NOT_VERIFIED：/,""),identity:"UNKNOWN",evidence_ids:[]}))));
      memo.append(details);
      result.append(memo);
    }
    // Decision, reasons and counterevidence precede financial detail.
    const financials=task.status==="COMPLETE" ? financialHistory(task) : null;if(financials)result.append(financials);
    const metricBand=result.querySelector(":scope > .desk-key-metrics");if(metricBand)result.append(metricBand);
    const personal=result.querySelector(":scope > .desk-personal-context");if(personal)result.append(personal);
    if(accountDetails)result.append(accountDetails);
    if(task.status==="COMPLETE")result.append(timeStrip(task));
    $("desk-trace").replaceChildren();
    $("desk-trace").append(researchDepth(task));
    const progress=node("ol",null,"desk-observed-steps");task.progress.forEach(text=>progress.append(node("li",text)));$("desk-trace").append(progress);
    for(const call of task.tool_calls){const p=node("p",`${call.name} · ${call.status === "success" ? "完成" : "失败："+call.error} · ${stamp(call.started_at)} → ${stamp(call.ended_at)}`);$("desk-trace").append(p);}
    for(const event of task.model_events) {$("desk-trace").append(node("p",`${event.purpose} · ${event.model} · ${event.status}${event.error ? " · "+event.error : ""}`));if(event.transport_attempts?.length)$("desk-trace").append(node("pre",JSON.stringify(event.transport_attempts,null,2)));if(event.validation_attempts?.length)$("desk-trace").append(node("pre",JSON.stringify(event.validation_attempts,null,2)));}
    const sources=task.sources.map(s=>[s.evidence.evidence_id,s.evidence.content_hash,s.identity,s.freshness].join(":")).join("|");
    if(sources!==sourceKey){sourceKey=sources;$("desk-source-list").replaceChildren();
    for(const [index,source] of task.sources.entries()){
      const e=source.evidence, row=node("details",null,"desk-source");row.tabIndex=-1;row.dataset.sourceId=e.evidence_id;
      const summary=node("summary",null,"desk-source-summary");
      summary.append(node("span",`[${index+1}]`,"desk-source-index"),node("span",sourceTitle(e),"desk-source-title"),sourceChip(source));
      const publishedLabel=["news","sec_filing"].includes(e.evidence_type)?"新闻/披露发布":"来源记录时间";
      const effectiveLabel=e.evidence_type==="market_data"?"行情时点":"资料有效时点";
      row.append(summary,node("p",`${sourceKinds[e.evidence_type]||"研究来源"} · ${identity[source.identity]} · ${freshnessLabels[source.freshness]||"时效待核查"}`,"small"),
        node("p",`${e.provider} · ${publishedLabel} ${localZone(e.published_at)} · ${effectiveLabel} ${localZone(e.as_of)} · 实际取回 ${localZone(e.retrieved_at)}`,"small muted"));
      if(e.evidence_type==="market_data"){
        const sessions={REGULAR:"常规交易",PRE_MARKET:"盘前",POST_MARKET:"盘后",OVERNIGHT:"隔夜",COMPLETED_CLOSE:"日终收盘"};
        const quoteSession=e.structured_data?.session;
        row.append(node("p",`交易时段：${sessions[quoteSession] || "来源未提供"} · 账户行情延迟权限：未知；不能据此称为实时。`,"small muted"));
        if(e.as_of){const age=(Date.parse(e.retrieved_at)-Date.parse(e.as_of))/1000;if(Number.isFinite(age)&&age>=0)row.append(node("p",`取回时距行情时点 ${Math.floor(age)} 秒；这是时间差，不是交易所 feed delay。`,"small muted"));}
      }
      if(source.identity==="AI_DRAFT") row.append(node("p","这是 AI 草稿或历史研究解释，不代表你已确认的持仓理由。","small"));
      if(e.excerpt)row.append(node("p",e.excerpt));
      if(e.source_uri){try{const url=new URL(e.source_uri);if(["https:","http:"].includes(url.protocol)){const link=node("a","查看原始来源（新标签页）");link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";row.append(link);}}catch{}}
      if(e.structured_data){const details=node("details"), pre=node("pre",JSON.stringify(e.structured_data,null,2));details.append(node("summary","来源字段与计算口径"),pre);row.append(details);}
      const provenance=node("details");provenance.append(node("summary","追溯标识与内容校验"),node("p",`证据 ${e.evidence_id} · 内容哈希 ${e.content_hash}`,"small muted"));row.append(provenance);
      if(!publicReplay){
        const ask=node("button","围绕这条证据追问","desk-evidence-followup");ask.type="button";
        ask.addEventListener("click",()=>{
          if(historicalView || active?.status==="RUNNING" || sending)return;
          $("desk-followup").value=`${active.focus_symbol || ""}：证据 ${e.evidence_id}「${sourceTitle(e).slice(0,160)}」支持哪些判断？最强反证和仍待核查的条件是什么？`;
          readingReturn=$("desk-followup");closeEvidence();$("desk-followup").focus();
        });row.append(ask);
      }
      const back=node("button","返回研究","desk-source-return");back.type="button";back.addEventListener("click",closeEvidence);row.append(back);$("desk-source-list").append(row);
    }}
    document.querySelectorAll(".desk-evidence-followup").forEach(button=>{button.disabled=historicalView || task.status==="RUNNING" || sending;});
    buttons(task);
  }
  async function historyOptions(task, generation) {
    const select=$("desk-history-select");select.disabled=true;
    try {
      const revisions=await readApi("/api/research/tasks/"+encodeURIComponent(task.task_id)+"/revisions");
      if(generation!==viewGeneration)return;
      select.replaceChildren(node("option","最新一轮"));select.firstChild.value="";
      const latest=Math.max(task.revision,...revisions.map(row=>row.revision));
      for(const row of revisions.filter(row=>row.revision<latest)) {
        const option=node("option",`第 ${row.revision} 轮 · ${local(row.updated_at)} · ${statuses[row.status]} · ${row.question}`);
        option.value=String(row.revision);select.append(option);
      }
      select.value=historicalView ? String(task.revision) : "";
    } catch { if(generation===viewGeneration) {$("desk-history-note").hidden=false;$("desk-history-note").textContent="历史列表暂时无法读取；重新打开任务可重试。";} }
    finally {if(generation===viewGeneration)select.disabled=false;}
  }
  // Home shows a short "continue" summary; 研究记录 is the one full, searchable list.
  const HOME_RECENT=3;
  let savedTasks=null;
  function renderRecent() {
    if(!savedTasks)return;
    const full=document.body.dataset.consoleView==="history";
    const query=full ? $("desk-recent-search").value.trim().toLowerCase() : "";
    $("desk-recent-title").textContent=full ? "全部研究" : "接着上次的研究";
    $("desk-recent-search-row").hidden=!full || !savedTasks.length;
    const list=$("desk-recent");list.replaceChildren();
    if(!savedTasks.length){list.append(node("p","研究会保存在这里，返回首页或重启后仍可继续。","muted"));$("desk-recent-all").hidden=true;return;}
    const matches=savedTasks.filter(task=>!query || `${task.focus_symbol||""} ${task.question}`.toLowerCase().includes(query));
    const shown=full ? matches : matches.slice(0,HOME_RECENT);
    if(!shown.length)list.append(node("p","没有匹配的研究记录。","muted"));
    shown.forEach(task=>{
      const button=node("button",null,"desk-recent-task");button.type="button";
      const meta=node("small",null,"desk-recent-meta");meta.dataset.state=task.status;
      meta.append(node("span",statuses[task.status],"desk-recent-status"),node("span",`第 ${task.revision} 轮 · ${local(task.updated_at)}`,"desk-recent-time"));
      button.append(node("strong",task.focus_symbol||"待明确"),node("span",task.question),meta);
      button.addEventListener("click",()=>openTask(task.task_id));list.append(button);
    });
    $("desk-recent-all").hidden=full || savedTasks.length<=HOME_RECENT;
    $("desk-recent-all").textContent=`全部 ${savedTasks.length} 项研究记录`;
    $("desk-recent-count").textContent=full ? `${savedTasks.length} 项 · 打开保存结果不会重新调用模型；历史版本在任务内切换。` : "";
  }
  async function recent() {
    const generation=++listGeneration;
    try {
      const tasks=await readApi("/api/research/tasks");
      if(generation!==listGeneration)return;
      savedTasks=tasks;renderRecent();
    }catch {if(generation===listGeneration){savedTasks=null;$("desk-recent").textContent="保存的任务暂时无法读取，可点击刷新重试。";}}
  }
  async function watch(taskId, generation) {
    const deadline=Date.now()+260000;
    $("desk-observation").hidden=true;$("desk-task").dataset.observing="true";
    while(generation===viewGeneration && Date.now()<deadline){
      try {
        const task=await readApi("/api/research/tasks/"+encodeURIComponent(taskId));
        if(generation!==viewGeneration)return;
        render(task);
        if(task.status!=="RUNNING"){$("desk-task").dataset.observing="false";void historyOptions(task,generation);void recent();return;}
      }catch(error){if(generation===viewGeneration)observationLost("暂时无法读取进度，服务器任务状态尚未确认。重新读取只查看同一任务，不会再次调用研究模型。");return;}
      await new Promise(resolve=>setTimeout(resolve,750));
    }
    if(generation===viewGeneration)observationLost("本次观察已结束，任务状态以重新读取为准；不会自动重试研究。");
  }
  async function openTask(taskId, revision="") {
    clearAttentionDraft();
    const generation=++viewGeneration;
    $("desk-observation").hidden=true;
    try{
      const task=await readApi("/api/research/tasks/"+encodeURIComponent(taskId)+(revision ? "/revisions/"+encodeURIComponent(revision) : ""));
      if(generation!==viewGeneration)return;
      const target=new URL(location.href);target.searchParams.set("research_task",taskId);
      if(revision)target.searchParams.set("revision",revision);else target.searchParams.delete("revision");
      if(target.href!==location.href)history.pushState(null,"",target);
      render(task,!!revision);$("desk-task").focus({preventScroll:true});$("desk-task").scrollIntoView({block:"start",behavior:motion()});
      void historyOptions(task,generation);
      if(!revision && task.status==="RUNNING")void watch(taskId,generation);
    }catch(error){if(generation===viewGeneration){if(active)observationLost("任务暂时无法读取，仍保留上次观察的版本。请重新读取进度。");else $("desk-request-status").textContent="任务暂时无法读取，请刷新最近研究；没有重新提交。";}}
  }
  async function send(question, followup) {
    if(publicReplay)return;
    if(sending || historicalView || followup && active?.status==="RUNNING")return;
    sending=true;buttons(active);$("desk-submit").disabled=true;
    const generation=++viewGeneration;
    const request={question};
    if(followup && active)request.task_id=active.task_id;
    else {
      if($('desk-model-profile')?.value)request.model_profile_id=$('desk-model-profile').value;
      if(state.snapshot)request.snapshot_id=state.snapshot.snapshot_id;
      if($("desk-account").value)request.account_id=$("desk-account").value;
      if(attentionDraft)request.attention_item_id=attentionDraft.attention_item_id;
    }
    $("desk-request-status").textContent="正在保存研究任务…";
    try{
      const task=await api("/api/research/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(request)});
      if(generation!==viewGeneration)return;
      const target=new URL(location.href);target.searchParams.set("research_task",task.task_id);target.searchParams.delete("revision");
      if(target.href!==location.href)history.pushState(null,"",target);
      clearAttentionDraft();render(task);$("desk-task").focus({preventScroll:true});$("desk-task").scrollIntoView({block:"start",behavior:motion()});$("desk-followup").value="";
      void historyOptions(task,generation);void recent();
      $("desk-request-status").textContent="研究任务已创建。";
      void watch(task.task_id,generation);
    }catch(error){if(generation===viewGeneration){$("desk-request-status").textContent=errorMessage(error)+" 如请求结果不明，请先刷新最近研究。";await recent();}}
    finally{sending=false;$("desk-submit").disabled=false;buttons(active);}
  }
  window.addEventListener("mia-ask-draft",event=>{
    if(publicReplay)return;
    leaveTask("#ask-mia");
    $("desk-question").value=event.detail.question.slice(0,1200);
    if(event.detail.attention_item_id){attentionDraft={attention_item_id:event.detail.attention_item_id,symbol:event.detail.symbol};$("desk-attention-context").hidden=false;$("desk-attention-context").querySelector("span").textContent=`已关联 ${attentionDraft.symbol} 的关注事项；提交后会核查该事项的冻结来源。`;}
    $("ask-mia").scrollIntoView({block:"start",behavior:motion()});$("desk-question").focus({preventScroll:true});
    $("desk-request-status").textContent="已带入这条关注事项，可修改后提交；尚未开始研究。";
  });
  $("desk-clear-attention").addEventListener("click",clearAttentionDraft);
  // Every research POST waits for the in-app cost confirmation; cancel sends nothing.
  const scopeLabel=()=>$("desk-account").selectedOptions[0]?.textContent || "全部账户";
  const confirmNew=()=>miaConfirmCost({title:"开始这项研究？",body:"创建研究任务，读取持仓、核查资料并由模型形成判断。",confirmLabel:"确认开始研究",
    facts:[["持仓范围",scopeLabel()],...(attentionDraft?[["关联事项",`${attentionDraft.symbol} 的关注事项`]]:[])]});
  const confirmFollowup=()=>miaConfirmCost({title:`继续研究 · 第 ${(active?.revision||0)+1} 轮`,body:"创建新版本并重新核查资料；之前的版本保留。",confirmLabel:"确认继续研究",
    facts:[["持仓范围",active?.account_id ? "所选账户 · 原持仓快照" : "全部账户 · 原持仓快照"]]});
  const confirmRetry=()=>miaConfirmCost({title:"重新研究同一问题？",body:"保留同一任务和旧版本，重新核查资料和价格；旧行情不会作为新事实。",confirmLabel:"确认重新研究"});
  // A confirmation covers only the task revision shown in the dialog; if navigation
  // changed the open task meanwhile, nothing is sent.
  const stillShowing=shown=>Boolean(shown && active && active.task_id===shown.task_id && active.revision===shown.revision && !historicalView);
  $("desk-question-form").addEventListener("submit",async event=>{event.preventDefault();if(publicReplay){const example=demoManifest?.examples[0];if(example)void openTask(example.task_id,String(example.revision));return;}if(sending)return;const draft=attentionDraft;if(await confirmNew() && attentionDraft===draft)void send($("desk-question").value,false);});
  $("desk-followup-form").addEventListener("submit",async event=>{event.preventDefault();if(publicReplay){const next=demoManifest?.examples.find(e=>e.task_id===active?.task_id)?.followup_revision;if(next && active.revision<next)void openTask(active.task_id,String(next));return;}if(sending)return;const shown=active;if(await confirmFollowup() && stillShowing(shown))void send($("desk-followup").value,true);});
  $("desk-earnings").addEventListener("click",()=>{$("desk-followup").value="那它下次财报需要重点看什么？";$("desk-followup").focus();});
  $("desk-next-draft").addEventListener("click",()=>{$("desk-followup").value=(active?.assessment?.next_checks[0]?.text||"").slice(0,1200);$("desk-followup").focus();});
  $("desk-retry").addEventListener("click",async()=>{if(active?.status!=="FAILED" || sending)return;const shown=active;if(await confirmRetry() && stillShowing(shown) && active.status==="FAILED")void send(active.question,true);});
  $("desk-latest").addEventListener("click",()=>{if(active)void openTask(active.task_id);});
  $("desk-refresh-progress").addEventListener("click",()=>{if(active)void openTask(active.task_id,historicalView ? String(active.revision) : "");});
  $("desk-back-to-reading").addEventListener("click",closeEvidence);
  $("desk-evidence-dialog").addEventListener("close",()=>{if(readingReturn?.isConnected)readingReturn.focus({preventScroll:true});});
  $("desk-evidence-open").addEventListener("click",event=>openEvidence([],event.currentTarget));
  $("desk-history-open").addEventListener("click",()=>{$("desk-history").open=!$("desk-history").open;if($("desk-history").open)$("desk-history-select").focus();});
  $("desk-read-saved").addEventListener("click",()=>{if(active)void openTask(active.task_id,historicalView ? String(active.revision) : "");});
  $("desk-pause-motion").addEventListener("click",event=>{const paused=event.currentTarget.getAttribute("aria-pressed")!=="true";event.currentTarget.setAttribute("aria-pressed",String(paused));event.currentTarget.textContent=paused?"恢复动画":"暂停动画";$("desk-task").dataset.motion=paused?"paused":"playing";});
  function applyNavigation() {
    if(active)return;
    const hash=location.hash,view={"#universe":"universe","#portfolio":"portfolio","#workspace":"workspace","#inspector":"inspector","#research-history":"history"}[hash]||"home";
    document.body.dataset.consoleView=view;renderRecent();
    const labels={home:["你的研究工作台","先看与你相关的变化，再研究一个重要问题。"],history:["你的研究记录","重新打开结果、核对来源，或从最新一轮继续。"],portfolio:["我的组合","管理持仓快照，查看已保存的成本、现金与集中度。"],universe:["研究范围","选择持续跟踪的公司；持仓会自动纳入。"],workspace:["快速判断工具","复用现有决策与情景工具；完整调查从工作台的研究提问发起。"],inspector:["依据与执行面板","查看现有工具结果、证据和可观察记录。"]};
    const heading=document.querySelector(".page-heading");heading.querySelector("h1").textContent=labels[view][0];heading.querySelector(".muted").textContent=labels[view][1];$("manage-portfolio").hidden=publicReplay || !["home","portfolio"].includes(view);
    $("universe").open=view==="universe";
    $("portfolio-disclosure").open=view==="portfolio";
    $("desk-legacy").open=["workspace","inspector"].includes(view);
    document.querySelectorAll(".rail nav a").forEach(link=>{const selected=view==="home"?link.hash==="#attention":link.hash===hash;link.classList.toggle("active",selected);if(selected)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  }
  function leaveTask(destination=null) {clearAttentionDraft();++viewGeneration;active=null;historicalView=false;renderKey="";$("desk-evidence-dialog").close();$("desk-task").hidden=true;delete document.body.dataset.deskView;const url=new URL(location.href);url.searchParams.delete("research_task");url.searchParams.delete("revision");if(destination!==null)url.hash=destination;if(destination!==null&&url.href!==location.href)history.pushState(null,"",url);else history.replaceState(null,"",url);applyNavigation();void recent();}
  $("desk-home").addEventListener("click",()=>{leaveTask("#ask-mia");$("ask-mia").scrollIntoView({block:"start"});$("desk-question").focus({preventScroll:true});});
  document.querySelectorAll(".rail nav a,.desk-home-shortcuts a").forEach(link=>link.addEventListener("click",()=>leaveTask(link.hash)));
  $("back-to-brief").addEventListener("click",()=>{leaveTask("#workspace");$("result").scrollIntoView({block:"start",behavior:motion()});});
  window.addEventListener("popstate",()=>{const params=new URL(location.href).searchParams;if(params.has("research_task")){const id=params.get("research_task"),revision=params.get("revision")||"";if(active?.task_id===id && revision===(historicalView?String(active.revision):""))return;void openTask(id,revision);}else leaveTask();});
  document.querySelectorAll("[data-desk-prompt]").forEach(button=>button.addEventListener("click",()=>{clearAttentionDraft();$("desk-question").value=button.dataset.deskPrompt;$("desk-question").focus();}));
  const mobileAction=document.querySelector(".desk-mobile-action");
  mobileAction.addEventListener("click",()=>{const target=document.querySelector(mobileAction.getAttribute("href"));target.tabIndex=-1;target.focus({preventScroll:true});});
  new IntersectionObserver(entries=>{
    const panelVisible=entries[0].isIntersecting;
    mobileAction.classList.toggle("is-panel-visible",panelVisible);
  },{rootMargin:"0px 0px -80px 0px",threshold:0}).observe($("desk-action-panel"));
  $("desk-refresh-tasks").addEventListener("click",recent);
  $("desk-recent-search").addEventListener("input",renderRecent);
  $("desk-recent-all").addEventListener("click",()=>{leaveTask("#research-history");scrollTo({top:0,behavior:motion()});});
  $("desk-history-select").addEventListener("change",async event=>{
    if(!active)return;
    const taskId=active.task_id, revision=event.target.value;
    if(!revision){openTask(taskId);return;}
    const generation=++viewGeneration;
    const priorHistory=historicalView, priorRevision=priorHistory ? String(active.revision) : "";
    historicalView=true;buttons(active);
    $("desk-observation").hidden=true;
    try {
      const task=await readApi("/api/research/tasks/"+encodeURIComponent(taskId)+"/revisions/"+encodeURIComponent(revision));
      if(generation!==viewGeneration)return;
      render(task,true);
    } catch(error) {if(generation===viewGeneration){
      historicalView=priorHistory;event.target.value=priorRevision;buttons(active);
      observationLost("历史版本暂时无法读取，仍显示原版本；没有改写任务或重新提交。");
    }}
  });
  window.addEventListener("hashchange",applyNavigation);
  if(location.hash==="#workspace")$("desk-legacy").open=true;
  // Portfolio is owned by existing Console; update choices when its snapshot changes.
  function accountOptions(){
    if(accountsSnapshot===state.snapshot?.snapshot_id)return;
    accountsSnapshot=state.snapshot?.snapshot_id;
    const select=$("desk-account"), prior=select.value;select.replaceChildren(node("option","全部账户"));select.firstChild.value="";
    for(const account of state.snapshot?.accounts||[]){const option=node("option",account.name);option.value=account.account_id;select.append(option);}
    if([...select.options].some(o=>o.value===prior))select.value=prior;
  }
  new MutationObserver(accountOptions).observe($("portfolio-state"),{childList:true,subtree:true});
  accountOptions();recent();
  if(!publicReplay)readApi('/api/research/model-profiles').then(data=>{
    const select=$('desk-model-profile');select.replaceChildren();
    for(const profile of data.profiles || [])if(profile.available){
      const option=node('option',`${profile.mode==='FAST' ? '快速判断' : '深入研究'} · ${profile.model}`);
      option.value=profile.profile_id;option.selected=profile.mode==='DEEP';select.append(option);
    }
    $('desk-profile-label').hidden=!select.options.length;
  }).catch(()=>{$('desk-profile-label').hidden=true;});
  applyNavigation();
  if(publicReplay){
    $("public-demo-notice").hidden=false;$("attention-scan").hidden=true;
    $("attention-title").textContent="今日关注 · 合成回放";
    $("ask-mia-title").textContent="研究提问 · 合成回放";
    $("desk-question").readOnly=true;
    $("desk-account").disabled=true;$("desk-submit").disabled=true;$("desk-submit").textContent="打开快速判断示例";
    document.querySelector("#desk-question-form + p").textContent="公开合成回放；只读取保存结果，不调用模型，也不保存访客输入。";
    document.querySelectorAll("[data-desk-prompt]").forEach(n=>{n.hidden=true;});
    document.querySelectorAll('.rail nav a[href="#universe"],.desk-nav-more').forEach(n=>{n.hidden=true;});
    document.querySelectorAll("#universe-form input,#universe-form select,#universe-form textarea,#universe-form button").forEach(n=>{n.disabled=true;});
    document.querySelector(".topbar .tag").textContent="Public Demo · SYNTHETIC · OFFLINE_REPLAY";
    const railText=document.querySelector(".rail-foot .local-dot").nextSibling;if(railText)railText.textContent=" 公开合成工作空间";
    readApi("/api/public-demo").then(manifest=>{
      demoManifest=manifest;const first=manifest.examples[0];
      if(first)$("desk-question").value=`${first.symbol}：今日关注事项会改变我的研究判断吗？`;
      $("desk-submit").disabled=false;if(active)buttons(active);
    }).catch(()=>{$("desk-request-status").textContent="合成回放暂时无法读取，请刷新页面重试；没有提交研究。";});
    window.addEventListener("mia-open-task",event=>void openTask(event.detail.task_id,String(event.detail.revision)));
  }
  setInterval(()=>{
    if(active?.status!=="RUNNING" || $("desk-task").dataset.observing!=="true")return;
    const elapsed=Date.now()-Date.parse(active.cutoff), valid=elapsed>=0 && elapsed<86400000;
    const seconds=Math.floor((valid ? elapsed : Date.now()-observedSince)/1000);
    $("desk-elapsed").textContent=`${valid ? "已等待" : "本次查看"} ${Math.floor(seconds/60)}分${seconds%60}秒`;
  },1000);
  const params=new URL(location.href).searchParams;
  if(params.has("research_task"))void openTask(params.get("research_task"),params.get("revision")||"");
})();
