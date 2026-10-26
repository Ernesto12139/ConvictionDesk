"use strict";
// Browser side of the Provider Hub runtime contract (D-088, RUNTIME-INTELLIGENCE-002).
// - Local mutations carry the process session CSRF token when the Hub is enabled.
// - A new research run sends owned data only with a server consent receipt: the
//   person sees the exact server preview (destination, model, holdings, question,
//   source policy), confirms it, and the receipt id travels with the task POST.
//   The checkbox below is only how the person says yes; the server decides.
// - A running task can be stopped and its consent revoked; failures say what to do.
(() => {
  const runtime = document.body.dataset.runtimeMode;
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const button = (text, className) => { const b = el("button", text, className); b.type = "button"; return b; };
  const state = { enabled: false, token: null, jev: null };
  const consents = new Map();

  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = String(init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
    if (state.token && url.origin === location.origin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      const headers = new Headers(init.headers || {});
      headers.set("X-Provider-CSRF", state.token);
      init = { ...init, headers };
    }
    return nativeFetch(input, init);
  };

  async function json(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error("request_failed"); error.status = response.status; error.detail = body.detail; throw error; }
    return body;
  }
  const code = error => typeof error?.detail === "string" ? error.detail : error?.detail?.code || "";

  window.miaHubReady = (async () => {
    if (runtime !== "OWNER") return state;
    try {
      const capabilityResponse = await nativeFetch("/api/console/status", { cache: "no-store" });
      if (capabilityResponse.ok && (await capabilityResponse.json()).provider_hub_enabled === false) return state;
      const session = await nativeFetch("/api/providers/session", { cache: "no-store" });
      if (session.ok) { state.token = (await session.json()).csrf_token; state.enabled = true; }
      if (state.enabled) state.jev = await json("/api/providers/jev-policy").catch(() => null);
    } catch (_) { /* Hub disabled or unreachable: the existing path stays as it was. */ }
    window.dispatchEvent(new CustomEvent("mia-hub-ready", { detail: { enabled: state.enabled } }));
    return state;
  })();

  // ---------- Outbound preview and consent ----------
  const dialog = el("dialog", null, "egress-dialog"); dialog.id = "egress-dialog"; dialog.setAttribute("aria-labelledby", "egress-title");
  document.body.append(dialog);
  const CATEGORY = {
    OWNED_PORTFOLIO: ["你的问题、所选账户的持仓代码、股数、成本与现金", true, true],
    PUBLIC_FILINGS: ["SEC 公开文件与财务数据（Facts）", true, false],
    THIRD_PARTY_NEWS: ["第三方新闻：仅当服务器已登记许可时以摘要形式附带，否则省略", false, false],
    MARKET_QUOTES: ["行情数据：仅当服务器已登记许可时附带，否则省略", false, false],
  };
  const LABEL_MODE = { FAST: "快速研究", DEEP: "深度研究" };

  function confirmEgress(request) {
    return new Promise(resolve => {
      let preview = null, settled = false;
      const finish = value => { if (settled) return; settled = true; if (dialog.open) dialog.close(); resolve(value); };
      const head = el("h2", "发送前确认 · 将交给模型的内容"); head.id = "egress-title";
      const intro = el("p", "下面是服务器生成的外传预览。确认后服务器记录授权（15 分钟内有效），只在这次研究中使用；你可以随时停止并撤销。", "small muted");
      const choose = el("fieldset", null, "egress-categories"); choose.append(el("legend", "允许附带的数据类别"));
      const boxes = new Map();
      Object.entries(CATEGORY).forEach(([key, [label, checked, locked]]) => {
        const row = el("label", null, "egress-category"), box = el("input"); box.type = "checkbox"; box.checked = checked; box.disabled = locked;
        row.append(box, el("span", label)); choose.append(row); boxes.set(key, box);
      });
      let jevBox = null;
      if (state.jev && state.jev.mode !== "OFF" && state.jev.configured) {
        const row = el("label", null, "egress-category"); jevBox = el("input"); jevBox.type = "checkbox";
        row.append(jevBox, el("span", `同时发给 Jev 路由（另一个目的地，模式 ${state.jev.mode}）`)); choose.append(row);
      }
      const body = el("div", null, "egress-preview"); body.setAttribute("aria-live", "polite");
      const consent = el("input"); consent.type = "checkbox"; consent.disabled = true;
      const consentRow = el("label", null, "mia-consent"); consentRow.append(consent, el("span", "我已看过以上内容，同意发送给所列目的地用于本次研究。"));
      const status = el("p", null, "small"); status.setAttribute("role", "status");
      const cancel = button("取消"), make = button("生成外传预览", "primary"), go = button("确认并开始研究", "primary"); go.disabled = true; go.hidden = true;
      const actions = el("div", null, "mia-cost-actions"); actions.append(cancel, make, go);
      consent.addEventListener("change", () => { go.disabled = !consent.checked; });
      cancel.addEventListener("click", () => finish(null));
      dialog.oncancel = event => { event.preventDefault(); finish(null); };
      make.addEventListener("click", async () => {
        make.disabled = true; status.textContent = "正在生成预览（不调用模型）…";
        const categories = [...boxes].filter(([, box]) => box.checked).map(([key]) => key);
        try {
          preview = await json("/api/research/egress/preview", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ research: request, categories, auxiliary_targets: jevBox?.checked ? ["jev"] : [] }) });
          renderPreview(body, preview); choose.disabled = true;
          consent.disabled = false; make.hidden = true; go.hidden = false; status.textContent = "";
        } catch (error) { make.disabled = false; status.textContent = egressError(code(error)); }
      });
      go.addEventListener("click", async () => {
        go.disabled = true; status.textContent = "正在记录授权…";
        try {
          const receipt = await json("/api/research/egress/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preview_id: preview.preview_id, preview_hash: preview.preview_hash, confirmed: true }) });
          finish(receipt.consent_id);
        } catch (error) { go.disabled = false; status.textContent = egressError(code(error)); }
      });
      dialog.replaceChildren(head, intro, choose, body, consentRow, el("p", "可能产生模型费用；预算未预约时服务器会拒绝发送。", "mia-cost-fee"), status, actions);
      dialog.showModal(); cancel.focus();
    });
  }

  function renderPreview(container, preview) {
    const b = preview.binding, owned = preview.owned_payload || {}, portfolio = owned.portfolio;
    const facts = el("dl", null, "mia-cost-facts");
    const add = (k, v) => { const row = el("div"); row.append(el("dt", k), el("dd", v)); facts.append(row); };
    add("目的地", `${b.provider} · ${b.model} · ${LABEL_MODE[b.mode] || b.mode}`);
    add("网址", b.destination);
    Object.entries(preview.auxiliary_destinations || {}).forEach(([name, url]) => add(`附加目的地 ${name}`, url));
    Object.entries(preview.auxiliary_models || {}).forEach(([name, model]) => add(`附加模型 ${name}`, model));
    add("账户范围", b.account_id || "全部账户");
    add("问题原文", owned.question || "");
    add("有效期至", new Date(preview.expires_at).toLocaleString("zh-CN"));
    container.replaceChildren(facts);
    if (portfolio?.positions?.length) {
      const table = el("table", null, "egress-holdings"), headRow = el("tr");
      ["账户", "代码", "股数", "平均成本"].forEach(t => headRow.append(el("th", t))); table.append(headRow);
      portfolio.positions.forEach(p => { const row = el("tr"); [p.account_id, p.symbol, p.quantity, p.avg_cost].forEach(v => row.append(el("td", String(v ?? "")))); table.append(row); });
      const details = el("details", null, "egress-holdings-wrap"); details.open = portfolio.positions.length <= 8;
      details.append(el("summary", `将发送 ${portfolio.positions.length} 条持仓与 ${portfolio.accounts?.length || 0} 个账户的现金`), table);
      container.append(details);
    } else container.append(el("p", "没有持仓快照：只发送问题本身。", "small muted"));
    const policies = Object.entries(preview.source_policy || {});
    container.append(el("p", policies.length ? `资料来源规则：${policies.map(([name, p]) => `${name}→${p.category}`).join("，")}；未登记许可的第三方原文不会发送。` : "服务器未登记任何第三方来源许可：新闻与行情原文不会发送，只用公开文件与你的数据。", "small muted"));
  }

  function egressError(value) {
    return ({ egress_owned_portfolio_confirmation_required: "存在持仓快照，必须允许发送持仓才能研究。", egress_confirmation_expired: "预览已过期，请重新生成。",
      egress_confirmation_mismatch: "预览内容已变化，请重新生成。", RESEARCH_MODEL_PROFILE_UNAVAILABLE: "所选模型没有可用密钥：先在「模型与数据源」配置。",
      RESEARCH_MODEL_PROFILE_INVALID: "所选研究方式与模型不匹配。", CSRF_REJECTED: "会话已过期，请刷新页面后重试。" })[value] || "无法生成或确认外传预览，未发送任何内容。";
  }

  window.miaEgress = {
    get enabled() { return state.enabled; },
    async confirm(request) {
      await window.miaHubReady;
      if (!state.enabled) return undefined;
      const id = await confirmEgress(request);
      return id;
    },
    remember(taskId, consentId) { if (taskId && consentId) consents.set(taskId, consentId); },
  };

  // ---------- Stop and revoke while a task runs ----------
  const waiting = document.getElementById("desk-waiting");
  if (waiting && runtime === "OWNER") {
    const stop = button("停止研究并撤销授权", "egress-stop"); stop.hidden = true;
    waiting.querySelector(".desk-waiting-heading")?.append(stop);
    const sync = () => { stop.hidden = !state.enabled || document.getElementById("desk-state")?.dataset.state !== "RUNNING"; };
    new MutationObserver(sync).observe(document.getElementById("desk-state"), { attributes: true, childList: true });
    window.addEventListener("mia-hub-ready", sync);
    stop.addEventListener("click", async () => {
      const taskId = new URL(location.href).searchParams.get("research_task");
      if (!taskId) return;
      stop.disabled = true; stop.textContent = "正在停止…";
      const consentId = consents.get(taskId);
      try { if (consentId) await json(`/api/research/egress/${encodeURIComponent(consentId)}`, { method: "DELETE" }); } catch (_) { /* cancel still runs */ }
      try { await json(`/api/research/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); stop.textContent = "已停止；资料已保留"; }
      catch (_) { stop.disabled = false; stop.textContent = "停止失败，请重试"; }
    });
  }

  // ---------- Failure kinds: what happened and what to do next ----------
  const FAILURE = {
    BUDGET_BLOCKED: ["预算未授权：服务器没有为这次模型调用预约费用，因此没有发送任何内容。", "需要先由 Lead 在账本中预约预算。"],
    DATA_RIGHTS_BLOCKED: ["数据外传未获授权、已过期或已撤销：没有发送受限内容。", "重新研究时会再次显示外传预览。"],
    PROVIDER_TIMEOUT: ["模型或数据源超时。已取得的资料已保留。", "可以直接重试，或改用快速研究。"],
    CANCELLED: ["研究已停止。已取得的资料和之前的版本都已保留。", "可以在下一轮继续。"],
    EVIDENCE_GAP: ["缺少回答这个问题所需的合格证据。", "可以补充或缩小问题，或者等待新的资料。"],
    MODEL_UNSUPPORTED_CLAIM: ["模型给出了没有证据支持的说法或数字，已被校验器拒绝，没有保存为结论。", "可以重试，或改用深度研究补查资料。"],
    CONTRACT_PROJECTION_MISSING: ["模型没有按要求的结构作答，结论未保存。", "可以重试同一问题。"],
    PROVIDER_ERROR: ["模型服务返回错误，可能是密钥、权限或服务异常。", "请到「模型与数据源」验证密钥后重试。"],
  };
  window.miaFailureHint = task => {
    const known = FAILURE[task?.error_kind];
    if (!known) return null;
    const box = el("div", null, "desk-failure-kind"); box.dataset.kind = task.error_kind;
    box.append(el("p", known[0]), el("p", known[1], "small muted"));
    if (["PROVIDER_ERROR", "BUDGET_BLOCKED"].includes(task.error_kind)) {
      const open = button("打开模型与数据源"); open.addEventListener("click", () => window.dispatchEvent(new CustomEvent("mia-open-providers"))); box.append(open);
    }
    return box;
  };
})();
