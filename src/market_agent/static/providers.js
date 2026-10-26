"use strict";
// 模型与数据源 — the Provider Hub panel over the D-088 backend contract.
// Keys go out once in a PUT body to the local, CSRF-protected Hub and are never
// read back, stored in the browser or placed in a URL. Each provider shows separate
// states: saved locally, auth verified, entitlement verified, model ready, error.
// Saving a key never authorizes spending; every run still needs consent and budget.
(() => {
  const runtime = document.body.dataset.runtimeMode;
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const button = (text, className) => { const b = el("button", text, className); b.type = "button"; return b; };
  const JSON_HEADERS = { "Content-Type": "application/json" };
  const STATE = { CONFIGURED_LOCAL: ["已保存在本机", "provider-ok"], AUTH_VERIFIED: ["授权已验证", "provider-ok"],
    ENTITLEMENT_VERIFIED: ["模型权限已验证", "provider-ok"], MODEL_READY: ["模型可调用", "provider-ok"],
    BALANCE_UNKNOWN: ["余额未知", "provider-badge"], ERROR: ["异常", "provider-bad"], NOT_CONFIGURED: ["未配置", "provider-badge"] };
  const ERRORS = { PROVIDER_TIMEOUT: "验证超时", PROVIDER_UNAVAILABLE: "服务不可用", AUTHENTICATION: "密钥无效或无权限",
    AUTH_PROBE_NOT_SUPPORTED: "该服务没有免费的授权验证接口", POTENTIALLY_BILLED_VERIFICATION_CONFIRMATION_REQUIRED: "验证这个数据源可能计费，需要你确认",
    PAID_VERIFICATION_CONFIRMATION_REQUIRED: "模型调用验证会产生费用，需要预算预约", PROVIDER_NOT_CONFIGURED: "还没有保存密钥",
    VAULT_UNAVAILABLE: "当前系统不支持本机加密保存，请用环境变量", CSRF_REJECTED: "会话已过期，请刷新页面", INVALID_REQUEST: "输入格式不正确" };
  const GROUPS = [["研究模型", ["OpenAI", "DeepSeek", "Compatible"]], ["快速路由（可选）", ["Jev"]], ["数据源", ["Finnhub", "Massive", "SEC", "Longbridge"]]];
  const PURPOSE = { OpenAI: "快速 / 深度研究", DeepSeek: "快速 / 深度研究", Compatible: "服务器配置的兼容端点", Jev: "意图、工具与深度路由，受约束的方向候选",
    Finnhub: "公司新闻、报价（可能延迟）", Massive: "收盘行情", SEC: "公开文件与财务数据（联系邮箱，非密钥）", Longbridge: "个人行情参考（只读）" };
  const JEV_MODES = [["OFF", "关闭"], ["SHADOW", "影子：只记录，不影响结果"], ["COMPARE", "比较：与验证后的结论对照"], ["FAST_CANDIDATE", "候选：提出受约束的方向，由校验器与解释模型审查"]];

  const dialog = el("dialog", null, "providers-dialog"); dialog.id = "providers-dialog"; dialog.setAttribute("aria-labelledby", "providers-title");
  document.body.append(dialog);
  const status = el("p", null, "providers-status-line"); status.setAttribute("role", "status");
  let data = null;

  async function request(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error("failed"); error.code = typeof body.detail === "string" ? body.detail : body.detail?.code; throw error; }
    return body;
  }
  const say = error => ERRORS[error?.code] || ERRORS[String(error?.code || "").toUpperCase()] || "操作失败，原有设置已保留。";

  async function load() {
    const hub = await window.miaHubReady;
    const profiles = await request("/api/research/model-profiles").catch(() => ({ profiles: [] }));
    if (!hub?.enabled) return { enabled: false, profiles: profiles.profiles || [] };
    const [inventory, preferences, jev] = await Promise.all([request("/api/providers"), request("/api/providers/preferences"),
      request("/api/providers/jev-policy").catch(() => null)]);
    return { enabled: true, providers: inventory.providers, preferences, jev, profiles: profiles.profiles || [] };
  }

  function badges(provider) {
    const wrap = el("div", null, "provider-status");
    const model = ["OpenAI", "DeepSeek", "Compatible"].includes(provider.provider);
    const configured = provider.states?.includes("CONFIGURED_LOCAL");
    // Balance only matters for a configured model provider; elsewhere it is noise.
    const states = (provider.states || []).filter(s => s !== "BALANCE_UNKNOWN" || (model && configured));
    if (!configured && !states.length) states.push("NOT_CONFIGURED");
    states.forEach(s => { const [label, cls] = STATE[s] || [s, "provider-badge"]; wrap.append(el("span", label, `provider-badge ${cls}`)); });
    wrap.append(el("span", provider.activation === "LIVE_FOR_MODEL_PROFILES" ? "保存后下一次调用生效" : "数据源：重启后生效", "provider-badge"));
    if (provider.error) wrap.append(el("span", ERRORS[provider.error] || provider.error, "provider-badge provider-bad"));
    if (provider.actual_model) wrap.append(el("span", `实际模型 ${provider.actual_model}`, "provider-badge"));
    if (provider.checked_at) wrap.title = `验证于 ${new Date(provider.checked_at).toLocaleString("zh-CN")}`;
    return wrap;
  }

  function row(provider) {
    const name = provider.provider, configured = provider.states?.includes("CONFIGURED_LOCAL");
    const wrap = el("div", null, "provider-row"); wrap.dataset.provider = name;
    const title = el("div", null, "provider-name"); title.append(el("strong", name === "Compatible" ? "OpenAI 兼容接口" : name), el("span", PURPOSE[name] || ""));
    const actions = el("div", null, "provider-actions");
    const form = el("form", null, "provider-key"); form.hidden = true;
    const input = el("input"); input.type = name === "SEC" ? "text" : "password"; input.autocomplete = "off"; input.spellcheck = false;
    input.placeholder = name === "SEC" ? "你的姓名与联系邮箱" : `${name} API Key`; input.setAttribute("aria-label", input.placeholder);
    const save = el("button", "保存到本机", "primary"); save.type = "submit"; form.append(input, save);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const secret = input.value.trim(); input.value = "";
      if (!secret) return;
      status.textContent = `正在保存 ${name}…`;
      try { await request(`/api/providers/${name}/credential`, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ secret }) });
        status.textContent = `${name} 已保存。${provider.activation === "LIVE_FOR_MODEL_PROFILES" ? "下一次研究即可使用。" : "数据源需重启工作台后生效。"}`; await refresh(); }
      catch (error) { status.textContent = say(error); }
    });
    const edit = button(configured ? "更换" : "添加"); edit.addEventListener("click", () => { form.hidden = !form.hidden; if (!form.hidden) input.focus(); });
    actions.append(edit);
    if (configured && ["OpenAI", "DeepSeek", "Finnhub", "Massive", "Longbridge"].includes(name)) {
      const check = button("验证授权");
      check.addEventListener("click", async () => {
        const billed = provider.verification?.AUTH?.may_bill;
        if (billed && !confirm(`验证 ${name} 会发送一次可能计费的请求。继续？`)) return;
        status.textContent = `正在验证 ${name}（不调用模型）…`;
        try { await request(`/api/providers/${name}/verify`, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ kind: "AUTH", allow_paid: Boolean(billed) }) });
          await refresh(); const now = data.providers.find(p => p.provider === name); status.textContent = `${name}：${now?.error ? (ERRORS[now.error] || now.error) : "授权已验证"}`; }
        catch (error) { status.textContent = `${name}：${say(error)}`; }
      });
      actions.append(check);
    }
    if (configured) {
      const remove = button("移除");
      remove.addEventListener("click", async () => {
        if (!confirm(`从本机移除 ${name} 的密钥？`)) return;
        try { await request(`/api/providers/${name}/credential`, { method: "DELETE" }); status.textContent = `${name} 已移除。`; await refresh(); }
        catch (error) { status.textContent = say(error); }
      });
      actions.append(remove);
    }
    wrap.append(title, badges(provider), actions, form);
    return wrap;
  }

  function modes() {
    const block = el("section", null, "providers-group"); block.append(el("h3", "研究方式与模型"));
    const form = el("form", null, "providers-models");
    const pick = (label, mode) => {
      const wrap = el("label", label), select = el("select");
      data.profiles.filter(p => p.mode === mode).forEach(p => { const o = el("option", `${p.provider} · ${p.model}${p.available ? "" : "（未配置密钥）"}`); o.value = p.profile_id; select.append(o); });
      select.value = data.preferences[mode.toLowerCase()] || select.value; wrap.append(select); form.append(wrap); return select;
    };
    const fast = pick("快速研究（Fast）", "FAST"), deep = pick("深度研究（Deep）", "DEEP");
    const jevWrap = el("label", "Jev 路由"), jev = el("select");
    JEV_MODES.forEach(([v, t]) => { const o = el("option", t); o.value = v; jev.append(o); });
    jev.value = data.jev?.mode || "OFF"; jev.disabled = !data.jev; jevWrap.append(jev); form.append(jevWrap);
    const save = el("button", "保存", "primary"); save.type = "submit"; const cell = el("div"); cell.append(save); form.append(cell);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      try {
        await request("/api/providers/preferences", { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ fast: fast.value, deep: deep.value }) });
        if (data.jev && jev.value !== data.jev.mode) await request("/api/providers/jev-policy", { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ mode: jev.value }) });
        status.textContent = "已保存；下一次研究使用新的默认模型。"; await refresh();
      } catch (error) { status.textContent = say(error); }
    });
    block.append(form);
    if (data.jev) block.append(el("p", `Jev：每次研究最多 ${data.jev.max_per_task} 次、每天 ${data.jev.max_per_day} 次，单次 ${data.jev.timeout_seconds} 秒超时${data.jev.circuit_open ? "，当前已熔断" : ""}。Jev 不做任何数值计算，分歧会标为“需要研究”。`, "providers-note"));
    return block;
  }

  function render() {
    const head = el("div", null, "providers-head"), title = el("h2", "模型与数据源"); title.id = "providers-title";
    const close = button("关闭"); close.addEventListener("click", () => dialog.close()); head.append(title, close);
    if (!data.enabled) {
      const list = el("ul", null, "providers-readonly");
      data.profiles.forEach(p => list.append(el("li", `${p.mode === "FAST" ? "快速" : "深度"} · ${p.provider} ${p.model} · ${p.available ? "本次运行可用" : "未加载密钥"}`)));
      dialog.replaceChildren(head, el("p", "这次运行没有开启本机 Provider Hub，只能查看。以 Provider Hub 方式启动后，可在这里保存和验证密钥（Windows 本机加密），并按次确认外传内容。", "providers-note"), list);
      return;
    }
    dialog.replaceChildren(head, el("p", "密钥只保存在这台电脑（Windows 当前用户加密），不会显示或回传给浏览器。保存密钥不等于授权扣费：每次研究都要你确认外传内容，并且需要预算。", "providers-note"), status, modes());
    GROUPS.forEach(([label, names]) => {
      const group = el("section", null, "providers-group"); group.append(el("h3", label));
      names.forEach(name => { const p = data.providers.find(x => x.provider === name); if (p) group.append(row(p)); });
      dialog.append(group);
    });
  }

  async function refresh() { data = await load(); render(); }
  async function open() {
    if (runtime !== "OWNER") return;
    dialog.replaceChildren(el("p", "正在读取配置…", "small muted"));
    if (!dialog.open) dialog.showModal();
    try { await refresh(); } catch (_) { dialog.replaceChildren(el("p", "无法读取模型与数据源配置。", "desk-error")); }
  }
  window.addEventListener("mia-open-providers", open);
  if (runtime === "OWNER") {
    const entry = button("⚙ 模型与数据源", "rail-providers"); entry.id = "open-providers";
    entry.addEventListener("click", open);
    document.querySelector(".rail-foot")?.prepend(entry);
  }

  // The cost dialog's facts. With the Hub, the server preview that follows is the
  // consent step; without it, the existing server gate refuses owned-data sends.
  window.miaOutboundDisclosure = () => window.miaEgress?.enabled
    ? { facts: [["数据外传", "下一步会显示服务器生成的外传预览（目的地、模型、持仓与问题），由你确认后才发送"]], consent: null }
    : { facts: [["数据外传", "本次运行未开启 Provider Hub：没有外传授权和预算预约时，服务器拒绝发送"]], consent: null };
})();
