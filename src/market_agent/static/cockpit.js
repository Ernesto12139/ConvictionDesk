"use strict";

let cockpitCorrectionQuery=null;
function clearCockpitCorrections() {
  $("cockpit-symbol").value="";$("cockpit-price").value="";cockpitCorrectionQuery=null;
}
$("query-input").addEventListener("input", clearCockpitCorrections);
for (const id of ["cockpit-symbol","cockpit-price"]) $(id).addEventListener("input",()=>{
  cockpitCorrectionQuery=$("query-input").value.trim();
});
document.querySelectorAll("[data-query]").forEach(button=>button.addEventListener("click",clearCockpitCorrections));

const cockpitActions = {ADD:"增持", HOLD:"继续持有，暂不加仓", REDUCE:"减持", EXIT:"退出持仓",
  RESEARCH_FIRST:"先核实关键证据，再决定行动", INSUFFICIENT_EVIDENCE:"先补齐证据，再决定行动",
  CLARIFY:"补充一项信息，即可继续", CAPABILITY_UNAVAILABLE:"新股票发现尚未提供"};
const cockpitObjectives = {TACTICAL_ENTRY:"短线机会", ADD_CAPITAL:"是否追加资金", HOLD_REVIEW:"是否继续持有",
  REDUCE_RISK:"是否减持", EVENT_IMPACT:"事件对持仓的影响", PORTFOLIO_RISK:"组合集中风险",
  GENERAL_DECISION:"持仓判断", UNKNOWN:"判断目标待明确"};
const cockpitStages = {queued:"研究已提交", framing:"已识别问题与价格来源", portfolio:"读取你的持仓与历史判断",
  context:"核对价格与组合背景", research:"核查公司新闻与披露", completion:"补查关键证据",
  verification:"查证你的前提与市场价格结构", frozen:"证据已冻结", fast_judge:"形成行动与反方判断",
  judge:"比较行动与反方理由", brief:"保存本次判断", complete:"判断已保存"};

function cockpitFramingLine(f) {
  let text = `${f.symbol || "组合"} · ${cockpitObjectives[f.objective]}`;
  if (f.decision_price !== null) text += ` · $${f.decision_price}（${f.price_source === "USER_INPUT" ? "你提供的价格" : "收盘参考"}）`;
  if (f.holding_status === "HELD") text += " · 已有持仓";
  return text;
}

function renderCockpitProgress(job) {
  const shell=el("div", null, "cockpit-progress");
  if (job.framing) shell.append(el("p", cockpitFramingLine(job.framing), "cockpit-framing"));
  const list=el("ol", null, "cockpit-stages");
  for (const stage of [...new Set([...(job.completed_stages || []), job.stage])]) {
    if (cockpitStages[stage]) list.append(el("li", cockpitStages[stage], stage===job.stage?"stage-active":"stage-done"));
  }
  shell.append(list);
  if (job.stage==="judge" || job.stage==="fast_judge") shell.append(el("p", "正在权衡业务、价格与组合风险，完成后会自动显示判断。", "small muted"));
  for (const check of job.investigation?.premise_verification || []) {
    if (check.status==="CONTRADICTED") shell.append(el("p", check.explanation, "cockpit-premise-correction"));
  }
  if (job.context) shell.append(el("p", `持有 ${job.context.quantity} 股 · 平均成本 ${moneyValue(job.context.average_cost_usd)}`, "small muted"));
  $("result").replaceChildren(shell);
}

function cockpitSection(title, statements) {
  const section=el("section", null, "cockpit-section");section.append(el("h4", title));
  for (const statement of statements.filter(Boolean)) section.append(el("p", statement.text));
  return section;
}

function cockpitDisclosure(title, content, className="cockpit-disclosure") {
  const details=el("details", null, className);details.append(el("summary", title), content);return details;
}

function renderCockpit(envelope, record, evidence, frozen=null) {
  state.cockpitResult=envelope; state.evidence=evidence; setRunId(envelope.run_id);
  $("evidence-count").textContent=String(evidence.length);$("evidence-filter").value="all";renderEvidence();
  const result=$("result");result.replaceChildren();
  const f=envelope.framing, brief=envelope.brief;
  $("query-input").value=f.query;
  $("cockpit-question-summary").textContent=`${f.query} · 点击换个问题`;
  $("cockpit-composer").open=false;
  $("answer-state").textContent=["CLARIFY","CAPABILITY_UNAVAILABLE"].includes(brief.primary_action)?"等待补充":"判断完成";
  result.append(el("p", cockpitFramingLine(f), "cockpit-framing"));
  const checks=frozen?.investigation?.premise_verification || [];
  for (const check of checks) if (check.status==="CONTRADICTED") result.append(el("p", check.explanation, "cockpit-premise-correction"));
  const card=el("article", null, "cockpit-main");
  const intensity={LIGHT:"小幅",MODERATE:"",STRONG:"明确",UNKNOWN:""};
  let action=cockpitActions[brief.primary_action];
  if (["ADD","REDUCE"].includes(brief.primary_action)) action=(intensity[brief.action_strength] || "")+action;
  card.append(el("h3", action, "cockpit-action decision-conclusion"));
  if (brief.one_line_decision) card.append(el("p", brief.one_line_decision.text, "cockpit-one-line"));
  if (brief.boundary) card.append(el("p", brief.boundary, "cockpit-binding"));
  if (f.assertions.length) card.append(el("p", "你提供的事件信息尚未核实；本次按其成立分析。", "cockpit-user-premise"));
  if (brief.top_reasons.length) card.append(cockpitSection("为什么", brief.top_reasons));
  const opposing=el("div", null, "cockpit-opposing");
  if (brief.main_counterargument) opposing.append(cockpitSection("最大的反方风险", [brief.main_counterargument]));
  if (brief.what_changes_my_mind.length) opposing.append(cockpitSection("什么会改变判断", brief.what_changes_my_mind));
  card.append(opposing);
  if (brief.next_best_check) card.append(cockpitSection("下一步最值得确认", [brief.next_best_check]));
  if (brief.conditional_branches?.length) card.append(cockpitSection("行动条件", brief.conditional_branches));
  result.append(card);
  if (envelope.version===2) {
    appendFastDetails(result,envelope,evidence,frozen);
    return;
  }
  if (!record) return;

  const refs=el("div", null, "cockpit-key-sources");
  refs.append(el("span", "关键依据", "small muted"), evidenceLinks(brief.decision_evidence_ids.slice(0,3)));
  result.append(refs);

  const packet=record.investment_context;
  const validWindows=packet.market_structure.windows.filter(w=>w.status==="AVAILABLE");
  if (validWindows.length) {
    const window=validWindows[validWindows.length-1];
    const relative=Number(window.decision_price_range_position_pct);
    if (window.decision_price_range_position_pct!==null) result.append(el("p",
      `${f.price_source === "USER_INPUT" ? "你提供的价格" : "收盘参考"} ${moneyValue(record.decision_price)}${relative<0?"低于":relative>100?"高于":"位于"}过去 ${window.sessions} 个完成交易日的收盘区间（${moneyValue(window.close_low)}—${moneyValue(window.close_high)}）。`, "cockpit-market-summary small muted"));
  }
  const details=el("div", null, "cockpit-details");
  // The incumbent detailed renderer remains the single source of deep inspection.
  // Mount its output into disclosure, then restore the concise first layer.
  const saved=[...result.childNodes];
  renderTdb(record,evidence);
  const deep=el("div", null, "cockpit-deep");deep.append(...result.childNodes);
  result.replaceChildren(...saved);
  details.append(cockpitDisclosure("查看完整行动权衡与研究详情", deep, "cockpit-disclosure cockpit-full-decision"));
  const provenance=el("div");
  provenance.append(el("p", `本次冻结 ${evidence.length} 项来源与计算输入。`), evidenceLinks(evidence.map(e=>e.evidence_id)));
  const calculated=evidence.find(e=>e.structured_data?.decision_context)?.structured_data.decision_context;
  if (calculated) provenance.append(renderDecisionContext(calculated));
  provenance.append(renderMarketStructure(packet), renderBindingConstraints(packet,record.conditional_assessment));
  details.append(cockpitDisclosure(`完整计算来源 · ${evidence.length} 项输入`, provenance, "cockpit-disclosure cockpit-provenance"));
  const boundaries=el("div");
  boundaries.append(el("p", "行动力度尚未评估；建议支持度只表示相对语义支持，不是收益概率或仓位比例。"),
    el("p", "历史收盘区间描述价格位置，不提供合理价值、支撑、阻力或买入底部。"));
  if (f.price_source==="COMPLETED_CLOSE") boundaries.append(el("p", `本次自动采用合资格收盘参考，时间为 ${date(f.price_as_of)}；不是盘中报价。`));
  for (const assertion of f.assertions) boundaries.append(el("p", `你提供的信息：${assertion.text}（待核实）`));
  for (const text of record.semantic.depth?.missing_context || []) boundaries.append(el("p", text));
  boundaries.append(el("p", `分析时点：${date(record.analysis_cutoff)}。未填写的投资周期与政策保持未知。`));
  details.append(cockpitDisclosure("判断边界", boundaries));result.append(details);
  const traceBody=el("div");
  traceBody.append(el("p", "展开后读取本次已保存的执行记录。", "small muted"));
  const traceDisclosure=cockpitDisclosure("查看执行与模型记录", traceBody);
  let traceLoaded=false;
  traceDisclosure.addEventListener("toggle", async()=>{
    if (!traceDisclosure.open || traceLoaded) return;
    traceLoaded=true;
    try {
      const run=await api(`/api/runs/${encodeURIComponent(envelope.run_id)}`);
      if (!traceDisclosure.isConnected) return;
      renderTrace(run);
      traceBody.replaceChildren(...$("trace").childNodes);
      traceBody.append(el("p", `判断 ${record.decision_id} · 冻结哈希 ${record.record_hash}`, "small muted"),
        el("p", `Cockpit ${envelope.cockpit_id} · 冻结哈希 ${envelope.record_hash}`, "small muted"));
    } catch { traceLoaded=false;traceBody.replaceChildren(el("p", "执行记录暂时无法读取，请重新展开。", "status-line error")); }
  });
  details.append(traceDisclosure);
  const lineage=el("p", "", "small muted");
  lineage.append(el("span", `判断已保存 · ${date(envelope.created_at)} · `));
  const restore=el("a", "本次判断记录");restore.href=`/?cockpit=${encodeURIComponent(envelope.cockpit_id)}`;lineage.append(restore);result.append(lineage);
  $("answer-state").textContent="判断完成";
}

async function loadCockpit(cockpitId) {
  const envelope=await api(`/api/cockpit/${encodeURIComponent(cockpitId)}`);
  state.cockpitResult=envelope;
  if (envelope.version===2) {
    const frozen=await api(`/api/cockpit/${encodeURIComponent(cockpitId)}/context`);
    renderCockpit(envelope,null,frozen.evidence,frozen);
  } else if (envelope.decision_id) {
    const [record,evidence]=await Promise.all([
      api(`/api/decisions/${encodeURIComponent(envelope.decision_id)}`),
      api(`/api/decisions/${encodeURIComponent(envelope.decision_id)}/evidence`)]);
    renderCockpit(envelope,record,evidence);
  } else if (["DAILY","EVENT_RESEARCH"].includes(envelope.framing.workflow)) {
    const run=await api(`/api/runs/${encodeURIComponent(envelope.run_id)}`);
    renderAnswer(run.answer,envelope.framing.query);await loadInspection(envelope.run_id);
    $("result").prepend(el("p", cockpitFramingLine(envelope.framing), "cockpit-framing"));
    if (envelope.brief.boundary) $("result").append(el("p", envelope.brief.boundary, "small muted"));
    $("query-input").value=envelope.framing.query;
    $("cockpit-question-summary").textContent=`${envelope.framing.query} · 点击换个问题`;
    $("cockpit-composer").open=false;
  } else renderCockpit(envelope,null,[]);
  return envelope;
}

function appendFastDetails(result,envelope,evidence,frozen) {
  const controls=el("div", null, "cockpit-research-controls");
  const deep=el("button", "深入研究", "primary");deep.type="button";
  controls.append(deep,el("span", "复用本次证据，展开完整行动权衡", "small muted"));
  result.append(controls);
  const deepHost=el("div", null, "cockpit-deep-host");result.append(deepHost);
  let deepJob=null,deepRecord=null,recoveryRun=null;
  const current=()=>deepHost.isConnected && state.cockpitResult?.cockpit_id===envelope.cockpit_id;
  function failedDeep(detail) {
    const causes={timeout:"模型请求超时。",rate_limit:"模型请求已达上限，请稍后重试。",
      transport:"模型连接中断。",schema_validation:"模型结果未通过校验。"};
    const text=detail?.code==="DECISION_INTERNAL_ERROR"
      ? "深入研究发生内部错误，未生成完整判断。快速判断和冻结证据已保留。"
      : detail?.code==="DECISION_JOB_NOT_FOUND"
      ? "研究进度已不可用，可能是服务重启。快速判断和冻结证据已保留。"
      : `${causes[detail?.judge_error] || errorMessage(detail)} 快速判断和冻结证据已保留。`;
    const nodes=[el("p",text,"status-line error")];
    if (detail?.job_id) nodes.push(el("p",`任务编号：${detail.job_id}`,"small muted"));
    if (recoveryRun) {
      const retry=el("button","复用证据，继续深入研究");retry.type="button";
      retry.addEventListener("click",()=>deep.click());nodes.push(retry);
    }
    deepHost.replaceChildren(...nodes);
    deep.textContent=recoveryRun?"重试深入研究":"重新运行深入研究";
  }
  deep.addEventListener("click",async()=>{
    if (deep.disabled) return;
    if (deepRecord) {deepHost.hidden=!deepHost.hidden;deep.textContent=deepHost.hidden?"查看深入研究":"收起深入研究";return;}
    deep.disabled=true;deep.textContent="正在深入研究…";
    deepHost.replaceChildren(el("p", "快速判断保留在上方。深入研究正在使用同一批冻结证据。", "small muted"));
    try {
      if (recoveryRun || !deepJob) {
        const path=recoveryRun ? `/api/decisions/recover/${encodeURIComponent(recoveryRun)}`
          : `/api/cockpit/${encodeURIComponent(envelope.cockpit_id)}/deep`;
        const start=await api(path,{method:"POST"});
        if (!current()) return;
        deepJob=start.job_id;recoveryRun=null;
      }
      let job;
      while (true) {
        job=await api(`/api/decisions/jobs/${encodeURIComponent(deepJob)}`);
        if (!current()) return;
        if (job.status!=="running") break;
        await new Promise(resolve=>setTimeout(resolve,450));
      }
      if (job.status==="error") {
        recoveryRun=job.error?.code==="DECISION_JUDGE_FAILED" ? job.error.run_id : null;
        deepJob=null;
        failedDeep(job.error);
        return;
      }
      const [record,deepEvidence]=await Promise.all([
        api(`/api/decisions/${encodeURIComponent(job.decision_id)}`),
        api(`/api/decisions/${encodeURIComponent(job.decision_id)}/evidence`)]);
      if (!current()) return;
      // Reuse the existing renderer while preserving the Fast result and evidence.
      const saved=[...$("result").childNodes],fastRun=envelope.run_id;
      let content;
      try {renderTdb(record,deepEvidence);content=[...$("result").childNodes];}
      finally {$("result").replaceChildren(...saved);state.evidence=evidence;setRunId(fastRun);
        $("evidence-count").textContent=String(evidence.length);renderEvidence();}
      deepRecord=record;
      deepHost.replaceChildren(...content);deep.textContent="收起深入研究";
      $("answer-state").textContent="快速判断 · 深入研究已完成";
    } catch (error) {
      if (!current()) return;
      if (error.detail) {
        if (error.detail.code==="DECISION_JOB_NOT_FOUND") {
          deepJob=null;failedDeep(error.detail);
        } else if (deepJob && !recoveryRun) {
          deepHost.replaceChildren(el("p",`${errorMessage(error.detail,error.status)} 可继续读取同一个任务。`,"status-line error"));
          deep.textContent="继续查看深入研究";
        } else failedDeep(error.detail);
      } else {
        const text=recoveryRun ? "恢复请求暂时未能确认。可再次复用冻结证据恢复。"
          : deepJob ? "深入研究暂时未能读取。再次点击可继续查看同一个任务。"
          : "深入研究请求暂时未能确认。可重新提交并复用冻结证据。";
        deepHost.replaceChildren(el("p",text,"status-line error"));
        deep.textContent=recoveryRun?"重试深入研究":deepJob?"继续查看深入研究":"重新提交深入研究";
      }
    }
    finally {deep.disabled=false;}
  });
  const details=el("div",null,"cockpit-details");
  const sources=el("div");
  sources.append(el("p", `本次冻结 ${evidence.length} 条证据与计算来源。关键引用 ${envelope.brief.decision_evidence_ids.length} 条。`),
    evidenceLinks(evidence.map(e=>e.evidence_id)));
  details.append(cockpitDisclosure(`查看全部 ${evidence.length} 条证据与计算来源`,sources,"cockpit-disclosure cockpit-provenance"));
  const context=el("div");
  for (const check of frozen.investigation.premise_verification) context.append(el("p",check.explanation));
  for (const w of frozen.investigation.technical.windows) if (w.status==="AVAILABLE") context.append(el("p",
    `近 ${w.sessions} 个已提供完成交易日：收盘变化 ${Number(w.recent_return_pct).toFixed(2)}%，最大回撤 ${Number(w.max_drawdown_pct).toFixed(2)}%，日波动 ${Number(w.daily_realized_volatility_pct).toFixed(2)}%。`));
  context.append(renderMarketStructure(frozen.investment_context));
  details.append(cockpitDisclosure("前提查证与价格结构",context));
  const boundaries=el("div");
  boundaries.append(el("p", "行动力度是定性的执行意愿，不是股数、仓位比例或收益概率。技术描述只辅助时机与风险。"),
    el("p", "盘前价格按你提供的信息保留；完成收盘历史不能证明盘前跌幅或事件因果。未配置的周期与风险规则保持未知。"),
    el("p", `本次证据截止：${date(frozen.evidence_cutoff)}。刷新证据需要提交一个新问题。`));
  details.append(cockpitDisclosure("判断边界",boundaries));
  const traceBody=el("div");traceBody.append(el("p","展开读取本次保存的执行记录。"));
  const disclosure=cockpitDisclosure("查看执行与模型记录",traceBody);let loaded=false;
  disclosure.addEventListener("toggle",async()=>{
    if (!disclosure.open || loaded) return;loaded=true;
    try {
      const run=await api(`/api/runs/${encodeURIComponent(envelope.run_id)}`);
      if (!disclosure.isConnected) return;
      renderTrace(run);traceBody.replaceChildren(...$("trace").childNodes);
      traceBody.append(el("p",`冻结 checkpoint：${frozen.checkpoint_id} · 哈希 ${frozen.checkpoint_hash}`,"small muted"));
    } catch {loaded=false;traceBody.replaceChildren(el("p","执行记录暂时无法读取，请重新展开。"));}
  });details.append(disclosure);result.append(details);
  const saved=el("p",null,"small muted cockpit-saved");saved.append(el("span",`判断已保存 · ${date(envelope.created_at)} · `));
  const link=el("a","本次判断记录");link.href=`/?cockpit=${encodeURIComponent(envelope.cockpit_id)}`;saved.append(link);result.append(saved);
  $("answer-state").textContent="快速判断已完成";
}

async function observeCockpit(jobId) {
  while (true) {
    let job;
    try { job=await api(`/api/decisions/jobs/${encodeURIComponent(jobId)}`); }
    catch (error) { error.detail={code:"COCKPIT_OBSERVATION_LOST",job_id:jobId};throw error; }
    if (job.status==="error") {
      const error=new Error("研究尚未完成");error.detail={...job.error,job_id:jobId};throw error;
    }
    if (job.status==="success") return loadCockpit(job.cockpit_id);
    renderCockpitProgress(job);
    await new Promise(resolve=>setTimeout(resolve,450));
  }
}

function cockpitFailure(error) {
  const detail=error.detail || {};
  const messages={COCKPIT_PORTFOLIO_CHANGED:"研究开始时持仓已发生变化，请按最新组合重新提交。",
    COCKPIT_RESEARCH_FAILED:"本次公开证据或组合数据未能完成读取，可检查来源后重试。",
    DECISION_JUDGE_FAILED:"证据已保存，但判断未完成。可复用证据重新生成判断。",
    DECISION_JUDGE_UNAVAILABLE:"判断服务尚未配置。配置后可重新提交。",
    COCKPIT_OBSERVATION_LOST:"连接中断。可以继续查看原研究。"};
  const judgeMessages={timeout:"模型请求超时，证据已保存。可复用证据继续判断，无需重新检索。",
    transport:"连接判断服务中断，证据已保存。连接恢复后可复用证据继续判断。",
    authentication:"判断服务的凭据未通过验证。修正服务配置后，可复用已保存的证据。",
    rate_limit:"判断服务暂时达到请求限制。稍后可复用已保存的证据继续判断。",
    provider_unavailable:"判断服务暂时不可用，证据已保存。稍后可复用证据继续判断。",
    configuration:"判断服务配置不完整。完成配置后可继续判断。"};
  const judgeFailure=["DECISION_JUDGE_FAILED","DECISION_JUDGE_UNAVAILABLE"].includes(detail.code);
  const text=judgeFailure && judgeMessages[detail.judge_error] || messages[detail.code] || "本次未能形成判断，请稍后重试。";
  $("result").replaceChildren(el("h3", judgeFailure && detail.judge_error==="timeout" ? "模型请求超时" : "研究未完成"), el("p", text));
  $("answer-state").textContent="待继续";
  if (detail.run_id) setRunId(detail.run_id);
  if (detail.code==="DECISION_JUDGE_FAILED" || detail.code==="COCKPIT_OBSERVATION_LOST") {
    const button=el("button", detail.code==="DECISION_JUDGE_FAILED"?"复用证据，重新生成判断":"继续查看本次研究", "primary");
    button.addEventListener("click", async()=>{
      if (state.busy) return;busy(true);
      try {
        if (detail.code==="DECISION_JUDGE_FAILED") {
          const start=await api(`/api/decisions/recover/${encodeURIComponent(detail.run_id)}`,{method:"POST"});
          await observeCockpit(start.job_id);
        } else await observeCockpit(detail.job_id);
      } catch (next) {cockpitFailure(next);} finally {busy(false);}
    });$("result").append(button);
  }
}

async function submitCockpit() {
  const query=$("query-input").value.trim();if (!query) {$("query-input").focus();return;}
  if (cockpitCorrectionQuery!==query) clearCockpitCorrections();
  const payload={query};
  $("cockpit-composer").open=true;
  if ($("cockpit-symbol").value.trim()) payload.symbol=$("cockpit-symbol").value.trim();
  if ($("cockpit-price").value) payload.decision_price=$("cockpit-price").value;
  busy(true);clearInspection();message("query-status", "");$("answer-state").textContent="研究中";
  renderCockpitProgress({stage:"queued",completed_stages:[]});
  try {
    const start=await api("/api/cockpit/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    renderCockpitProgress({framing:start.framing,stage:"framing",completed_stages:[]});
    await observeCockpit(start.job_id);
  } catch (error) {cockpitFailure(error);} finally {busy(false);}
}

document.querySelector('a[href="#portfolio"]').addEventListener("click",()=>$("portfolio-disclosure").open=true);
const restoreCockpitId=new URLSearchParams(location.search).get("cockpit");
if (restoreCockpitId) loadCockpit(restoreCockpitId).catch(cockpitFailure);
