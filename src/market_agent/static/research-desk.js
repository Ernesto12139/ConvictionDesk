"use strict";
(() => {
  const publicReplay=document.body.dataset.runtimeMode==="PUBLIC_DEMO";
  // A missing direction is a real research outcome, never a blank or a HOLD.
  const directionLabel = value => value == null || value === ""
    ? "方向未形成（UNDETERMINED）" : String(value);
  let demoManifest=null;
  let active = null, viewGeneration = 0, listGeneration = 0, sending = false;
  let accountsSnapshot = null;
  let historicalView = false;
  let attentionDraft = null;
  let filingDraft = null;
  let selectedResearchMode = "AUTO", availableProfiles = [], profileDefaults = {}, selectedProvider = null, researchConfigured = false;
  let configurationReason = "正在读取模型配置；尚未开始研究。";
  const effectiveMode = () => selectedResearchMode === "DEEP" ? "DEEP" : "FAST";
  function profileForMode(mode) {
    return availableProfiles.find(p => p.mode === mode && p.provider === selectedProvider)
      || availableProfiles.find(p => p.profile_id === profileDefaults[mode])
      || availableProfiles.find(p => p.mode === mode) || null;
  }
  function selectResearchMode(value) {
    selectedResearchMode = value;
    document.querySelectorAll("[data-research-depth]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.researchDepth === value)));
    const profile = profileForMode(effectiveMode());
    if (profile) $("desk-model-profile").value = profile.profile_id;
    $("desk-model-readout").textContent = profile ? `当前模型：${profile.model}` : "模型尚未配置";
    if(profile && [...$("desk-provider").options].some(o=>o.value===profile.provider))$("desk-provider").value=profile.provider;
    $("desk-mode-help").textContent = researchConfigured ? ({AUTO: "先快速核查；是否深入，由你再次确认。", FAST: "先用已有证据形成判断，不自动补查。", DEEP: "围绕关键缺口做有预算的补查；费用确认后才开始。"})[value] : configurationReason;
  }
  document.querySelectorAll("[data-research-depth]").forEach(button => button.addEventListener("click", () => selectResearchMode(button.dataset.researchDepth)));
  $("desk-provider").addEventListener("change", () => {selectedProvider = $("desk-provider").value; selectResearchMode(selectedResearchMode);});
  function clearAttentionDraft() {
    attentionDraft=null;filingDraft=null;$("desk-attention-context").hidden=true;
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
    const synthetic=source?.evidence?.provider==="synthetic_marks"
      || source?.evidence?.structured_data?.synthetic===true;
    if(synthetic){
      // The packaged demo mark is invented. Saying "行情时点" made it read as a quote.
      return {label:"合成价格标记 · 非行情、非真实报价",day:null};
    }
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
    $("desk-continue").disabled=running || sending || !researchConfigured;
    $("desk-earnings").disabled=running || sending || !task?.focus_symbol;
    $("desk-followup").disabled=running || sending;
    $("desk-followup-model-profile").disabled=running || sending;
    $("desk-retry").disabled=running || sending || !researchConfigured;
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
  const sourceKinds={news:"公司新闻",sec_filing:"公司披露",market_data:"行情",calculation:"确定性计算",portfolio_snapshot:"持仓快照",thesis:"历史投资逻辑",semantic_judgment:"研究背景"};
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
  // Citation honesty: buttons name the saved source index, and a repeat of an
  // already cited source says so instead of reading as new independent support.
  let cited=new Set();
  const sourceIndex=id=>active.sources.findIndex(source=>source.evidence.evidence_id===id)+1;
  function sourceLabel(ids){
    const repeat=ids.every(id=>cited.has(id));ids.forEach(id=>cited.add(id));
    return `${repeat ? "同一来源" : "来源"} ${ids.map(id=>`[${sourceIndex(id) || "?"}]`).join("")}`;
  }
  function leadSentence(paragraph) {
    const text=paragraph.textContent, cut=text.search(/[。！？](?=.)/);
    if(paragraph.children.length || cut<8 || cut>=text.length-1)return;
    paragraph.replaceChildren(node("strong",text.slice(0,cut+1),"desk-lead"),document.createTextNode(text.slice(cut+1)));
  }
  function overview(title, statement, preserveFull=false) {
    const section=node("section",null,"desk-memo-section desk-overview");section.append(node("h3",title));
    if(!statement){section.append(node("p","本轮没有合格材料。","muted"));return section;}
    section.append(node("p",preserveFull ? statement.text : firstSentence(statement.text),"desk-statement"));
    if(statement.evidence_ids.length){const button=node("button",sourceLabel(statement.evidence_ids),"desk-source-button");button.type="button";
      button.addEventListener("click",()=>openEvidence(statement.evidence_ids,button));section.append(button);}
    if(!preserveFull && firstSentence(statement.text)!==statement.text){const details=node("details",null,"desk-full-statement");details.append(node("summary","完整说明与限定条件"),citation(statement));section.append(details);}
    return section;
  }
  // Decision brief (Design Contract v0.3 §3C): up to four distinct drivers,
  // the strongest counterevidence, the change condition and the thesis delta,
  // each shown once. Saved statements are selected, never rewritten or padded.
  function decisionBrief(task) {
    const normalize=text=>text.replace(/[\s，。；、：！？,.!?:;]/g, "");
    const seen=[normalize(task.assessment.core_judgment.text)], fresh=statement=>{
      const text=statement && normalize(statement.text);
      if(!text || seen.includes(text))return false;
      seen.push(text);return true;
    };
    const selected=[];
    for(const statement of [...task.assessment.key_reasons,...task.assessment.support]) {
      if(fresh(statement))selected.push(statement);
      if(selected.length===4)break;
    }
    const brief=node("div",null,"desk-brief");brief.setAttribute("role","group");brief.setAttribute("aria-label","判断要点");
    const drivers=group("决策依据",selected);drivers.classList.add("desk-decision-drivers","desk-brief-item");drivers.dataset.brief="why";
    if(selected.length && selected.length<3)drivers.querySelector("h3").append(node("span",`仅 ${selected.length} 条独立依据`,"desk-brief-count"));
    brief.append(drivers);
    const counter=task.assessment.counterevidence[0], change=task.assessment.action_conditions[0], thesis=task.assessment.action?.thesis_delta;
    for(const [title,statement,kind] of [["最强反证",counter,"counter"],["改变判断的条件",change,"change"],["投资逻辑变化",thesis,"thesis"]]){
      // The saved, cited thesis now leads the report beside the typed Action.
      if(kind==="thesis")continue;
      if(kind!=="thesis")fresh(statement);
      const item=overview(title,statement,true);item.classList.add("desk-brief-item");item.dataset.brief=kind;brief.append(item);
    }
    // Count distinct saved sources behind the brief; one source is shown as one.
    const ids=new Set([...selected,counter,change,thesis].filter(Boolean).flatMap(s=>s.evidence_ids));
    if(ids.size){const note=node("p",null,"desk-source-tally small");
      note.textContent=ids.size===1 ? `以上要点均引用同一来源 [${sourceIndex([...ids][0])}]；本轮共保存 ${task.sources.length} 条资料。` : `以上要点引用 ${ids.size} 个不同来源；本轮共保存 ${task.sources.length} 条资料。`;
      brief.append(note);}
    return brief;
  }
  function citation(statement) {
    const p=node("p",statement.text,"desk-statement");
    for(const id of statement.evidence_ids) {
      const index=active.sources.findIndex(source=>source.evidence.evidence_id===id);
      const source=active.sources[index];
      cited.add(id);
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
    if(task.execution_strategy==="ADAPTIVE")values.push(["研究策略",`自适应（实验）· 停止原因 ${task.loop_trace?.stop_reason || "未记录"}${task.loop_trace?.fallback ? " · 回退 "+task.loop_trace.fallback : ""}`]);
    const grid=node("dl",null,"desk-diagnostic-metrics");
    for(const [title,value] of values){const cell=node("div");cell.append(node("dt",title),node("dd",value));grid.append(cell);}
    section.append(grid);
    // ADAPTIVE only: the server-compiled tool plan, as saved (RUN/SKIP and why).
    const plan=task.loop_trace?.plan?.items || [];
    if(plan.length){const list=node("ul",null,"desk-loop-plan");for(const row of plan)list.append(node("li",`${row.tool} · ${row.decision==="RUN" ? "执行" : "跳过"} · ${row.reason}`));section.append(list);}
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
  // Display a Decimal string exactly, by string shifting only: exponent form,
  // negative zero and insignificant trailing zeros never reach the reader.
  const exactDecimal=value=>{
    if(value==null)return "未知";
    const raw=String(value).trim(), parts=raw.match(/^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
    if(!parts || !(parts[2] || parts[3]))return raw;
    let digits=(parts[2] || "")+(parts[3] || ""), point=(parts[2] || "").length+Number(parts[4] || 0);
    if(point<=0){digits="0".repeat(1-point)+digits;point=1;}
    if(point>digits.length)digits+="0".repeat(point-digits.length);
    const whole=digits.slice(0,point).replace(/^0+(?=\d)/,""), fraction=digits.slice(point).replace(/0+$/,"");
    const sign=parts[1]==="-" && /[1-9]/.test(whole+fraction) ? "-" : "";
    return sign+whole.replace(/\B(?=(\d{3})+(?!\d))/g,",")+(fraction ? "."+fraction : "");
  };
  async function whatifSection(task,section){
    const tags=node("p",null,"desk-whatif-tags");
    for(const text of ["假设价格 · 非实际行情","不下单 · 不写入持仓","原研究方向不变"])tags.append(node("span",text));
    section.append(node("h3","单账户调仓预演"),tags);
    if(publicReplay){section.append(node("p","公开历史回放不提供账户模拟。"));return;}
    let context;
    try{context=await readApi(`/api/research/tasks/${encodeURIComponent(task.task_id)}/what-if?revision=${task.revision}`);}
    catch(_){section.append(node("p","本轮没有已保存的操作方向，可改用当前组合快照预演。","small"));if(window.miaOpenWhatIf){const go=node("button",`用当前组合预演 ${task.focus_symbol}`);go.type="button";go.addEventListener("click",()=>window.miaOpenWhatIf(task.focus_symbol));section.append(go);}return;}
    section.append(node("p",`持仓快照 ${localZone(context.snapshot_as_of)} · 研究截止 ${localZone(context.research_cutoff)}`,"small muted"));
    const form=node("form",null,"desk-whatif-form"),result=node("section");result.setAttribute("aria-live","polite");
    const field=(label,input)=>{const wrap=node("label",label);wrap.append(input);form.append(wrap);return input;};
    const input=(value="")=>{const el=node("input");el.type="text";el.inputMode="decimal";el.required=true;el.value=value;return el;};
    const account=field("模拟账户",node("select"));
    // Default to the account that actually holds the researched symbol, and say
    // what each account holds, so a zero-share account is never a silent default.
    const held=a=>a.positions.find(p=>p.symbol===task.focus_symbol)?.shares;
    context.accounts.forEach(a=>{const option=node("option",`${a.name} · ${held(a) ? `持有 ${exactDecimal(held(a))} 股` : `未持有 ${task.focus_symbol}`}`);option.value=a.account_id;account.append(option);});
    // One holder: preselect it. Several holders: the reader must choose.
    const holders=context.accounts.filter(held);
    if(holders.length===1)account.value=holders[0].account_id;
    else if(holders.length>1){const pick=node("option",`选择账户 · ${holders.length} 个账户持有 ${task.focus_symbol}`);pick.value="";pick.disabled=true;account.prepend(pick);account.value="";account.required=true;}
    const side=field("增减方向",node("select"));
    [["BUY","增加股数"],["SELL","减少股数"]].forEach(([v,t])=>{const option=node("option",t);option.value=v;side.append(option);});
    const shares=field("增减股数（正数）",input()),price=field(`${task.focus_symbol} 假设成交价 · USD/股`,input()),fee=field("交易费用 · USD",input("0"));
    const referenceMark=field("比较基准价（选填，假设 USD/股，非实际行情）",input());referenceMark.required=false;
    section.addEventListener("technical-price-selected",event=>{
      price.value=event.detail.price;referenceMark.value=event.detail.reference;
      form.dispatchEvent(new Event("input",{bubbles:true}));
      section.scrollIntoView({behavior:"smooth",block:"start"});price.focus();
    });
    const advanced=node("details",null,"desk-whatif-advanced");advanced.append(node("summary","费用、其他持仓计价与资本假设"));form.append(advanced);
    const day=field("假设情景日期",node("input"));day.type="date";day.required=true;day.value=context.snapshot_as_of.slice(0,10);day.min=day.value;
    const other=node("fieldset"),marks=new Map();form.append(other);
    const accountScope=node("p",null,"desk-account-scope");form.prepend(accountScope);
    const renderMarks=()=>{marks.clear();other.replaceChildren(node("legend","其他持仓的假设计价（选填）"));
      const current=context.accounts.find(a=>a.account_id===account.value);
      if(!current){accountScope.replaceChildren(node("span",`请先选择账户；多个账户持有 ${task.focus_symbol}`));return;}
      accountScope.replaceChildren(node("strong",current.name),node("span",`${task.focus_symbol} ${exactDecimal(held(current) || "0")} 股`),node("span",`现金 ${exactDecimal(current.cash)} USD`),node("span","仅此账户"));
      other.append(node("p","缺少其他持仓价格时，账户净值、权重与集中度保持未知。","small"));
      current.positions.filter(p=>p.symbol!==task.focus_symbol).forEach(p=>{const wrap=node("label",`${p.symbol} · ${p.shares} 股 · 假设计价 USD/股`),value=input();value.required=false;wrap.append(value);other.append(wrap);marks.set(p.symbol,value);});
    };account.addEventListener("change",renderMarks);renderMarks();
    const attest=node("input");attest.type="checkbox";attest.required=true;
    const attestation=node("label","确认采用自定假设价格，且快照后无拆股、换股或股类变化（未独立核实）。");attestation.prepend(attest);form.append(attestation);
    advanced.append(node("p","仅 USD 现金账户；支持10位小数碎股，不接受科学计数法。费用不模拟券商结算舍入。平均成本损益不是税务核算；HHI 为股票权重平方和，分母为账户 NAV（含现金），不是股票内部归一化 HHI。","small muted"));
    for(const element of [fee.parentElement,referenceMark.parentElement,day.parentElement,other])advanced.append(element);
    const submit=node("button","预览组合变化");submit.type="submit";form.append(submit,result);section.append(form);
    form.addEventListener("invalid",event=>{const drawer=event.target.closest("details");if(drawer)drawer.open=true;},true);
    let inputVersion=0;
    const changed=()=>{inputVersion++;result.replaceChildren();};
    form.addEventListener("input",changed);form.addEventListener("change",changed);
    const refusals={OVERSELL_NOT_ALLOWED:"减少股数超过该账户持股。",INSUFFICIENT_CASH_INCLUDING_FEES:"该账户现金不足以支付成交额和费用。",KNOWN_CAPITAL_OR_SHARE_CLASS_CONFLICT:"已有资料显示股类或资本结构冲突，不能在未调和的口径上模拟。",POSITIVE_SHARES_AND_PRICES_REQUIRED:"股数与所有填写的价格必须大于零。"};
    form.addEventListener("submit",async event=>{event.preventDefault();if(submit.disabled)return;
      const payload={revision:task.revision,account_id:account.value,symbol:task.focus_symbol,side:side.value,shares:shares.value.trim(),fill_price:price.value.trim(),reference_mark:referenceMark.value.trim() || null,fee:fee.value.trim(),currency:"USD",scenario_date:day.value,unchanged_share_basis_attested:attest.checked,other_prices:Object.fromEntries([...marks].filter(([,v])=>v.value.trim()).map(([s,v])=>[s,v.value.trim()]))};
      const submittedVersion=inputVersion;submit.disabled=true;result.replaceChildren(node("p","正在计算假设调仓影响。"));
      try{const value=await api(`/api/research/tasks/${encodeURIComponent(task.task_id)}/what-if`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        if(submittedVersion!==inputVersion){result.replaceChildren(node("p","输入已改变，请重新预览。"));return;}
        if(value.task_id!==task.task_id || value.revision!==task.revision)throw Error("模拟版本不匹配");
        result.replaceChildren(node("h4",`${context.accounts.find(a=>a.account_id===value.input.account_id)?.name || value.input.account_id} · 单账户预演`));
        result.classList.add("desk-whatif-result");
        const rows=[["股数","shares"],["账户现金 · USD","cash"],["账户净值 · USD","nav"],["标的权重","focus_weight"]];
        const list=node("dl",null,"desk-valuation-list desk-whatif-deltas");rows.forEach(([label,key])=>{const cell=node("div"),format=v=>v==null?"未知":key.includes("weight")?percent(v):exactDecimal(v);cell.append(node("dt",label),node("dd",`${format(value.before[key])} → ${format(value.after[key])}`));list.append(cell);});result.append(list);
        // Three separately labelled effects: the trade itself, repricing of the
        // existing position against the optional reference, and modeled P&L.
        const usd=v=>`${exactDecimal(v)} USD`, split=node("dl",null,"desk-whatif-split");split.setAttribute("aria-label","影响拆分");
        const effects=[["交易本身",usd(value.trade_nav_delta),"同一假设价格成交，净资产只因费用变化"],
          ["既有持仓重估",value.market_repricing_delta==null ? "未计算" : usd(value.market_repricing_delta),value.market_repricing_delta==null ? "填写比较基准价后单独计算" : `原 ${exactDecimal(value.before.shares)} 股：比较基准价 → 假设成交价`]];
        if(value.total_nav_delta!=null)effects.push(["合计净资产变化",usd(value.total_nav_delta),"交易本身 + 既有持仓重估"]);
        effects.push(["情景损益",value.modeled_realized_pnl==null ? `未实现 ${usd(value.modeled_unrealized_pnl_after)}` : `已实现 ${usd(value.modeled_realized_pnl)} · 未实现 ${usd(value.modeled_unrealized_pnl_after)}`,"相对总成本基础，平均成本口径"]);
        for(const [label,amount,note] of effects){const cell=node("div");cell.append(node("dt",label),node("dd",amount),node("small",note));split.append(cell);}
        result.append(split);
        const accounting=node("div");accounting.append(node("p",`最大单一持仓权重 ${percent(value.before.largest_weight)} → ${percent(value.after.largest_weight)}；股票集中度 HHI（账户 NAV 分母） ${value.before.hhi_nav==null?"未知":Number(value.before.hhi_nav).toFixed(4)} → ${value.after.hhi_nav==null?"未知":Number(value.after.hhi_nav).toFixed(4)}`,"small"));accounting.append(node("p",`现金变化 ${usd(value.cash_delta)}；费用 ${usd(value.input.fee)}；成交金额 ${usd(value.gross_value)}；模拟前总成本基础 ${usd(value.focus_cost_before)}；模拟后总成本基础 ${usd(value.focus_cost_after)}。`,"small"));
        const missing=[...new Set([...value.before.missing_price_symbols,...value.after.missing_price_symbols])];if(missing.length)result.append(node("p",`未计价持仓：${missing.join("、")}。未知值没有按零处理。`,"small"));
        // A saved action can legitimately have no direction. Printing the raw JSON
        // null here read as a missing value; it is an honest UNDETERMINED.
        const direction = directionLabel(value.original_action);
        result.append(node("p",`原研究方向 ${direction}${value.original_action ? " 不变" : ""} · 未下单、未保存到持仓或研究历史`,"small muted"));
        const detail=node("details");detail.append(node("summary","成本、集中度与完整计算回执"),accounting,node("pre",JSON.stringify(value,null,2)));result.append(detail);
      }catch(error){const code=typeof error.detail==="string"?error.detail:error.message;result.replaceChildren(node("p",refusals[code] || (error.status===422?"输入未通过检查：请使用普通十进制正数、最多10位小数；确认日期、费用及账户范围，再重新预览。":"暂时无法读取计算结果。请先刷新已保存研究，再重试本地预览；无需重新研究，也不会下单。"),"desk-error"));}
      finally{submit.disabled=false;}
    });
  }

  function technicalSection(task,section,whatif){
    section.append(node("h3","有来源的技术区间"),node("p","尚无获准的真实 OHLCV 数据接入。可用自有合成行情复核算法，或提交同格式的合成输入；不会把它当作真实标的价格。","small"));
    if(publicReplay){section.append(node("p","公开只读回放不计算新的技术区间。","small"));return;}
    const details=node("details");details.append(node("summary","复核 OHLCV 与算法（仅合成示例）"));
    const form=node("form",null,"desk-technical-form"),load=node("button","载入虚构 OHLCV 示例");load.type="button";
    const label=node("label","带来源、时间与复权口径的 OHLCV JSON"),data=node("textarea");data.rows=6;data.required=true;data.spellcheck=false;label.append(data);
    const submit=node("button","计算候选技术区间 · 无模型费用");submit.type="submit";
    const result=node("section");result.setAttribute("aria-live","polite");form.append(load,label,submit,result);details.append(form);section.append(details);
    let version=0;data.addEventListener("input",()=>{version++;result.replaceChildren();});
    load.addEventListener("click",async()=>{load.disabled=true;try{const value=await readApi(`/api/research/tasks/${encodeURIComponent(task.task_id)}/technical-example?revision=${task.revision}`);data.value=JSON.stringify(value.series,null,2);data.dispatchEvent(new Event("input"));}catch(_){result.replaceChildren(node("p","示例暂不能读取，请刷新已保存研究后重试。","desk-error"));}finally{load.disabled=false;}});
    const errors={PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION:"价格出现至少15%的跨日跳变；可能是重大新闻或公司行动。请核查来源与复权口径，不能仅凭无拆股标签生成区间。",REAL_SOURCE_RIGHTS_ADAPTER_NOT_ADMITTED:"真实数据尚未获准接入：需要使用权与历史版本证明，当前不计算真实技术价位。",STALE_OR_FUTURE_SERIES:"行情窗口不是研究截止点最近一个已完成交易日，请更换日期合格的完整序列。",MISSING_TRADING_SESSION:"缺少交易日 K 线，请补齐；不会用插值伪造。",SPLIT_BASIS_UNRECONCILED:"拆股口径未知或发生变化，请先调和完整 OHLCV，当前拒绝计算。",AT_LEAST_21_COMPLETED_BARS_REQUIRED:"至少需要21根连续的已完成 regular-session K线。",INCOMPLETE_OR_POST_CUTOFF_BAR:"存在未完成、晚于截止点或来源时间不匹配的 K线，请核对时点。",HALT_STATUS_UNQUALIFIED:"停牌状态未明确，不生成技术区间。",SAVED_EVIDENCE_CAPITAL_CONFLICT:"已保存证据包含股类或资本行动冲突，先核查口径。"};
    form.addEventListener("submit",async event=>{event.preventDefault();if(submit.disabled)return;let series;
      try{if(data.value.length>200000)throw Error();series=JSON.parse(data.value);}catch(_){result.replaceChildren(node("p","请输入有效且不超过200KB的 JSON。价格请用十进制字符串。","desk-error"));return;}
      const submitted=version;submit.disabled=true;result.replaceChildren(node("p","正在核查来源、交易日与计算区间。"));
      try{const value=await api(`/api/research/tasks/${encodeURIComponent(task.task_id)}/technical-zones`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({revision:task.revision,series})});
        if(submitted!==version){result.replaceChildren(node("p","输入已改变，请重新计算。"));return;}
        if(value.status!=="AVAILABLE_SYNTHETIC"){result.replaceChildren(node("p",errors[value.reason] || `当前输入不合格（${value.reason}）。请核查时段、复权、股类及完整 OHLCV，再计算。`,"desk-error"));return;}
        result.replaceChildren(node("h4","合成教学区间 · LIVE_DATA_NOT_VERIFIED"),node("p",`原研究方向 ${directionLabel(value.original_action)}${value.original_action ? " 不变" : ""}。算法区间不是目标价或买卖指令。最后交易日 ${value.last_session}；完成下一交易日 K线后须重算。`,"small"));
        result.append(node("p",`SMA20 ${money(value.sma20)}；简单均值 ATR14 约 ${money(value.atr14)}。支撑从10日最低价向上0.5 ATR，压力从最高价向下0.5 ATR。复权口径：${value.series.adjustment}；仅常规时段。`,"small"));
        for(const [key,title] of [["support","候选支撑观察区"],["resistance","候选压力观察区"]]){const zone=value.zones[key];result.append(node("p",`${title}：${exactDecimal(zone[0])}～${exactDecimal(zone[1])} USD`));}
        result.append(node("p",`失效条件：收盘低于 ${exactDecimal(value.zones.support[0])} 或高于 ${exactDecimal(value.zones.resistance[1])}、下一根已完成 K线、拆股或停牌变化。它们触发重新核查，不自动改变 Action。`,"small"));
        const choose=node("button","将合成支撑区上沿用于账户假设（不会成交）");choose.type="button";choose.addEventListener("click",()=>{if(!whatif.querySelector('.desk-whatif-form')){result.append(node("p","没有可用的账户快照，先选择带持仓快照的已保存研究。","desk-error"));return;}whatif.dispatchEvent(new CustomEvent("technical-price-selected",{detail:{price:value.zones.support[1],reference:value.last_close}}));});result.append(choose);
        const receipt=node("details");receipt.append(node("summary","来源、算法参数与完整可复算回执"),node("pre",JSON.stringify(value,null,2)));result.append(receipt);
      }catch(_){result.replaceChildren(node("p","未能计算：请核对 JSON 字段、正数 OHLC、整数成交量及日期；若服务暂不可用，刷新本轮后重试。不会重新研究或下单。","desk-error"));}finally{submit.disabled=false;}
    });
  }

  async function valuationSection(task,section){
    try{
      const report=await readApi(`/api/research/tasks/${encodeURIComponent(task.task_id)}/valuation?revision=${task.revision}`);
      if(!section.isConnected || active?.task_id!==task.task_id || active?.revision!==task.revision)return;
      if(report.task_id!==task.task_id || report.revision!==task.revision || Date.parse(report.cutoff)!==Date.parse(task.cutoff))throw Error("valuation_revision_mismatch");
      section.replaceChildren(node("summary","估值、财务与价格情景"));
      section.append(node("p","按本轮已保存来源计算；打开报告不会获取新价格或重新研究。参考倍数不等于便宜、合理价值或目标价。","small muted"));
      const reference=report.local_capital_reference;
      if(reference?.status==="AVAILABLE"){
        const referenceSection=node("section",null,"desk-provider-reference");
        referenceSection.setAttribute("aria-label","Provider 报告参考市值");
        referenceSection.append(node("h4","Provider 报告参考市值"),node("p",`${exactDecimal(reference.value)} USD`,"desk-valuation-number"));
        referenceSection.append(node("p",`Massive · 观察于 ${localZone(reference.observed_at)}；字段生效时点未知。来源报告参考，未独立计算，不是实时市值。`,"small"));
        referenceSection.append(node("p","单独读取的本地快照，观察时间不晚于本轮截止点；不属于原研究的冻结证据。仅供个人查看，未发送给模型、未用于下方市值或倍数计算，不改变原结论。","small muted"));
        if(report.metrics.market_cap_reference?.value!=null)referenceSection.append(node("p","来源报告值与下方本系统计算值属于不同资格层级；价格、股数时点和股类口径尚未逐项调和，两者不可互换。","small"));
        const metadata=node("details",null,"desk-provider-meta");metadata.append(node("summary","来源、日期与股类限制"));
        const close=reference.regular_close_reference;
        metadata.append(node("p",`SOURCE_REPORTED_REFERENCE · 原字段 ${reference.raw_field} · USD · Provider 口径：最近收盘价 × 加权等价股数。多股类经济权益、增发、回购、转换和拆股对齐未独立核实。`,"small"));
        metadata.append(node("p",`市值所用价格与股数日期未知。另存常规收盘 ${exactDecimal(close.value)} USD/股 · ${close.price_as_of} · 未复权；不能证明它是该市值的价格输入。盘后价未用于此显示。`,"small"));
        for(const [label,uri,id]of [["市值原始来源",reference.source_uri,reference.source_id],["常规收盘来源",close.source_uri,close.source_id]]){
          const link=node("a",label);link.href=uri;link.target="_blank";link.rel="noopener noreferrer";
          metadata.append(link,node("p",id,"small muted"));
        }
        referenceSection.append(metadata);section.append(referenceSection);
      }else if(reference){
        section.append(node("p",reference.status==="STALE"?"本地 Provider 快照已超过七天，未作为本轮近期参考展示；需另行取得合格新来源，打开本页不会自动刷新。":reference.reason==="NOT_OBSERVED_AT_RESEARCH_CUTOFF"?"本地 Provider 参考取得时间晚于本轮截止点，未并入历史研究。":"本地 Provider 参考不可用或完整性核查失败；未以缓存、零或模型猜测补齐。","small muted"));
      }
      const price=report.price_reference,shares=report.shares_reference;
      const sessions={COMPLETED_CLOSE:"日终收盘",REGULAR:"常规交易",PRE_MARKET:"盘前",POST_MARKET:"盘后",OVERNIGHT:"隔夜",UNKNOWN:"时段未核实"};
      section.append(node("p",price?`价格参考 ${exactDecimal(price.value)} USD/股 · ${localZone(price.as_of)} · ${sessions[price.session] || "时段未核实"}；feed delay 未核实，不称为实时。`:"价格参考未知：本轮没有符合期间、币种与时点要求的来源。","small"));
      section.append(node("p",shares?`报告普通股 ${exactDecimal(shares.value)} 股 · 报告时点 ${shares.end} · 披露 ${localZone(shares.available_at)}。这是有日期的报告数，不等于实时流通股本。`:"报告股数未知；不使用加权平均股数代替当前普通股，也不猜测市值。","small"));
      section.append(node("p",report.capital_basis_status==="VERIFIED_NO_CHANGE"?"来源记录已核查所覆盖期间的拆股、报告股数变化与股类口径；市值和倍数仍是带日期的参考。":"拆股、报告股数变化或股类口径未核实：本系统计算的市值与适用倍数保持未知。亏损或零分母仍明确为不适用。","small muted"));
      if(report.ttm_period)section.append(node("p",`历史 TTM 期间 ${report.ttm_period.start} — ${report.ttm_period.end}；不是公司预测。`,"small muted"));
      const names={ttm_revenue:"TTM 营收",ttm_net_income:"TTM 净利润",ttm_operating_cash_flow:"TTM 经营现金流",ttm_diluted_eps:"TTM 摊薄 EPS",market_cap_reference:"报告股数市值参考",pe_ttm:"TTM 市盈率参考",price_sales_ttm:"TTM 市销率参考",enterprise_value:"企业价值 EV"};
      const hasValues=Object.values(report.metrics).some(row=>row.value!=null || row.data_status==="NOT_MEANINGFUL");
      const list=node("dl",null,"desk-valuation-list");
      for(const [key,label]of Object.entries(names)){
        const row=report.metrics[key];if(!row)continue;
        const cell=node("div"),status=row.data_status || row.status;
        cell.append(node("dt",label));
        const value=status==="NOT_MEANINGFUL"?"不适用 · NOT_MEANINGFUL":row.value==null?"未知 · UNKNOWN":`${row.display_approximate?"约 ":""}${exactDecimal(row.display_value ?? row.value)} ${row.unit==="multiple"?"倍":row.unit==="USD/shares"?"USD/股":row.unit}`;
        cell.append(node("dd",value,"desk-valuation-number"));
        const detail=node("details",null,"desk-valuation-meta");detail.append(node("summary","期间、时点与来源"));
        detail.append(node("p",`${status} · 币种 ${row.currency || "不适用"} · 可用时点 ${localZone(row.as_of)}`,"small muted"));
        if(row.display_approximate)detail.append(node("p",`完整计算值：${row.value} ${row.unit}；上方仅对展示精度取近似。`,"small"));
        if(row.period)detail.append(node("p",Object.entries(row.period).map(([key,value])=>`${({start:"期间起",end:"期间止",price_as_of:"价格时点",shares_reported_end:"股数报告时点"})[key] || key} ${value}`).join(" · "),"small"));
        const reasons={QUALIFIED_TOTAL_DEBT_AND_ALIGNED_CASH_NOT_AVAILABLE:"缺少有资格且时点对齐的债务与现金，EV 保持未知。",NONPOSITIVE_DENOMINATOR:"利润或分母非正，不适用该估值倍数。",QUALIFIED_ALIGNED_INPUTS_REQUIRED:"缺少有资格且时点可比的输入。",QUALIFIED_PRICE_AND_SHARES_REQUIRED:"缺少合格价格或报告普通股数。",FOUR_CONTIGUOUS_QUALIFIED_QUARTERS_REQUIRED:"缺少四个连续且有来源的兼容季度。",INCOMPATIBLE_CONCEPT_OR_RESTATEMENT_BASIS:"概念、期间或报告版本不兼容，不能混算。"};
        if(reasons[row.reason])detail.append(node("p",reasons[row.reason],"small"));
        if(row.reason==="CAPITAL_BASIS_UNVERIFIED")detail.append(node("p","缺少完整的公司行动与股本口径核查，不能把不同口径的价格、股数或 EPS 混算。","small"));
        const ids=(row.source_id || row.evidence_ids || []).filter(id=>task.sources.some(s=>s.evidence.evidence_id===id));
        if(ids.length){const button=node("button","查看计算依据","desk-source-button");button.type="button";button.addEventListener("click",()=>openEvidence(ids,button));detail.append(button);}
        else detail.append(node("p","缺失输入没有可引用的数值；不会以零或模型猜测补齐。","small muted"));
        cell.append(detail);list.append(cell);
      }
      if(hasValues)section.append(list);
      else{const gaps=node("details");gaps.append(node("summary","本轮估值缺口"),list);section.append(node("p","本轮尚无可计算的合格 TTM／估值输入。判断仍须保留这个缺口。"),gaps);}
      if(!publicReplay)datedPriceForm(task,report,section);
    }catch{
      if(!section.isConnected)return;
      section.replaceChildren(node("summary","估值、财务与价格情景"),node("p","估值依据暂不可读取；原研究和引用仍可查看。读取失败不代表数值为零。","small"));
    }
  }
  function datedPriceForm(task,frozen,section){
    const entry=node("details",null,"desk-dated-price"),form=node("form",null,"desk-dated-form");
    entry.append(node("summary","使用指定日期价格评估"));
    form.append(node("p","只在本页用日期化 SEC 普通股数与 TTM 做确定性计算，不调用模型、不修改原研究。收据离开页面后不保存。用户观察不等于系统核实行情。","small muted"));
    const fields=node("div",null,"desk-price-fields");
    const field=(text,control)=>{const label=node("label",text);label.append(control);fields.append(label);return control;};
    const select=options=>{const s=node("select");for(const [value,text]of options){const o=node("option",text);o.value=value;s.append(o);}return s;};
    const kind=field("价格身份",select([["USER_SUPPLIED_SCENARIO","我设定的假设情景价"],["USER_SUPPLIED_OBSERVATION","我声称观察到的价格 · 未核实"]]));
    const price=node("input");price.type="text";price.inputMode="decimal";price.required=true;price.maxLength=26;price.placeholder="例如 12.50";price.autocomplete="off";
    field(`${task.focus_symbol || "标的"} · USD/股`,price);
    const day=node("input");day.type="date";day.required=true;
    day.value=frozen.price_reference?.session_date || "";
    day.max=new Intl.DateTimeFormat("en-CA",{timeZone:"America/New_York",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(task.cutoff));
    field("价格日期 · 美东日期",day);
    const session=field("观察时段",select([["COMPLETED_CLOSE","该日未复权收盘"],["REGULAR","未复权常规时段"]]));
    const note=node("input");note.type="text";note.required=true;note.maxLength=400;note.placeholder="说明自有观察或情景设定的来源";field("来源说明",note);
    const comparisonPrice=node("input");comparisonPrice.type="text";comparisonPrice.inputMode="decimal";comparisonPrice.maxLength=26;comparisonPrice.autocomplete="off";
    comparisonPrice.placeholder="可选，例如 12.50；仅自己设定的假设";
    field("对照假设价 · USD/股（可选）",comparisonPrice);
    form.append(fields);
    form.append(node("p","对照价必须是你自行设定的假设，不是复制的 Provider 报价。两价使用同一日期、股数和 TTM；差异只说明价格敏感度，不表示市场涨跌或收益。","small muted"));
    const checkbox=(text,required)=>{const label=node("label",null,"desk-price-condition"),input=node("input");input.type="checkbox";input.required=required;label.append(input,node("span",text));form.append(label);return input;};
    const rights=checkbox("我有权将此自有输入用于本地派生计算；不是从仅限展示或禁止派生的 Provider 复制价格。系统未独立核实该授权。",true);
    form.append(node("p","以下是影响结果的关键假设，未由系统确认。已知股类或公司行动冲突时，即使勾选仍会拒绝计算。","small"));
    const continuity=checkbox("假设从 SEC 股数有效日至价格日，拆股、反拆股、股票股息、增发、回购和转换均未改变此股数口径。",true);
    const classes=checkbox("假设全部报告普通股可按输入股票类别的价格计价，经济权益等价；不是仅凭一类股价推算未调和的多股类。",true);
    const income=checkbox("额外假设合并 TTM 净利润可全部归属普通股权益，无优先股或非控股扣减；否则不输出权益值／净利润倍数。",false);
    form.append(node("p",`仅使用本轮截止点 ${localZone(task.cutoff)} 已公开的来源；晚于截止点的价格须在新的分析上下文评估。参考市销率不含债务／现金调整；此处不生成目标价或自动 ADD。`,"small muted"));
    const submit=node("button","仅本地计算 · 无模型费用");submit.type="submit";
    const result=node("section",null,"desk-dated-result");result.setAttribute("aria-live","polite");
    form.append(submit,result);entry.append(form);section.append(entry);
    const mode=()=>{session.disabled=kind.value==="USER_SUPPLIED_SCENARIO";};kind.addEventListener("change",mode);mode();
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(submit.disabled)return;
      const observed=kind.value==="USER_SUPPLIED_OBSERVATION";
      const payload={revision:task.revision,symbol:task.focus_symbol,price:price.value.trim(),comparison_price:comparisonPrice.value.trim() || null,currency:"USD",price_date:day.value,
        session:observed?session.value:"HYPOTHETICAL",adjustment_basis:"UNADJUSTED",kind:kind.value,
        source_basis:observed?"OWN_OBSERVATION":"HYPOTHETICAL",source_note:note.value,
        local_calculation_rights_attested:rights.checked,assumptions:{unchanged_since_share_date:continuity.checked,
          entire_common_equity_at_input_class_price:classes.checked,consolidated_income_available_to_common:income.checked}};
      submit.disabled=true;result.replaceChildren(node("p","正在本地检查来源与计算。","small"));
      try{
        const value=await api(`/api/research/tasks/${encodeURIComponent(task.task_id)}/dated-valuation`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        if(!entry.isConnected || active?.task_id!==task.task_id || active?.revision!==task.revision)return;
        const report=value.report;
        if(report.task_id!==task.task_id || report.revision!==task.revision || Date.parse(report.research_cutoff)!==Date.parse(task.cutoff))throw Error("receipt_context_mismatch");
        result.replaceChildren(node("h4",report.qualification_status==="UNAVAILABLE"?"本次输入无法形成参考估值":report.qualification_status==="CONDITIONAL_REFERENCE"?"条件参考 · 用户观察尚未核实":"假设情景 · 非实际行情"));
        const reasonLabels={TASK_INSTRUMENT_OR_REVISION_MISMATCH:"标的或研究版本不匹配。",COMPLETED_RESEARCH_REQUIRED:"需要已完成的研究版本。",PRICE_AFTER_RESEARCH_CUTOFF_CREATE_NEW_CONTEXT:"价格晚于研究截止点；请使用新的分析上下文，不能并入旧判断。",DATE_ONLY_OBSERVATION_REQUIRES_PRIOR_MARKET_DATE:"只有日期无法证明当日观察早于研究截止时刻，请使用此前交易日。",OBSERVATION_NON_WEEKDAY:"该观察日期不是工作日；系统不把它当作交易收盘。",SHARE_DATE_MUST_PRECEDE_PRICE_WITHIN_100_DAYS:"SEC 股数日期须不晚于价格日，且相隔最多 100 天。",SHARE_FILING_NOT_PUBLIC_BY_PRICE_DATE:"该股数在价格日尚未公开。",DATE_ONLY_PRICE_VS_FILING_TIME_AMBIGUOUS:"同日披露与日期级价格的先后不明。",SEC_COMMON_SHARE_CONCEPT_AND_ACCESSION_REQUIRED:"缺少合格普通股概念或 SEC accession；不用加权平均摊薄股数。",SEC_SHARE_SOURCE_INTEGRITY:"SEC 股数来源校验失败。",QUALIFIED_DATED_SEC_COMMON_SHARES_REQUIRED:"本研究没有合格的日期化 SEC 普通股数。",KNOWN_CAPITAL_BASIS_CONFLICT:"已有来源显示资本口径冲突。",MULTI_CLASS_OR_INSTRUMENT_SCOPE_UNRECONCILED:"多股类或股票身份尚未调和，不能用单一价格推算。",KNOWN_CAPITAL_ACTION_OR_CLASS_CONFLICT:"已有公司行动或股类冲突，用户假设不能覆盖它。",EXPLICIT_CAPITAL_CONTINUITY_AND_CLASS_ASSUMPTIONS_REQUIRED:"须明确接受资本连续性与股类计价假设。",LOCAL_DERIVATION_RIGHTS_NOT_ATTESTED:"尚未确认本地计算使用权。",DISPLAY_ONLY_PROVIDER_NOT_ADMITTED_FOR_DERIVATION:"仅供展示的 Provider 数据不能复制到此处绕过派生许可。",SERVER_VERIFIED_PROVIDER_DERIVATION_RIGHTS_REQUIRED:"第三方自动行情须先由服务端核实派生计算权限。"};
        Object.assign(reasonLabels,{OBSERVATION_CALENDAR_COVERAGE_UNAVAILABLE:"该年份不在已核对的交易日历范围，不能将观察日期当作已验证交易日。可另用明确的假设情景。",OBSERVATION_PUBLISHED_MARKET_CLOSURE:"该日期是已公布的全日休市日，不能作为常规交易观察。",SEC_COMMON_SHARE_COUNT_INVALID:"普通股总数不是有效的正整数或超出数值边界，不能据此计算。",SHARE_DATE_MUST_PRECEDE_PRICE_WITHIN_120_DAYS:"股数须不晚于价格日；最多 100 天可作条件参考，101—120 天仅作旧股数假设情景；超出则拒绝。"});
        for(const code of report.rejected_inputs)result.append(node("p",reasonLabels[code] || `输入资格未满足：${code}`,"small"));
        const qualificationLabels={MISSING_POINT_IN_TIME_SINGLE_LISTING_IDENTITY:"没有在价格日前确认的单一上市身份，结果降级为假设情景；勾选股类假设不能提升资格。",SHARES_OLDER_THAN_100_DAYS_SCENARIO_ONLY:"股数距价格日已超过 100 天，仅展示 120 天以内的旧股数假设，不是当前参考股本。"};
        for(const code of report.qualification_notes || [])result.append(node("p",qualificationLabels[code] || code,"small desk-price-assumptions"));
        if(observed && report.qualification_status==="ILLUSTRATIVE_SCENARIO")result.append(node("p","输入仍是你声称观察到的价格，未独立核实；这里降级的是股本估值资格，不表示市场事实得到确认。","small"));
        if(report.qualification_status!=="UNAVAILABLE"){
          result.append(node("p","结果仅在以下未核实条件成立时适用：股本自报告日未变化，全部普通股与输入股类经济等价。它不是已确认的当前市值。","desk-price-assumptions"));
          if(report.assumptions.consolidated_income_available_to_common)result.append(node("p","权益值／净利润还假设合并净利润全部归属普通股；不等同于 Provider 标准 P/E。","small"));
          const list=node("dl",null,"desk-valuation-list");
          for(const [key,label]of [["reference_equity_value","参考普通股权益值"],["reference_ps","参考权益值／TTM 营收"],["reference_pe","参考权益值／TTM 净利润"]]){
            const metric=report.metrics[key],cell=node("div");
            cell.append(node("dt",label),node("dd",metric.status==="NOT_MEANINGFUL"?"不适用 · NOT_MEANINGFUL":metric.value==null?"未知 · UNKNOWN":`${metric.display_approximate?"约 ":""}${exactDecimal(metric.display_value ?? metric.value)} ${metric.unit==="multiple"?"倍":"USD"}`,"desk-valuation-number"));
            if(metric.reason==="COMMON_INCOME_ATTRIBUTION_NOT_ESTABLISHED")cell.append(node("p","净利润归属普通股的口径未确立。","small"));
            if(metric.reason==="QUALIFIED_ALIGNED_TTM_REQUIRED")cell.append(node("p","缺少在价格日已公开、期间对齐且足够近期的 TTM。","small"));
            list.append(cell);
          }
          result.append(list);
          const difference=report.scenario_difference;
          if(difference?.status==="ILLUSTRATIVE_SCENARIO"){
            const delta=node("section",null,"desk-price-difference");
            delta.append(node("h4","只改变价格，哪些结果会变？"),node("p",`自定对照假设价 ${exactDecimal(difference.comparison_price)} → 本次输入 ${exactDecimal(difference.input_price)} USD/股；输入差异约 ${exactDecimal(difference.price_delta_percent_display)}%。这是同一日期的敏感度比较，不是实际市场变化。`,"small"));
            const values=node("dl",null,"desk-valuation-list");
            for(const [key,label]of [["reference_equity_value","普通股权益值差异"],["reference_ps","权益值／营收倍数差异"],["reference_pe","权益值／净利润倍数差异"]]){
              const row=difference.metrics[key],cell=node("div"),unit=row.unit==="USD"?"USD":"倍";
              cell.append(node("dt",label),node("dd",row.delta==null?"未知／不适用":`${row.delta_display_approximate?"约 ":""}${exactDecimal(row.delta_display_value)} ${unit}`,"desk-valuation-number"));
              if(row.comparison_value!=null)cell.append(node("p",`对照值 ${row.comparison_display_approximate?"约 ":""}${exactDecimal(row.comparison_display_value)} ${unit}；完整精度见收据。`,"small muted"));
              values.append(cell);
            }
            delta.append(values,node("p","未改变：已保存的经营事实、TTM、股本假设、投资逻辑和持仓。价格更低不会确认需求、回款或消除反证。","small"));result.append(delta);
          }
        }
        result.append(node("p",`价格日 ${report.price_as_of} · 股数有效日 ${report.shares_as_of || "未知"} · 披露 ${localZone(report.filing_available_at)} · ${report.currency}。`,"small"));
        if(report.ttm_period)result.append(node("p",`TTM ${report.ttm_period.start} — ${report.ttm_period.end}；计算时点 ${localZone(report.evaluated_at)}。`,"small muted"));
        result.append(node("p","原研究的 Thesis／Action 保持不变，尚未消费这份输入或计算。需结合经营事实、最强反证、改变判断条件与持仓适用性另行判断；单一倍数不触发操作。未发送给 Deep。","small"));
        if(task.assessment){
          const linked=node("details",null,"desk-price-action-link");linked.append(node("summary","回到这条研究的操作依据"));
          linked.append(node("p",`原方向 ${task.action_decision?.decision_action || "UNDETERMINED"} · ${task.action_decision?.decision_status || "未记录"}；本次价格计算没有重新评估该方向。`,"small"));
          if(task.assessment.action)linked.append(overview("原投资逻辑变化",task.assessment.action.thesis_delta));
          linked.append(overview("仍须面对的反证",task.assessment.counterevidence[0]),overview("能改变判断的新事实",task.assessment.action_conditions[0]));result.append(linked);
        }
        const ids=report.source_ids.filter(id=>task.sources.some(s=>s.evidence.evidence_id===id));
        if(ids.length){const sources=node("button","查看 SEC 股数及财务来源","desk-source-button");sources.type="button";sources.addEventListener("click",()=>openEvidence(ids,sources));result.append(sources);}
        const receipt=node("details");receipt.append(node("summary","本地价格与计算收据 · 完整数值、假设和校验"),node("p",`原研究第 ${report.revision} 轮未改变；收据未写入保存历史，未进入模型证据。`,"small muted"),node("pre",JSON.stringify(value,null,2)));result.append(receipt);
      }catch{if(entry.isConnected)result.replaceChildren(node("p","本次计算未完成。请检查十进制价格、日期和来源选择；原研究与记录未改变，也没有提交模型请求。","small desk-error"));}
      finally{submit.disabled=false;}
    });
    form.addEventListener("input",()=>{if(result.children.length && !submit.disabled)result.replaceChildren(node("p","输入已改变，请重新计算；原研究保持不变。","small"));});
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
    if(active?.task_id!==task.task_id || active?.revision!==task.revision){$("desk-evidence-dialog").close();sourceKey=null;readingReturn=null;observedSince=Date.now();$("desk-followup-model-profile").value="";}
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
    mobile.hidden=task.status==="COMPLETE";
    $("desk-task-details").textContent=`任务 ${task.task_id} · 截止 ${stamp(task.cutoff)} · 更新 ${stamp(task.updated_at)} · ${task.account_id ? "账户 "+task.account_id : "全部账户"} · 持仓 ${task.snapshot_id || "尚无快照"}`;
    const progressText=task.status==="COMPLETE" ? "研究完成，结果已保存。" : task.status==="FAILED" ? "本轮研究未完成；任务和来源已保留。" : task.status==="NEEDS_INPUT" ? "补充问题后可继续同一任务。" : task.progress.at(-1)||"研究问题已保存。";
    if($("desk-progress").textContent!==progressText)$("desk-progress").textContent=progressText;
    $("desk-progress").classList.toggle("sr-only",task.status!=="RUNNING");
    $("desk-result").setAttribute("aria-busy",String(task.status==="RUNNING"));
    renderWaiting(task);
    const result=$("desk-result");result.replaceChildren();let accountDetails=null;cited=new Set();
    $("desk-next-action").replaceChildren();$("desk-next-draft").hidden=true;
    $("desk-read-saved").textContent="重新读取保存状态 · 不调用模型";
    $("desk-action-hint").textContent=historical ? "历史版本只读。回到最新一轮后可继续研究。" : "沿用这次的账户与持仓快照；更换范围请开始新研究。";
    $("desk-retry-options").open=task.status==="FAILED";
    if(task.status!=="RUNNING" && task.status!=="COMPLETE")result.append(timeStrip(task));
    if(task.status==="FAILED") {result.append(node("h3","本轮没有新的研究判断"),node("p",errorMessage(task.error,task),"desk-error"));const hint=window.miaFailureHint?.(task);if(hint)result.append(hint);if(task.unresolved_question)result.append(node("p",task.unresolved_question));$("desk-next-action").append(node("h3","先恢复读取，再决定是否重试"),node("p",task.pending_next_action || "读取保存状态不会重新提交研究。确认原因后，再选择下方的重新研究。"));}
    if(task.status==="NEEDS_INPUT") result.append(node("p",task.unresolved_question,"desk-input-needed"));
    if(task.status==="COMPLETE" && task.assessment) {
      const dispositions={CONDITIONAL_ADD:"有条件考虑加仓",DEFER:"暂缓行动",REVIEW_REDUCE:"复核减仓条件",RESEARCH_ONLY:"继续研究",UNKNOWN:"证据不足，暂无法判断"};
      if(task.objective==="CONSIDER_ADD")dispositions.DEFER="暂缓加仓";
      if(task.objective==="CONSIDER_REDUCE")dispositions.DEFER="暂缓减仓";
      const action=task.action_decision, actionLabels={ADD:"ADD · 倾向加仓",HOLD:"HOLD · 倾向持有",REDUCE:"REDUCE · 倾向减持",EXIT:"EXIT · 倾向退出"};
      const actionHeading=action ? actionLabels[action.decision_action] || (action.decision_status==="NOT_APPLICABLE" ? "该操作不适用" : action.decision_status==="NO_ACTION_REQUESTED" ? "研究回答 · 未请求操作方向" : "方向未形成 · 证据不足") : "UNKNOWN · 未记录操作方向";
      const core=overview(task.objective==="EARNINGS_CHECK" ? "财报研究重点" : actionHeading || dispositions[task.assessment.disposition]||"核心判断",task.assessment.core_judgment,true);core.classList.add("desk-core");
      if(action)core.append(node("p",action.applicability_note || (action.decision_status==="SUPPORTED" ? "证据支持的研究方向" : action.decision_status==="PROVISIONAL" ? "暂定 · PROVISIONAL" : "尚无适用且有依据的操作方向"),"small desk-core-note"));
      if(action){core.querySelector(".desk-statement")?.remove();core.querySelector(".desk-source-button")?.remove();cited=new Set();}
      if(action){
        const statusLabels={SUPPORTED:"证据支持",PROVISIONAL:"暂定",UNDETERMINED:"方向未定",NOT_APPLICABLE:"不适用",NO_ACTION_REQUESTED:"未请求操作方向"};
        const evidenceLabels={STRONG:"较强",MIXED:"正反并存",LIMITED:"有限",INSUFFICIENT:"不足"};
        const status=node("p",`${action.decision_action || "UNDETERMINED"} · ${statusLabels[action.decision_status] || action.decision_status} · 证据${evidenceLabels[action.evidence_strength] || "状态未记录"}`,"small desk-action-status");
        status.dataset.direction=action.decision_action || "UNDETERMINED";
        status.dataset.status=action.decision_status;
        status.dataset.evidence=action.evidence_strength;
        core.append(status);
        if(task.assessment.action?.thesis_delta){const thesis=overview("投资逻辑变化 · Thesis Delta",task.assessment.action.thesis_delta,true);thesis.classList.add("desk-core-thesis");core.append(thesis);}
      }
      if(!action)core.append(node("p","这是基于所列资料的条件研究判断，尚待你确认。","small desk-core-note"));
      // Narrow screens: the next check sits under the conclusion instead of ~5
      // screens below; the side panel copy is hidden there by CSS, not duplicated.
      const nextCheck=task.assessment.next_checks[0];
      if(nextCheck){const jump=node("div",null,"desk-core-next");jump.append(node("h4","下一步优先核查"),node("p",firstSentence(nextCheck.text)));
        const go=node("a","拟定核查或继续研究");go.href="#desk-action-panel";jump.append(go);core.append(jump);}
      result.append(core,decisionBrief(task));
      // Scan aid: the first sentence carries the verdict; qualifiers stay in the same paragraph.
      result.querySelectorAll(".desk-overview .desk-statement").forEach(leadSentence);
      if(task.status==="COMPLETE")result.append(timeStrip(task));
      const gain=researchGain(task);
      if(task.assessment.investigation_effect){const effect=overview("深入研究补查带来的变化",task.assessment.investigation_effect,true);effect.classList.add("desk-gain-effect");effect.append(node("p",{CHANGED:"方向因新证据改变",UNCHANGED:"方向未变：新证据让风险更具体",UNRESOLVED:"关键问题仍未解决"}[task.assessment.judgment_change] || "方向变化未记录","small desk-core-note"));if(gain)gain.append(effect);else result.append(effect);}
      if(gain)result.append(gain);
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
      section.append(node("summary","账户、持仓与完整计算口径"),node("p",`账户：${p.account_names?.join("、") || p.account_id || "全部账户"} · 绑定持仓快照 · ${p.valuation_status === "complete" ? "全部持仓已定价" : "部分持仓缺少合格价格"}`,"small muted"));
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
    // Everything auditable stays one click away in a single reference list:
    // full report, boundaries, valuation, quarters, technical, account basis.
    const fold=(title,hint,content,cls="")=>{const box=node("details",null,("desk-fold "+cls).trim());const summary=node("summary");summary.append(node("span",title,"desk-fold-title"));if(hint)summary.append(node("span",hint,"desk-fold-hint"));box.append(summary);if(content)box.append(content);return box;};
    const reference=node("section",null,"desk-reference");reference.setAttribute("aria-label","依据、计算与完整报告");reference.append(node("h3","依据、计算与完整报告"));
    if(task.status==="COMPLETE" && task.assessment) {
      const premise=node("div",null,"desk-premise");
      premise.append(node("p",task.premise.explanation,"small muted"));
      const details=node("details",null,"desk-report-details desk-fold");details.append(node("summary","完整研究报告"),group("原始核心判断",[task.assessment.core_judgment]),premise.cloneNode(true),group("全部支持",task.assessment.support),group("全部反证",task.assessment.counterevidence),group("全部行动条件",task.assessment.action_conditions));
      if(task.assessment.action)details.append(group("投资逻辑变化",[task.assessment.action.thesis_delta]));
      details.append(group("全部理由",task.assessment.key_reasons),group("全部下一步核查",task.assessment.next_checks));
      reference.append(details);
      // Limitations are stated once, here, instead of repeated beside each block.
      const limits=[...new Set(task.limitations.map(text=>text.replace(/^MARKET_REGIME_NOT_VERIFIED：/,"")))];
      if(limits.length){const list=node("ul",null,"desk-boundary-list");limits.forEach(text=>list.append(node("li",text)));reference.append(fold("本轮边界",`${limits.length} 项`,list,"desk-boundary"));}
    }
    if(task.status==="COMPLETE"){
      const valuation=node("details",null,"desk-memo-section desk-valuation desk-fold");valuation.append(node("summary","估值、财务与价格情景"));valuation.setAttribute("aria-label","本轮估值与财务依据");
      valuation.append(node("h3","估值与财务依据"),node("p","正在读取本轮冻结来源；不会刷新价格。","small muted"));reference.append(valuation);
      void valuationSection(task,valuation);
      const financials=financialHistory(task);if(financials)reference.append(fold("最近已披露季度","确定性财务",financials));
      if(!task.action_decision && task.focus_symbol && window.miaOpenWhatIf && !publicReplay){const entry=node("section",null,"desk-memo-section desk-whatif-entry");entry.setAttribute("aria-label","调仓预演");const go=node("button",`调仓预演 · ${task.focus_symbol}`);go.type="button";go.addEventListener("click",()=>window.miaOpenWhatIf(task.focus_symbol));entry.append(node("h3","如果现在加减仓？"),node("p","用当前组合快照与最新报价预演一次加仓或减仓对现金、股数和权重的影响；不下单、不保存。","small muted"),go);result.append(entry);}
      if(task.action_decision){const technical=node("details",null,"desk-memo-section desk-technical desk-fold");technical.append(node("summary","技术条件 · 当前仅合成行情"));const whatif=node("section",null,"desk-memo-section desk-whatif");whatif.setAttribute("aria-label","单账户调仓预演");result.append(whatif);reference.append(technical);void whatifSection(task,whatif);technicalSection(task,technical,whatif);}
    }
    const metricBand=result.querySelector(":scope > .desk-key-metrics");if(metricBand && accountDetails)accountDetails.append(metricBand);
    if (task.status === "COMPLETE" && task.assessment) {
      // Reorder saved statements only. No model rewrite, inferred Action or invented before/after.
      const communication=node("section",null,"desk-decision-communication");communication.setAttribute("aria-label","结论依据与改变条件");
      const assessment=task.assessment;
      if(assessment.key_reasons?.[0])communication.append(overview("首要依据",assessment.key_reasons[0],true));
      if(assessment.counterevidence?.[0])communication.append(overview("关键反证",assessment.counterevidence[0],true));
      if(assessment.action?.thesis_delta)communication.append(overview("本轮保存的投资逻辑变化",assessment.action.thesis_delta,true));
      else communication.append(node("p","Thesis 对比 UNKNOWN：本记录未保存可比较的前后论点。","small muted"));
      for(const condition of (assessment.action_conditions || []).slice(0,2))communication.append(overview("什么证据会改变判断",condition,true));
      const core=result.querySelector(".desk-core");if(core)core.after(communication);
      const oldBrief=result.querySelector(":scope > .desk-brief");
      if(oldBrief){const tally=oldBrief.querySelector(".desk-source-tally");if(tally){tally.textContent=`本轮保存 ${task.sources.length} 条资料；完整依据和来源仍保留在下方报告中。`;communication.append(tally);}oldBrief.remove();}
    }
    if (!publicReplay && researchConfigured && !historicalView && task.status === "COMPLETE" && task.mode === "FAST") {
      const deep = profileForMode("DEEP");
      if (deep || !availableProfiles.length) {
        const offer = node("section", null, "desk-memo-section");
        const next = task.assessment?.next_checks?.[0]?.text || "核查当前判断最关键的证据缺口。";
        offer.append(node("h3", "下一步最有价值的核查"), node("p", next));
        const button = node("button", "继续为 Deep · 保留同一条记录"); button.type = "button";
        button.addEventListener("click", async () => {
          const shown = active;
          $("desk-followup").value = `请深入核查：${next}`.slice(0, 1200);
          if (deep) $("desk-followup-model-profile").value = deep.profile_id;
          if (await confirmFollowup("DEEP") && stillShowing(shown)) void send($("desk-followup").value, true, "DEEP");
        });
        offer.append(button, node("p", "只有确认后才创建新版本；当前结果与原始来源保留。", "small muted"));
        result.append(offer);
      }
    }
    const personal=result.querySelector(":scope > .desk-personal-context");if(personal && accountDetails)accountDetails.append(personal);
    if(accountDetails){accountDetails.classList.add("desk-fold");reference.append(accountDetails);}
    if(reference.childElementCount>1)result.append(reference);
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
  // A direct link or reload opens the record where the page starts: no scroll
  // jump past the identity banner and no focus ring. In-app navigation moves.
  async function openTask(taskId, revision="", initial=false) {
    clearAttentionDraft();
    const generation=++viewGeneration;
    $("desk-observation").hidden=true;
    try{
      const task=await readApi("/api/research/tasks/"+encodeURIComponent(taskId)+(revision ? "/revisions/"+encodeURIComponent(revision) : ""));
      if(generation!==viewGeneration)return;
      const target=new URL(location.href);target.searchParams.set("research_task",taskId);
      if(revision)target.searchParams.set("revision",revision);else target.searchParams.delete("revision");
      if(target.href!==location.href)history.pushState(null,"",target);
      render(task,!!revision);if(!initial){$("desk-task").focus({preventScroll:true});$("desk-task").scrollIntoView({block:"start",behavior:motion()});}
      void historyOptions(task,generation);
      if(!revision && task.status==="RUNNING")void watch(taskId,generation);
    }catch(error){if(generation===viewGeneration){if(active)observationLost("任务暂时无法读取，仍保留上次观察的版本。请重新读取进度。");else $("desk-request-status").textContent="任务暂时无法读取，请刷新最近研究；没有重新提交。";}}
  }
  async function send(question, followup, forceMode=null) {
    if(publicReplay)return;
    if(!researchConfigured){$("desk-request-status").textContent="研究模型尚未配置；可以读取披露和历史记录，本次没有创建研究。";return;}
    if(sending || historicalView || followup && active?.status==="RUNNING")return;
    const shown=active;
    sending=true;buttons(active);$("desk-submit").disabled=true;
    const generation=++viewGeneration;
    const request={question};
    if(followup && active){
      request.task_id=active.task_id;
      request.expected_revision=shown.revision;
      const profile=$("desk-followup-model-profile"),option=profile.selectedOptions[0];
      if(profile.value){request.model_profile_id=profile.value;request.mode=option.dataset.mode;}
      if(forceMode){request.mode=forceMode;const chosen=profileForMode(forceMode);if(chosen)request.model_profile_id=chosen.profile_id;}
    }
    else {
      request.mode=effectiveMode();
      const profile=profileForMode(request.mode);if(profile)request.model_profile_id=profile.profile_id;
      if(!$('desk-strategy-label').hidden)request.execution_strategy=$('desk-adaptive').checked ? 'ADAPTIVE' : 'BASELINE';
      if(state.snapshot)request.snapshot_id=state.snapshot.snapshot_id;
      if($("desk-account").value)request.account_id=$("desk-account").value;
      if(attentionDraft)request.attention_item_id=attentionDraft.attention_item_id;
      if(filingDraft)request.filing_evidence_id=filingDraft.filing_evidence_id;
    }
    try{
      // Hub runtime: owned data leaves only with a server consent receipt for this exact request.
      const consent=await window.miaEgress?.confirm(request);
      if(consent===null){$("desk-request-status").textContent="已取消，没有发送任何内容。";return;}
      if(generation!==viewGeneration || historicalView || followup && !stillShowing(shown))return;
      if(consent)request.egress_consent_id=consent;
      $("desk-request-status").textContent="正在保存研究任务…";
      const task=await api("/api/research/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(request)});
      window.miaEgress?.remember(task.task_id,consent);
      if(generation!==viewGeneration)return;
      const target=new URL(location.href);target.searchParams.set("research_task",task.task_id);target.searchParams.delete("revision");
      if(target.href!==location.href)history.pushState(null,"",target);
      clearAttentionDraft();render(task);$("desk-task").focus({preventScroll:true});$("desk-task").scrollIntoView({block:"start",behavior:motion()});$("desk-followup").value="";
      void historyOptions(task,generation);void recent();
      $("desk-request-status").textContent="研究任务已创建。";
      void watch(task.task_id,generation);
    }catch(error){if(generation===viewGeneration){$("desk-request-status").textContent=errorMessage(error)+" 如请求结果不明，请先刷新最近研究。";await recent();}}
    finally{sending=false;$("desk-submit").disabled=!researchConfigured;buttons(active);}
  }
  window.addEventListener("mia-ask-draft",event=>{
    if(publicReplay)return;
    leaveTask("#ask-mia");
    clearAttentionDraft();
    $("desk-question").value=event.detail.question.slice(0,1200);
    if(event.detail.attention_item_id){attentionDraft={attention_item_id:event.detail.attention_item_id,symbol:event.detail.symbol};$("desk-attention-context").hidden=false;$("desk-attention-context").querySelector("span").textContent=`已关联 ${attentionDraft.symbol} 的关注事项；提交后会核查该事项的冻结来源。`;}
    if(event.detail.filing_evidence_id){filingDraft={filing_evidence_id:event.detail.filing_evidence_id,symbol:event.detail.symbol};$("desk-attention-context").hidden=false;$("desk-attention-context").querySelector("span").textContent=`已关联 ${filingDraft.symbol} 的 SEC 申报元数据；提交时由服务器核对来源身份和时间。`;}
    selectResearchMode(selectedResearchMode);
    $("ask-mia").scrollIntoView({block:"start",behavior:motion()});$("desk-question").focus({preventScroll:true});
    $("desk-request-status").textContent=researchConfigured ? "已拟定研究问题，可修改后提交；尚未开始研究。" : `已拟定研究问题；${$("desk-mode-help").textContent}`;
  });
  $("desk-clear-attention").addEventListener("click",clearAttentionDraft);
  // Every research POST waits for the in-app cost confirmation; cancel sends nothing.
  const scopeLabel=()=>$("desk-account").selectedOptions[0]?.textContent || "全部账户";
  const outbound=()=>window.miaOutboundDisclosure?.()||{facts:[],consent:null};
  const confirmNew=()=>miaConfirmCost({title:"开始这项研究？",body:"创建研究任务，读取持仓、核查资料并由模型形成判断。",confirmLabel:"确认开始研究",
    facts:[["持仓范围",scopeLabel()],["研究深度",`${selectedResearchMode} → ${effectiveMode()}${profileForMode(effectiveMode()) ? ' · '+profileForMode(effectiveMode()).model : ''}`],...(attentionDraft?[["关联事项",`${attentionDraft.symbol} 的关注事项`]]:[]),...(filingDraft?[["关联 SEC 披露",filingDraft.symbol]]:[]),
      ...(!$('desk-strategy-label').hidden?[["研究策略",$('desk-adaptive').checked?"自适应补查（实验）":"标准"]]:[]),...outbound().facts],consent:outbound().consent});
  const confirmFollowup=(mode=null)=>miaConfirmCost({title:`继续研究 · 第 ${(active?.revision||0)+1} 轮`,body:`${$("desk-followup-model-profile").selectedOptions[0]?.textContent || "沿用本轮研究方式"}。创建新版本并重新核查资料；之前的版本保留。`,confirmLabel:"确认继续研究",
    facts:[["研究深度",mode || $("desk-followup-model-profile").selectedOptions[0]?.dataset.mode || active?.mode || "DEEP"],["持仓范围",active?.account_id ? "所选账户 · 原持仓快照" : "全部账户 · 原持仓快照"],...outbound().facts],consent:outbound().consent});
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
    const hash=location.hash,legacyRecord=new URLSearchParams(location.search).has("cockpit"),view=legacyRecord ? "workspace" : ({"#universe":"universe","#portfolio":"portfolio","#inspector":"inspector","#research-history":"history"}[hash]||"home");
    document.body.dataset.consoleView=view;renderRecent();
    const labels={home:["你的研究工作台","先看与你相关的变化，再研究一个重要问题。"],history:["你的研究记录","重新打开结果、核对来源，或从最新一轮继续。"],portfolio:["我的组合","管理持仓快照，查看已保存的成本、现金与集中度。"],universe:["研究范围","选择持续跟踪的公司；持仓会自动纳入。"],workspace:["旧版研究记录","读取已保存的判断；新研究从工作台的研究提问发起。"],inspector:["依据与执行面板","查看现有工具结果、证据和可观察记录。"]};
    const heading=document.querySelector(".page-heading");heading.querySelector("h1").textContent=labels[view][0];heading.querySelector(".muted").textContent=labels[view][1];$("manage-portfolio").hidden=publicReplay || !["home","portfolio"].includes(view);
    $("universe").open=view==="universe";
    $("portfolio-disclosure").open=view==="portfolio";
    $("desk-legacy").open=["workspace","inspector"].includes(view);
    if(hash==="#workspace" && !legacyRecord){
      const target=new URL(location.href);target.hash="#ask-mia";history.replaceState(null,"",target);
      $("desk-request-status").textContent="新研究使用统一入口；旧版记录可通过原来的链接读取。";
      const focusQuestion=()=>setTimeout(()=>{$("ask-mia").scrollIntoView({block:"start"});$("desk-question").focus({preventScroll:true});},0);
      if(document.readyState==="complete")focusQuestion();else window.addEventListener("load",focusQuestion,{once:true});
    }
    document.querySelectorAll(".rail nav a").forEach(link=>{const selected=view==="home"?link.hash==="#attention":link.hash===hash;link.classList.toggle("active",selected);if(selected)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  }
  function leaveTask(destination=null) {clearAttentionDraft();++viewGeneration;active=null;historicalView=false;renderKey="";$("desk-evidence-dialog").close();$("desk-task").hidden=true;delete document.body.dataset.deskView;const url=new URL(location.href);url.searchParams.delete("research_task");url.searchParams.delete("revision");if(destination!==null){url.searchParams.delete("cockpit");url.hash=destination;}if(destination!==null&&url.href!==location.href)history.pushState(null,"",url);else history.replaceState(null,"",url);applyNavigation();void recent();}
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
    availableProfiles=(data.profiles || []).filter(p=>p.available);profileDefaults=data.default_profile_ids || {};
    const select=$('desk-model-profile');select.replaceChildren();
    const followup=$('desk-followup-model-profile');
    for(const profile of data.profiles || [])if(profile.available){
      const option=node('option',`${profile.mode==='FAST' ? '快速判断' : '深入研究'} · ${profile.model}`);
      option.value=profile.profile_id;option.dataset.mode=profile.mode;option.dataset.provider=profile.provider;
      followup.append(option.cloneNode(true));option.selected=profile.mode==='DEEP';select.append(option);
    }
    $('desk-profile-label').hidden=true;
    const providers=[...new Set(availableProfiles.map(p=>p.provider))].filter(provider=>['FAST','DEEP'].every(mode=>availableProfiles.some(p=>p.provider===provider && p.mode===mode)));
    const providerSelect=$('desk-provider');providerSelect.replaceChildren();
    for(const provider of providers){const option=node('option',provider);option.value=provider;providerSelect.append(option);}
    $('desk-provider-disclosure').hidden=providers.length<2;
    $('desk-followup-profile-label').hidden=!select.options.length;
    selectResearchMode(selectedResearchMode);
    const availability=data.mode_availability || {FAST:availableProfiles.some(p=>p.mode==='FAST'),DEEP:availableProfiles.some(p=>p.mode==='DEEP')};
    researchConfigured=Boolean(availability.FAST || availability.DEEP);
    configurationReason='模型尚未配置：研究已禁用。仍可读取公开披露、Thesis 缺口与历史记录。';
    selectResearchMode(selectedResearchMode);
    document.querySelectorAll('[data-research-depth]').forEach(button=>{const mode=button.dataset.researchDepth==='DEEP'?'DEEP':'FAST';button.disabled=!availability[mode];if(button.disabled)button.title='当前模型配置没有这个可用档位';});
    if(!availability.FAST && availability.DEEP)selectResearchMode('DEEP');
    $('desk-submit').disabled=!researchConfigured;
    if(!researchConfigured)$('desk-mode-help').textContent='模型尚未配置：研究已禁用。仍可读取公开披露、Thesis 缺口与历史记录。';
    buttons(active);
    // Server policy decides whether ADAPTIVE may be chosen; follow-ups inherit.
    const strategy=data.execution_strategy;
    $('desk-strategy-label').hidden=!strategy?.adaptive_allowed;
    $('desk-adaptive').checked=strategy?.default==='ADAPTIVE';
  }).catch(()=>{$('desk-profile-label').hidden=true;$('desk-strategy-label').hidden=true;$('desk-submit').disabled=true;configurationReason='无法读取模型配置；请刷新后重试。本次没有创建研究。';selectResearchMode(selectedResearchMode);});
  applyNavigation();
  if(publicReplay){
    document.querySelectorAll('[data-research-depth]').forEach(button=>{button.disabled=true;});
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
  if(params.has("research_task"))void openTask(params.get("research_task"),params.get("revision")||"",true);
})();
