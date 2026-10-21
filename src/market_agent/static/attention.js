"use strict";
(() => {
  // Public replay reads the saved synthetic scan through the same queue API;
  // every scan/deep/review control is replaced by read-only links to saved tasks.
  const publicDemo=document.body.dataset.runtimeMode==="PUBLIC_DEMO";
  let demoExamples=[];
  const lifecycle = {HOLDING:"持仓",WATCHING:"持续跟踪",CANDIDATE:"待评估",ARCHIVED:"暂停研究"};
  const priorities = {HIGH:"高优先级",MEDIUM:"值得复核",VERIFY:"待核实"};
  let busy = false;
  let universeEntries = [];
  let scanScope=null, pendingEvidence=0, screenLimit=null, currentPending=0;
  let activeScan = sessionStorage.getItem("mia-attention-scan");
  const jsonOptions = value => ({method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
  const displayZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const stamp = value => {
    const date=new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleString("zh-CN",{
      timeZone:displayZone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})+" · "+displayZone : "时间未知";
  };
  const publication = e => {
    if(!e.published_at) return "发布时间未知";
    const precision=e.structured_data?.published_at_precision ||
      (["finnhub","synthetic","synthetic-copy","synthetic-research"].includes(e.provider)?"INSTANT":"UNKNOWN");
    if(precision==="DATE") return e.published_at.slice(0,10)+" · 仅来源日期，时区未披露";
    return precision==="INSTANT" ? stamp(e.published_at) : "发布时间精度未披露";
  };
  // Publication identity comes from the backend market_identity projection.
  // TODAY means a New York publication date, never a verified event time.
  function freshnessChip(freshness, evidence) {
    const status=freshness?.freshness_status || "UNKNOWN";
    const precision=freshness?.publication_precision;
    let text={TODAY:"今日发布",RECENT:"早前发布 · 待复核",HISTORICAL:"历史报道 · 待复核",INELIGIBLE:"时间身份不合格"}[status];
    if(!text) text=precision==="DATE" && evidence?.published_at ? "仅来源日期 "+evidence.published_at.slice(0,10) : "发布时间未知";
    const chip=el("span",text,"attention-fresh");
    chip.dataset.freshness=status==="UNKNOWN" && precision==="DATE" ? "DATE" : status;
    chip.title="报道发布时间，不等于事件发生时间或交易日。";
    return chip;
  }
  const session = freshness => {
    const value=freshness?.trade_session;
    return value && value!=="UNKNOWN" ? `交易时段 ${value}` : "交易时段未提供";
  };
  const status = text => {$("attention-status").textContent=text;};
  const errorText = error => {
    const code = error.detail?.code || "";
    if (code === "UNIVERSE_HOLDING_IS_PORTFOLIO_DERIVED") return "这是已有持仓，请在组合中管理持仓。";
    if (code === "ATTENTION_UNIVERSE_LIMIT") return "当前服务仍按旧版本限制研究范围总数。请重启更新后的本地 ConvictionDesk，再刷新今日关注；无需归档持仓。";
    if (code.includes("ALREADY_RUNNING")) return "已有任务正在运行，请等待当前任务完成。";
    if (code.includes("HISTORY_INVALID")) return "已保存记录未通过完整性检查，读取已停止。";
    return "暂时无法完成请求。已保存的标的与证据仍保留，可稍后重试。";
  };
  function disclosure(title,child) {
    const d=el("details",null,"attention-disclosure");d.append(el("summary",title),child);return d;
  }
  // One shared detail drawer keeps the Today column short: sources, older items and
  // overflow items open here instead of expanding inline.
  // detailSource remembers which item the drawer shows, so a queue refresh can
  // redraw it from the new server row or close it only when the item is gone.
  let detailReturn=null, detailSource=null;
  function closeDetail(restore=true) {
    const dialog=$("attention-detail");
    if(!restore)detailReturn=null;
    if(dialog.open)dialog.close();
  }
  function showDetail(row) {
    const [title,content]=detailSource.view(row);
    $("attention-detail-title").textContent=title;
    $("attention-detail-body").replaceChildren(content);
  }
  function openDetail(row,view) {
    const dialog=$("attention-detail");
    detailReturn=document.activeElement;detailSource={itemId:row.item.item_id,view};
    showDetail(row);
    if(!dialog.open)dialog.showModal();
    $("attention-detail-close").focus();
  }
  function syncDetail(rows) {
    const dialog=$("attention-detail");
    const row=dialog.open && detailSource ? rows.find(r=>r.item.item_id===detailSource.itemId) : null;
    if(!row){closeDetail(false);return;}
    const hadFocus=dialog.contains(document.activeElement);
    showDetail(row);
    if(hadFocus && !dialog.contains(document.activeElement))$("attention-detail-close").focus();
  }
  $("attention-detail-close").addEventListener("click",()=>closeDetail());
  $("attention-detail").addEventListener("click",event=>{if(event.target!==event.currentTarget)return;const box=event.currentTarget.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)closeDetail();});
  $("attention-detail").addEventListener("close",()=>{
    const target=detailReturn?.isConnected ? detailReturn : null;detailReturn=null;detailSource=null;
    if(target)target.focus({preventScroll:true});
  });
  function deepResult(container,deep) {
    container.replaceChildren();
    if (!deep) return;
    if (["FAILED","INTERRUPTED"].includes(deep.status)) {
      const code=deep.result?.error || (deep.status === "INTERRUPTED" ? "RESEARCH_INTERRUPTED" : "UNKNOWN");
      const reasons={timeout:"模型响应超时",transport:"服务连接失败",authentication:"模型认证失败",rate_limit:"服务请求受限",configuration:"模型尚未配置",schema_validation:"模型结果不符合数据契约",unsupported_numeric_fact:"结果含无法核实的数字",invalid_evidence_reference:"证据引用未通过校验",unsupported_thesis_claim:"模型误用了未确认的投资逻辑",session_call_limit:"本轮模型调用已达到上限",RESEARCH_INTERRUPTED:"服务中断了调查"};
      container.append(el("p",`深入研究未完成：${reasons[code] || "结果未通过验证"}（${code}）。原始关注事项与证据已保留。${code === "session_call_limit" ? "停止本轮付费重试，检查调用记录后再安排研究。" : "排除原因后可选择「重新研究」；查看状态不会重新提交。"}`,"small attention-deep-failed"));
      return;
    }
    if(deep.input_changed) {
      container.append(el("p",deep.status === "RUNNING" ?
        "该事件已增加证据，正在运行的深入研究仅覆盖原冻结材料。完成后可主动重新研究新增证据。" :
        "该事件已增加证据，原深入研究未覆盖全部材料。点击深入研究可核查新的冻结证据集。","small"));return;
    }
    if(deep.applicability && deep.applicability.status !== "CURRENT") {
      container.append(el("p","这是历史研究，尚未确认适用于当前关注事项。原始结果仍保留。","small"));return;
    }
    if (deep.status === "RUNNING") {container.append(el("p","深入研究进行中 · 离开页面不会中断，回来后可只读查看进度。","small attention-deep-running"));return;}
    const output=deep.result?.output;
    if (!output) return;
    const body=el("div",null,"attention-deep");
    body.append(el("strong","深入研究"),el("p",output.what_changed.text),el("p",output.why_it_matters.text),el("p","下一步："+output.next_check.text));
    body.append(el("p",`${deep.result.model || "模型未记录"} · 引用 ${(output.what_changed.evidence_ids || []).join("、")} · 已保存的冻结证据，未重新取回行情。`,"small muted"));
    body.append(el("p",output.thesis_impact==="UNKNOWN"?"投资逻辑影响尚未确认。":
      ({SUPPORTED:"支持扫描时的已确认投资逻辑。",WEAKENED:"削弱扫描时的投资逻辑，应复核。",POTENTIALLY_INVALIDATING:"可能涉及投资逻辑失效，需优先核查。",UNCHANGED:"尚未识别投资逻辑的实质改变。"}[output.thesis_impact]),"small muted"));
    if (output.next_step==="DECISION_REFRESH") body.append(el("p","这条变化值得刷新持仓判断。请在下方主动提问中进行新的当前决策。"));
    container.append(body);
  }
  async function poll(path,deadline=240000,onUpdate=null) {
    const end=Date.now()+deadline;
    while (Date.now()<end) {
      const result=await api(path);
      if(onUpdate) onUpdate(result);
      if (result.status!=="RUNNING") return result;
      await new Promise(resolve=>setTimeout(resolve,400));
    }
    throw new Error("Observation deadline");
  }
  function renderItem(row, mode="card") {
    const inDrawer=mode==="drawer";
    const item=row.item, current=row.current_universe;
    const event=row.research_event, brief=item.screen.brief;
    const receipts=event?.evidence || item.packet.evidence;
    const article=el("article",null,"attention-item");article.dataset.itemId=item.item_id;
    const meta=el("div",null,"attention-meta");
    meta.append(el("strong",item.symbol,"attention-symbol"),freshnessChip(row.freshness,item.packet.evidence[0]),
      el("span",priorities[item.priority],"attention-priority"),el("span",lifecycle[current.state]+" · 待复核","small muted"));
    article.append(meta,el("h3",brief?.headline.text || item.screen.rationale.text));
    if(brief) article.append(el("p",item.screen.rationale.text));
    else article.append(el("p",item.what_changed.text,"small attention-original"),
      el("p","旧筛选记录 · 尚无中文事件提要和具体核查问题","small muted"));
    if(brief) article.append(el("p","接下来核查："+brief.next_check.text,"attention-next"));
    if(item.packet.revision_of) article.append(el("p","事实修订 / 新版本：原报道仍在证据中，请对照变化。","small"));
    const direction={POSITIVE:"证据倾向正面",NEGATIVE:"证据倾向负面",MIXED:"支持与反对并存",NEUTRAL:"方向中性",UNKNOWN:"方向尚不明确"};
    const timing=item.screen.urgency==="NOW"?"优先核查":item.screen.urgency==="SOON"?"近期核查":"核查时机待判断";
    article.append(el("p",`${direction[item.screen.direction]} · ${timing} · 报道发布 ${publication(item.packet.evidence[0])} · ${session(row.freshness)}`,"small muted attention-time"));
    if(event?.grouped) article.append(el("p",`同一研究事件 · ${receipts.length} 条原始证据；合并依据可展开查看。`,"small"));
    if(item.screening_lineage?.candidate_repost_unit)article.append(el("p",`转载候选共同筛选 · ${item.screening_lineage.candidate_repost_unit.member_event_keys.length} 篇原文全部保留。相似性不是同事件或事实确认，新披露仍须单独核查。`,"small"));
    const actions=el("div",null,"attention-actions");
    // Two separate controls: resume only reads the saved deep job (GET);
    // start/restart is the sole POST and is always an explicit, confirmed action.
    const resumeButton=el("button","查看研究进度","primary");resumeButton.type="button";
    const deepButton=el("button","深入研究");deepButton.type="button";
    const askButton=el("button","提问核查此事");askButton.type="button";
    askButton.addEventListener("click",()=>{closeDetail(false);window.dispatchEvent(new CustomEvent("mia-ask-draft",{
      detail:{question:`${item.symbol}：「${brief?.headline.text || item.screen.rationale.text}」会改变我的研究判断吗？`,
        attention_item_id:item.item_id, symbol:item.symbol}}));});
    const reviewed=el("button","已复核，移出关注");reviewed.type="button";
    reviewed.title="移出卡片所列记录，保留历史证据；新材料仍会进入筛选。";
    const evidenceBody=()=>{
    const detailBody=el("div");
    for (const e of receipts) {
      const source=el("div",null,"attention-source");
      let uri;
      try {uri=new URL(e.source_uri);} catch {uri=null;}
      if (uri && ["https:","http:"].includes(uri.protocol)) {
        const link=el("a",e.title || "原始来源");link.href=uri.href;link.target="_blank";link.rel="noopener noreferrer";source.append(link);
      } else source.append(el("strong",e.title || "原始来源"));
      if (e.excerpt) source.append(el("p",e.excerpt));
      source.append(el("small",`${e.provider} · 发布 ${publication(e)} · 获取 ${stamp(e.retrieved_at)}`));
      source.append(el("small",`证据 ${e.evidence_id}`));
      detailBody.append(source);
    }
    detailBody.append(el("p",item.packet.confirmed_thesis?"采用扫描时已确认的投资逻辑版本。":"没有已确认的投资逻辑，本次 Thesis 影响保持未知。","small muted"));
    detailBody.append(el("p",`代表记录冻结截止：${stamp(item.packet.cutoff)}。报道发布时间不等于事件发生时间。排序使用 Python 规则；持仓相关性采用成本与现金口径。`,"small muted"));
    if(event?.grouped) {
      detailBody.append(el("p","事件合并来自有证据引用的筛选判断，仍可能有语义误判；深入研究将在点击时冻结所列证据。","small muted"));
      for(const group of event.grouping_records || []) detailBody.append(el("p","合并依据："+group.basis.text,"small"));
    }
    detailBody.append(disclosure("模型与排序记录",el("pre",JSON.stringify({screen:item.screen,lineage:item.screening_lineage,
      frozen_universe:item.universe,current_universe:current,ranking_version:item.ranking_version,score:item.score,
      research_event:event},null,2))));
    return detailBody;
    };
    if(mode==="evidence")return evidenceBody();
    let evidenceDetails;
    if(inDrawer){evidenceDetails=disclosure("查看证据",evidenceBody());evidenceDetails.open=true;}
    else {
      evidenceDetails=el("button","查看证据","attention-evidence-open");evidenceDetails.type="button";
      evidenceDetails.title=`${receipts.length} 条原始来源、发布与获取时间、排序记录`;
      evidenceDetails.addEventListener("click",()=>openDetail(row,r=>[`${r.item.symbol} · 证据与记录`,renderItem(r,"evidence")]));
    }
    const deepBody=el("div",null,"attention-deep-host");deepBody.setAttribute("aria-live","polite");deepResult(deepBody,row.deep);
    let deepId=row.deep?.deep_id || null, deepState=row.deep || null, observing=false;
    const restartable=deep=>!deep || ["FAILED","INTERRUPTED"].includes(deep.status) || deep.input_changed ||
      (deep.status!=="RUNNING" && deep.applicability && deep.applicability.status!=="CURRENT");
    function controls() {
      resumeButton.hidden=!deepId;
      resumeButton.disabled=observing;
      resumeButton.textContent=observing ? "正在读取进度…" : deepState?.status==="RUNNING" ? "查看研究进度" : "重新读取研究结果";
      deepButton.hidden=!restartable(deepState) || deepState?.status==="RUNNING";
      deepButton.disabled=observing;
      deepButton.textContent=deepId ? "重新研究" : "深入研究";
      deepButton.classList.toggle("primary",!deepId);
      resumeButton.title="只读取已保存的研究任务，不会重新提交或调用模型。";
      deepButton.title=deepId ? "创建新的深入研究，可能产生模型费用。" : "冻结所列证据并开始深入研究，可能产生模型费用。";
    }
    async function observe(id) {
      observing=true;controls();
      try {
        const result=await poll(`/api/attention/deep/${encodeURIComponent(id)}`,240000,
          update=>{deepState=update;deepResult(deepBody,update);});
        deepState=result;deepResult(deepBody,result);
      } catch(error) {
        // Never conclude FAILED from a lost observation; keep the same deep ID.
        deepBody.replaceChildren(el("p",error.message==="Observation deadline" ?
          "本次查看已结束，研究仍可能在进行。点击「查看研究进度」只读刷新，不会重新提交。" :
          "暂时无法读取进度，任务状态待确认。已保留同一研究任务，稍后可只读刷新。","status-line attention-deep-unknown"));
      } finally {observing=false;controls();}
    }
    if(publicDemo){
      for(const example of demoExamples.filter(e=>e.attention_item_id===item.item_id)){
        const open=el("button",example.mode==="FAST"?"打开快速判断 · Fast":"打开深入研究 · Deep",example.mode==="FAST"?"primary":"");open.type="button";
        open.title="只读打开已保存的合成研究结果，不调用模型。";
        open.addEventListener("click",()=>{closeDetail(false);window.dispatchEvent(new CustomEvent("mia-open-task",{detail:example}));});
        actions.append(open);
      }
      actions.append(evidenceDetails);article.append(actions);return article;
    }
    if(inDrawer){actions.append(resumeButton,deepButton,askButton,reviewed);article.append(actions,deepBody,evidenceDetails);}
    else {actions.append(resumeButton,deepButton,askButton,evidenceDetails,reviewed);article.append(actions,deepBody);}
    controls();
    resumeButton.addEventListener("click",()=>{if(deepId && !observing)void observe(deepId);});
    deepButton.addEventListener("click",async()=>{
      if(observing)return;
      const confirmed=await miaConfirmCost(deepId ?
        {title:`重新研究 ${item.symbol}`,body:"冻结当前证据并创建新的深入研究；原结果保留。",confirmLabel:"确认重新研究"} :
        {title:`深入研究 ${item.symbol}`,body:`冻结所列 ${receipts.length} 条证据，由模型核查这项变化。`,confirmLabel:"确认开始"});
      if(!confirmed || observing)return;
      observing=true;controls();deepResult(deepBody,{status:"RUNNING"});
      try {
        const job=await api(`/api/attention/items/${encodeURIComponent(item.item_id)}/deep`,{method:"POST"});
        deepId=job.deep_id;deepState={status:job.status};
      } catch(error) {observing=false;controls();deepBody.replaceChildren(el("p",errorText(error),"status-line"));return;}
      observing=false;void observe(deepId);
    });
    reviewed.addEventListener("click",async()=>{
      reviewed.disabled=true;
      try {await api(`/api/attention/items/${encodeURIComponent(item.item_id)}/reviewed`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({member_item_ids:event?.member_item_ids || [item.item_id]})});await refresh();}
      catch(error) {status(errorText(error));reviewed.disabled=false;}
    });
    return article;
  }
  function compactRow(row) {
    const item=row.item, brief=item.screen.brief;
    const button=el("button",null,"attention-row");button.type="button";button.dataset.itemId=item.item_id;
    const head=el("span",null,"attention-row-meta");
    head.append(el("strong",item.symbol,"attention-symbol"),freshnessChip(row.freshness,item.packet.evidence[0]));
    if(row.deep?.status==="RUNNING")head.append(el("span","深入研究进行中","small muted"));
    button.append(head,el("span",brief?.headline.text || item.screen.rationale.text,"attention-row-title"));
    button.addEventListener("click",()=>openDetail(row,r=>[`${r.item.symbol} · 关注详情`,renderItem(r,"drawer")]));
    return button;
  }
  function renderQueue(data) {
    syncDetail([...data.items,...(data.historical_items||[])]);
    const queue=$("attention-queue");queue.replaceChildren();queue.setAttribute("aria-busy","false");
    const report=data.latest_scan?.report;
    const scope=data.scan_scope;
    scanScope=scope;pendingEvidence=data.pending_events || 0;screenLimit=data.screen_batch_limit;
    const lifecycle=data.pending_lifecycle;
    currentPending=lifecycle?.current || 0;
    const continueButton=$("attention-continue");
    continueButton.hidden=publicDemo || currentPending===0;
    continueButton.disabled=busy || !!activeScan;
    continueButton.textContent=`继续筛选近期材料（${currentPending}）`;
    const scopeLimited=report?.symbols_not_checked>0;
    const currentWindow=Number.isInteger(report?.pending_current_events);
    const savedOnly=report?.scan_mode==="SAVED_CURRENT_ONLY";
    const windowDone=currentWindow && (report.symbols>0 || savedOnly) && report.current_batch_result==="COMPLETED_NO_MATERIAL_EVENTS";
    const failed=data.latest_scan && ["FAILED","INTERRUPTED","RUNNING"].includes(data.latest_scan.status);
    const incomplete=data.pending_events>0 || report?.coverage_incomplete ||
      failed;
    const coverage=data.coverage;
    $("attention-coverage").textContent=coverage ?
      `已筛选 ${coverage.screened_records} 条新闻记录，形成 ${coverage.unresolved_events} 项未复核研究事件；此处显示其中前 ${coverage.shown_events} 项。`+
      (data.pending_events?`还有 ${data.pending_events} 条待筛，未筛选材料可能改变顺序。`:"")+
      "仅对已筛选集合排序，不是整个研究范围的全局 Top-5。" : "仅显示已筛选且待复核的研究事件，覆盖范围见扫描记录。";
    if(lifecycle)$("attention-coverage").textContent+=` 当前已保存近七天候选待筛 ${lifecycle.current} 条；曾合格但过期未筛 ${lifecycle.expired_unscreened} 条；其他早前及时间不合格材料 ${lifecycle.historical_other} 条保留。历史积压不自动占用本轮模型名额。`;
    else if(currentWindow)$("attention-coverage").textContent+=` 本次记录：近七天候选待筛 ${report.pending_current_events} 条，早前及不合格时间材料 ${report.pending_historical_events} 条保留；历史积压不自动占用本轮模型名额。`;
    const symbols=[...new Set(data.items.map(row=>row.item.symbol))];
    $("attention-summary").textContent=data.items.length ?
      `当前显示 ${data.items.length} 项 · ${symbols.join(" / ")}`+(incomplete||scopeLimited?" · 覆盖仍有缺口":" · 查看扫描覆盖") : "先建立研究范围，再检查相关变化。";
    if(lifecycle?.expired_unscreened)$("attention-summary").textContent+=` ${lifecycle.expired_unscreened} 条近期材料已过期未筛；不代表没有重要变化，也不会重新标为今日消息。`;
    if (data.items.length) {
      const extra=el("div",null,"attention-rows");
      data.items.forEach((row,index)=>index<3?queue.append(renderItem(row)):extra.append(compactRow(row)));
      if(data.items.length>3)queue.append(disclosure(`其余 ${data.items.length-3} 项关注`,extra));
    }
    else {
      const empty=el("div",null,"attention-empty");
      const scopeOnly=scopeLimited && !failed && !data.pending_events &&
        report.errors?.every(error=>error.code==="SYMBOL_BATCH_LIMIT");
      empty.append(el("h3",windowDone?"本批近期材料未发现新的重要事件。":scopeOnly?"本批没有新增需要优先处理的变化。":incomplete?"部分证据尚待核查。":scope?.active_symbols===0 || report?.symbols===0?"研究范围还没有标的。":report?"没有新增需要优先处理的变化。":"尚未运行关注扫描。"),
        el("p",windowDone?(savedOnly?"这只是已保存近期材料的筛选结果；本次没有获取新来源，不能说明市场没有新变化。":"这是本次已检索批次和已保存近期材料的结果；未检索标的与历史积压仍是覆盖缺口。"):scopeOnly?"这是本批检查结果，不代表全部研究范围都没有变化。":incomplete?"本次核查未完整结束。已获取的证据会保留；刷新优先检查近期变化，历史材料不会自动清空。":
          scope?.active_symbols===0 || report?.symbols===0?"建立持仓，或在研究范围中加入观察与候选标的。":report?"已复核的事项不会反复打扰你；没有新变化也是有效结果。":"点击「刷新今日关注」开始检查。持仓自动纳入研究范围，无需归档持仓。","muted"));
      queue.append(empty);
    }
    if(data.historical_items?.length){
      const older=el("div",null,"attention-rows");data.historical_items.forEach(row=>older.append(compactRow(row)));
      const count=data.freshness?.historical_events || data.historical_items.length;
      const details=disclosure(`早前与时间未核实的报道 · ${count} 项`,older);
      details.classList.add("attention-older");
      older.prepend(el("p","发布时间较早或未注明，不应当作今日新消息；复核时请对照原始来源。","small muted"));
      queue.append(details);
    }
    const screenError=report?.errors?.find(error=>error.stage==='screen' && !['PENDING_BACKLOG','PENDING_EVIDENCE_RETAINED'].includes(error.code));
    if(data.scan_result_status==='PROVIDER_UNAVAILABLE')status('本轮来源暂时不可用；未形成新的重要事件判断。已保存证据仍可查看。');
    else if(screenError)status(`已保存来源，但本轮筛选${screenError.code==='timeout' ? '超时' : '未通过验证'}；还有 ${data.pending_events || 0} 条待筛。可确认费用后继续筛选，当前排序不完整。`);
    else if(savedOnly)status(`本次筛选 ${report.screened} 个候选事件单元，覆盖 ${report.screened_article_receipts ?? report.screened} 篇已保存原文；近期仍待筛 ${currentPending} 条。未获取新来源，全范围仍为 PARTIAL。`);
    else if(data.scan_result_status==='COMPLETED_NO_MATERIAL_EVENTS')status('本轮扫描完成，未发现新的重要事件。');
    else if(windowDone)status(`本批近期候选已核查，未发现新的重要事件；全范围仍为 PARTIAL。保留 ${report.pending_historical_events} 条早前及时间不合格材料，不自动付费筛选。`);
    else if (scopeLimited && !failed && !data.pending_events && report.errors?.every(error=>error.code==="SYMBOL_BATCH_LIMIT")) {
      status(`本次已检查 ${report.symbols} / ${report.universe_symbols} 个标的。`+
        (report.batch_remaining?`本轮还有 ${report.batch_remaining} 个，点击「继续扫描剩余标的」。`:"本轮批次已结束；下次刷新从研究范围开头检查新变化。"));
    }
    else if (incomplete) status("部分完成 · PARTIAL：本次扫描未完整结束，关注列表可能不完整。已保存证据仍可查看；继续扫描需确认可能的费用。");
    else if (report) status("最近扫描尝试 · "+stamp(report.query_cutoff)+" · 报道发布时间分别标注");
    else status(scope?.active_symbols?`尚未运行 · ${scope.active_symbols} 个活跃标的，本次检查 ${scope.next_batch_symbols} 个。`:"尚未运行 · 先建立持仓或加入研究标的。");
    if(publicDemo)status(`合成回放 · 冻结扫描 ${stamp(report?.frozen_cutoff)} · 虚构公司与来源，不是实时新闻或投资建议。`);
    $("attention-status").classList.toggle("warning",!!incomplete);
    $("attention-scan").textContent=activeScan?"继续查看扫描":
      scope?.remaining_after_batch===0 && scope?.next_batch_symbols<scope?.active_symbols?"继续扫描剩余标的":"刷新今日关注";
    const audit=$("attention-scan-details");audit.replaceChildren();
    const successful=data.latest_successful_scan?.report;
    audit.append(el("p",successful ? "最近完整成功扫描 · "+stamp(successful.frozen_cutoff || successful.query_cutoff) : "尚无完整成功扫描记录。","small muted"));
    if(report) audit.append(el("p",`最近扫描尝试 · ${stamp(report.frozen_cutoff || report.query_cutoff)} · ${data.latest_scan.status}`,"small muted"));
    if(savedOnly)audit.append(el("p","本次仅使用已保存的近期来源；新闻 Provider 请求 0 次，没有推进来源抓取游标。","small"));
    if(report?.candidate_selection){
      const selection=report.candidate_selection;
      audit.append(el("p",`候选预选：持仓、新鲜线索、临期与非持仓分配名额，再按标的轮转。本轮 ${selection.current_candidates} 篇近期报道提供 ${selection.offered_candidates} 个语义筛选单元，覆盖 ${selection.offered_article_receipts ?? selection.offered_candidates} 篇原文，另 ${selection.deferred_current} 篇延后保留；上限 ${selection.event_limit} 个单元。关键词和来源只是待复核线索，不代表已验证重大事件。`,"small"));
      if(selection.clustering){const c=selection.clustering;audit.append(el("p",`保守转载候选聚类：${c.raw_articles} 篇原文 → ${c.candidate_event_units} 个候选单元；本轮候选中可避免 ${c.same_round_redundant_slots_avoided} 个重复筛选名额。全部来源、发布时间和 Evidence ID 保留；不同数字、主体、披露阶段或修订保持分开。相似性不证明同一事件。`,"small"));audit.append(el("p",`候选上下文上限 ${selection.candidate_context_byte_limit} 字节，完整模型上下文仍受原有上限约束；${selection.context_deferred_units.length} 个单元因字节容量本轮延后。更多原文可能增加输入 Token，不承诺单轮费用下降。`,"small muted"));}
      audit.append(el("p","持续流入超过筛选容量时仍可能过期漏筛；本轮缩减仅代表延后，未证明其余材料无价值。每次确认只运行一个有界批次，不自动追加调用。","small muted"));
    }
    if(lifecycle)for(const [symbol,row]of Object.entries(lifecycle.by_symbol)){
      if(row.expired_unscreened)audit.append(el("p",`${symbol}：${row.expired_unscreened} 条曾合格近期材料过期未筛，排序覆盖仍有缺口。`,"small"));
    }
    if(data.freshness?.as_of) audit.append(el("p","列表读取时点 · "+stamp(data.freshness.as_of)+"；读取列表不会刷新来源。","small muted"));
    audit.append(el("p","当前来源：公司新闻。扫描按需触发，采用七天迟到新闻重叠窗口；不覆盖全市场或全部披露。","small muted"));
    if(scope) audit.append(el("p",`研究范围 ${scope.active_symbols} 个；每次最多检查 ${scope.symbol_limit} 个，下一批 ${scope.next_batch_symbols} 个。批次位置已保存，重启后可继续；持仓无需归档。`,"small muted"));
    if(report?.symbols_not_checked) audit.append(el("p",`本次检索 ${report.symbols} / ${report.universe_symbols} 个标的，另有 ${report.symbols_not_checked} 个未在本次检索；本轮后续批次剩余 ${report.batch_remaining} 个。`,"small"));
    if(report?.symbol_batch?.length){
      audit.append(el("p","本次已检索："+report.symbol_batch.join(" / ")+"。检索完成不等于全部新闻已筛选。","small"));
      const unchecked=universeEntries.filter(e=>e.state!=="ARCHIVED"&&!report.symbol_batch.includes(e.symbol)).map(e=>e.symbol);
      if(unchecked.length)audit.append(el("p","当前范围中本次未检索："+unchecked.join(" / "),"small muted"));
    }
    const sourceGaps=(report?.errors || []).filter(e=>e.symbol);
    if(report?.symbol_coverage){
      const entries=el("ul"), labels={COMPLETE_WINDOW:"本次来源窗口完整",PARTIAL_SOURCE:"来源窗口不完整",PROVIDER_UNAVAILABLE:"本次来源不可用",NOT_FETCHED_IN_THIS_SCAN:"本次未检索"};
      Object.entries(report.symbol_coverage).forEach(([symbol,row])=>entries.append(el("li",`${symbol} · ${labels[row.status] || "状态未核实"}${row.until?` · 截止 ${stamp(row.until)}`:""}`,"small")));
      audit.append(disclosure("逐标的来源范围",entries));
    }
    if(sourceGaps.length)audit.append(el("p","来源覆盖缺口："+sourceGaps.map(e=>`${e.symbol} · ${e.code}`).join("；"),"small"));
    if(report?.model_events?.length)audit.append(el("p",`最近扫描记录了 ${report.model_events.length} 次模型阶段；实际费用以 Provider 账单为准，读取缓存不会重新调用模型。`,"small muted"));
    audit.append(el("p","手动刷新会检索公司新闻，并可能调用付费模型筛选已保存材料。新扫描先确认费用；查看已有扫描进度只读取保存状态。不会自动清空待筛队列或无限重试。","small muted"));
    if(data.inactive_pending_events) audit.append(el("p",`非活跃标的保留 ${data.inactive_pending_events} 条未筛证据；重新加入研究范围后可继续核查。`,"small muted"));
    if (report) audit.append(el("p",`本次检查 ${report.events_considered} 条，去重 ${report.duplicates} 条，初筛抑制 ${report.suppressed} 条；未筛完 ${report.pending_events} 条。`,"small"));
    audit.append(disclosure("完整执行记录",data.latest_scan?el("pre",JSON.stringify(data.latest_scan,null,2)):
      el("p","尚未运行：点击「刷新今日关注」后才会生成执行记录。","small")));
  }
  function renderUniverse(data) {
    universeEntries=data.entries;
    $("universe-count").textContent=`· ${data.entries.filter(e=>e.state!=="ARCHIVED").length} 个活跃标的`;
    const list=$("universe-list");list.replaceChildren();
    for (const entry of data.entries) {
      const row=el("div",null,"universe-row");row.dataset.symbol=entry.symbol;
      const head=el("div",null,"universe-identity");
      head.append(el("strong",entry.symbol,"universe-symbol"),el("span",lifecycle[entry.state],"universe-state"));
      const body=el("div",null,"universe-body");body.append(head);
      // Saved reason / note read back from the server, never from the form.
      const defaultReason=["Owner research","Authoritative portfolio holding"].includes(entry.reason);
      if(entry.state==="HOLDING" && defaultReason) body.append(el("p","由组合持仓自动纳入研究范围","small muted universe-reason"));
      else if(defaultReason || !entry.reason) body.append(el("p","尚未填写关注原因","small muted universe-reason is-empty"));
      else {
        const reason=el("p",null,"universe-reason");reason.append(el("span","关注原因","universe-label"),document.createTextNode(entry.reason));body.append(reason);
      }
      if(entry.owner_note){
        const note=el("p",null,"universe-note");note.append(el("span","个人备注","universe-label"),document.createTextNode(entry.owner_note));body.append(note);
      }
      body.append(el("small",`更新于 ${stamp(entry.updated_at)}`,"universe-updated"));
      row.append(body);
      if (entry.state==="HOLDING") row.append(el("span","由组合持仓决定","small muted"));
      else {
        const select=el("select");select.setAttribute("aria-label",entry.symbol+" 研究状态");
        for (const key of ["WATCHING","CANDIDATE","ARCHIVED"]) {const option=el("option",lifecycle[key]);option.value=key;select.append(option);}
        select.value=entry.state;
        select.addEventListener("change",async()=>{
          select.disabled=true;
          try {await api("/api/research-universe",jsonOptions({symbol:entry.symbol,state:select.value,reason:entry.reason,owner_note:entry.owner_note}));await refresh();}
          catch(error) {$("universe-status").textContent=errorText(error);select.value=entry.state;select.disabled=false;}
        });row.append(select);
      }list.append(row);
    }
  }
  async function refresh() {
    if(publicDemo && !demoExamples.length)demoExamples=(await api("/api/public-demo")).examples || [];
    const [queue,universe]=await Promise.all([api("/api/attention/queue"),api("/api/research-universe")]);
    renderUniverse(universe);renderQueue(queue);
  }
  async function runScan(savedOnly=false){
    if(busy)return;busy=true;$("attention-scan").disabled=true;$("attention-continue").disabled=true;
    try {
      if (!activeScan) {
        if(scanScope?.active_symbols===0){status("研究范围还没有标的；请先加入观察或候选标的。");return;}
        if(savedOnly && currentPending===0){status("当前没有已保存且仍在近期窗口内的待筛材料；历史积压保持不变。");return;}
        const confirmed=await miaConfirmCost({title:savedOnly?"继续筛选已保存材料？":"刷新今日关注？",body:savedOnly?"不获取新闻、不推进来源游标；只筛选已保存且仍在近七天窗口内的候选。可能调用付费模型，最多一个有界批次，不自动重试。过期材料保持原样，不当作今日消息。":"将检索公司新闻，并可能调用付费模型筛选近七天候选。历史积压保留，不自动填充模型名额。仅处理有界批次，不会自动重试。数据 Provider 也可能按你的套餐计费。",confirmLabel:savedOnly?"确认筛选":"确认扫描",facts:[["本次范围",savedOnly?`${currentPending} 条已保存近期候选；新闻请求 0 次`:scanScope ? `${scanScope.next_batch_symbols} / ${scanScope.active_symbols} 个标的` : "以服务端研究范围为准"],["全部保留待筛",`${pendingEvidence} 条；${Number.isInteger(screenLimit)?`每轮最多筛选 ${screenLimit} 个候选事件单元；全部原文保留，上下文有界`:"筛选上限以服务端配置为准"}`],["费用",savedOnly?"可能产生模型费用；本次不请求新闻 Provider。未提供精确模型报价。":"模型和数据费用以你配置的 Provider 为准，当前未提供精确报价"]]});
        if(!confirmed)return;
        status(savedOnly?"正在筛选已保存近期材料，不获取新来源…":"正在获取新公司新闻并核查重要变化…");
        const job=await api(savedOnly?"/api/attention/continue":"/api/attention/scan",{method:"POST"});
        activeScan=job.scan_id;sessionStorage.setItem("mia-attention-scan",activeScan);
      }
      else status("正在读取已保存的扫描进度 · 不重新提交扫描…");
      await poll(`/api/attention/scans/${encodeURIComponent(activeScan)}`);
      activeScan=null;sessionStorage.removeItem("mia-attention-scan");
      await refresh();
    } catch(error) {
      if(error.detail?.code==="ATTENTION_SCAN_NOT_FOUND") {
        activeScan=null;sessionStorage.removeItem("mia-attention-scan");
        $("attention-scan").textContent="刷新今日关注";
        status("上次扫描记录未找到。请点击「刷新今日关注」重新开始；没有自动重发扫描请求。");
      } else status(errorText(error));
    }
    finally {busy=false;$("attention-scan").disabled=false;$("attention-continue").disabled=!!activeScan || currentPending===0;
      if(activeScan) $("attention-scan").textContent="继续查看扫描";}
  }
  $("attention-scan").addEventListener("click",()=>runScan());
  $("attention-continue").addEventListener("click",()=>runScan(true));
  $("universe-form").addEventListener("submit",async event=>{
    event.preventDefault();const button=event.currentTarget.querySelector("button");button.disabled=true;
    try {
      const existing=universeEntries.find(entry=>entry.symbol===$("universe-symbol").value.trim().toUpperCase());
      const saved=await api("/api/research-universe",jsonOptions({symbol:$("universe-symbol").value,state:$("universe-state").value,
        reason:$("universe-note").value.trim() || existing?.reason || "Owner research",owner_note:$("universe-owner-note").value.trim() || existing?.owner_note || ""}));
      const reason=saved?.reason && saved.reason!=="Owner research" ? `关注原因：${saved.reason}` : "未填写关注原因";
      $("universe-status").textContent=`已保存 ${saved?.symbol || ""} · ${lifecycle[saved?.state] || ""} · ${reason}`;
      $("universe-form").reset();await refresh();
    }catch(error){$("universe-status").textContent=errorText(error);}
    finally{button.disabled=false;}
  });
  window.addEventListener("mia-portfolio-changed",()=>refresh().catch(error=>status(errorText(error))));
  refresh().catch(error=>{
    status(errorText(error));const queue=$("attention-queue");queue.setAttribute("aria-busy","false");
    if(!queue.children.length) queue.append(el("p","关注列表暂时无法加载，请稍后重试「刷新今日关注」。","attention-empty"));
  });
  if (activeScan) $("attention-scan").textContent="继续查看扫描";
})();
