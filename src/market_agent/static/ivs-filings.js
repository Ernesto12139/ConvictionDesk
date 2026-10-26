"use strict";
(() => {
  if (document.body.dataset.runtimeMode === "PUBLIC_DEMO") return;
  const publicIvs = document.body.dataset.publicIvs === "1";
  const host = document.getElementById("today-screened-title");
  if (!host) return;
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const lane = node("section", null, "ivs-filings"); lane.id = "ivs-filings"; lane.setAttribute("aria-labelledby", "ivs-filings-title");
  const heading = node("div", null, "ivs-filings-heading"), title = node("h3", "公司原始披露"); title.id = "ivs-filings-title";
  const refresh = node("button", "扫描 SEC 披露"); refresh.type = "button";
  heading.append(title, refresh);
  const status = node("p", "正在读取披露覆盖状态…", "small muted"); status.setAttribute("role", "status");
  const list = node("ol", null, "ivs-filing-list"), coverage = node("div");
  lane.append(heading, status, node("p", "持仓优先，再按披露时间展示；不代表投资重要性。", "small muted"), list, coverage);
  // In the public slice this lane *is* the first screen: it goes directly under the
  // three-class banner instead of inside the market-pulse section, which the public
  // build hides because it has no market data source by design.
  const banner = document.getElementById("public-ivs-banner");
  if (publicIvs && banner) banner.after(lane); else host.before(lane);
  const states = {NOT_SCANNED: "尚未扫描当前范围。点击扫描，读取持仓和关注公司的公开 SEC 申报。", COMPLETE_NO_NEW_FILINGS: "已完成本次范围核查，时间窗口内没有新申报。", COMPLETE_WITH_FILINGS: "以下申报进入待核查队列；尚未形成研究判断。", PARTIAL: "部分标的未能完成核查；已取得的申报如下。", SOURCE_UNAVAILABLE: "SEC 来源暂不可用。恢复后可重新扫描，不能据此判断没有披露。", INVALID_DATA: "来源元数据未通过校验，未将其作为有效披露。"};
  const stamp = value => value ? new Date(value).toLocaleString("zh-CN", {hour12: false, timeZoneName: "short"}) : "时间未知";
  let scanned = false;
  function render(data) {
    if (data.status !== "NOT_SCANNED") scanned = true;
    list.replaceChildren(); coverage.replaceChildren();
    status.textContent = `${states[data.status] || "覆盖状态待核查。"}${data.scan_as_of ? ` 核查截至 ${stamp(data.scan_as_of)}。` : ""}`;
    if(data.failure_reason === "NO_SCOPE_TO_SCAN")status.textContent = "还没有持仓或关注标的；本次未扫描 SEC。";
    if(data.stale)status.textContent += " 以下保留上次取得的历史披露；本次刷新失败，不代表最新覆盖，恢复扫描后才能用于新研究。";
    const deltas = new Map((data.thesis_deltas?.deltas || []).map(row => [row.evidence_id, row]));
    const filings=[...(data.filings || [])].sort((a,b)=>(a.scope?.kind==='HOLDING'?0:1)-(b.scope?.kind==='HOLDING'?0:1) || Date.parse(b.available_at)-Date.parse(a.available_at) || a.accession.localeCompare(b.accession));
    for (const filing of filings) {
      const item = node("li", null, "ivs-filing"), delta = deltas.get(filing.evidence_id);
      const lead = node("div", null, "ivs-filing-lead");
      lead.append(node("strong", `${filing.symbol} · ${filing.event_label || filing.form}`), node("span", publicIvs ? (filing.scope?.kind === "HOLDING" ? "虚构持仓 · SYNTHETIC" : "虚构关注 · SYNTHETIC") : (filing.scope?.kind === "HOLDING" ? "你的持仓" : "你的关注"), "small muted"), node("span", "待核查，尚未研究", "small muted"));
      const source = node("a", `打开 SEC 原始申报 · ${filing.form}`); source.href = filing.source_url; source.target = "_blank"; source.rel = "noopener noreferrer"; source.dataset.secAccession = filing.accession;
      const time = filing.time_precision === "SECOND" ? `SEC 接收 ${stamp(filing.accepted_at)}` : `仅知申报日期 ${filing.filing_date || "未知"}；未伪造精确时刻`;
      item.append(lead, node("p", `${time} · 条目 ${(filing.items || []).join("、") || "未注明"}`, "small muted"));
      item.append(node("p", filing.scope?.kind === "HOLDING" ? "这家公司属于你的持仓范围，因此将原始披露列入待核查。" : "这家公司在你的关注范围，因此将原始披露列入待核查。", "small"));
      const held=filing.scope?.kind === "HOLDING", missing=delta?.status === "THESIS_NOT_AVAILABLE";
      let check = missing ? `先读原文，再记录你${held ? "持有" : "关注"} ${filing.symbol} 的理由，明确需要核查什么。` : delta?.next_check || filing.review_questions?.[0] || "先阅读原始披露，核查是否涉及当前投资逻辑。";
      if(!held)check=check.replaceAll("当前持有逻辑","关注理由");
      item.append(node("p", `下一步：${check}`));
      const detail = node("details"), summary = node("summary", delta?.status === "THESIS_READY" ? "查看申报前的 Thesis 待核查条件" : delta?.status === "THESIS_UNCONFIRMED" ? "申报前仅有未确认的 Thesis 草稿" : "申报前没有可用 Thesis");
      detail.append(summary);
      if (delta?.thesis_version_id) detail.append(node("p", `版本 ${delta.thesis_version_id}`, "small muted"));
      const mappedItems=(filing.items || []).filter(code=>code!=="9.01");
      for (const statement of delta?.statements || []) {const p=node("p");if(mappedItems.length)p.append(node("span",`对应本次条目 ${mappedItems.join("、")} · 待核查`,"ivs-condition-tag"));p.append(document.createTextNode(`${statement.bucket_label}：${statement.text}`));detail.append(p);}
      if(missing){const reasons={NO_PRIOR_VERSION:"申报前没有已保存的投资逻辑版本",PRIOR_VERSION_UNRESOLVED:"无法证明申报前存在可用版本",TIME_UNKNOWN:"申报时间未知，无法确定事前边界"};detail.append(node("p",reasons[delta.reason] || `事前版本不可用：${delta.reason || "原因待核查"}`,"small"),node("p",`可先打开原文；在研究问题中记录你为什么${held ? "持有" : "关注"}它；或直接研究这份披露。`,"small"));}
      if (!delta?.statements?.length) detail.append(node("p", "这是研究缺口，不表示披露没有影响。先读申报，再建立或确认投资逻辑。", "small"));
      detail.append(node("p", `Accession ${filing.accession}`, "small muted"), node("p", "条目代码用于提出核查问题，不证明价格或投资逻辑已经改变。", "small muted"));
      detail.append(node("p", `读取于 ${stamp(filing.provenance?.retrieved_at)} · 待核查，尚未研究`, "small muted"));
      const actions = node("div", null, "ivs-filing-actions"), ask = node("button", "研究这份披露"), whatif = node("button", "账户 What-if");
      ask.type = whatif.type = "button";
      ask.addEventListener("click", () => window.dispatchEvent(new CustomEvent("mia-ask-draft", {detail: {symbol: filing.symbol, filing_evidence_id: filing.evidence_id, question: `${filing.symbol} 的这份 SEC 披露需要如何核查？${check}`}})));
      whatif.addEventListener("click", () => window.miaOpenWhatIf?.(filing.symbol));
      actions.append(source, ask, whatif); item.append(detail, actions); list.append(item);
    }
    if (data.limitations?.length || data.per_symbol_coverage?.length) {
      const disclosure = node("details"), summary = node("summary", "覆盖范围与限制"); disclosure.append(summary);
      for (const row of data.per_symbol_coverage || []) disclosure.append(node("p", `${row.symbol} · ${row.status}`, "small"));
      for (const limit of data.limitations || []) disclosure.append(node("p", limit, "small muted"));
      coverage.append(disclosure);
    }
  }
  async function load(scan = false) {
    refresh.disabled = true; lane.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(scan ? "/api/today/filings/refresh" : "/api/today/filings", scan ? {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"} : {});
      if (!response.ok) { if (response.status === 429) { status.textContent = "扫描仍在进行或刚刚完成，请稍后重试；没有重复发起请求。"; return; } throw new Error("SEC_READ_FAILED"); }
      render(await response.json());
    } catch { status.textContent = "披露暂时无法读取。请重新扫描；这不表示没有新披露。"; }
    finally { refresh.disabled = false; lane.setAttribute("aria-busy", "false"); }
  }
  refresh.addEventListener("click", () => void load(true));
  void load().then(() => {
    // A stranger should not have to guess that a scan is what reveals the SEC cards.
    // One automatic scan on first open keeps the journey inside three minutes while
    // still showing the real coverage state (the button stays available afterwards).
    if (publicIvs && !scanned) { scanned = true; void load(true); }
  });
})();
