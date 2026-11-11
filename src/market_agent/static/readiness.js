"use strict";
// 设置 · 研究就绪 (D104). One state model for "can I start research now?", shown as a
// six-step ladder in Settings and as one line above the research submit button.
// Read-only GETs only. Prefers GET /api/research/readiness (server truth); without it,
// derives what the existing endpoints actually prove and marks everything else
// UNKNOWN. A saved key is never shown as ready, and no control here creates a budget
// grant, clears a Stop or renews an expired admission.
(() => {
  const ds = document.body.dataset;
  if (ds.portfolioPriority) return;
  const $ = id => document.getElementById(id);
  const main = document.querySelector("main");
  if (!main) return;
  const synthetic = ds.runtimeMode === "PUBLIC_DEMO" || Boolean(ds.ivsPreview);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const read = url => fetch(url, { cache: "no-store", headers: { Accept: "application/json" } })
    .then(r => r.ok ? r.json() : Promise.reject(Object.assign(new Error(String(r.status)), { status: r.status })));
  const optional = url => read(url).catch(() => null);

  const STEPS = ["KEY_SAVED", "AUTH_VERIFIED", "MODEL_QUALIFIED", "BUDGET_READY", "RIGHTS_CONSENT_READY", "LIVE_READY"];
  const STEP_NAME = { KEY_SAVED: "密钥已保存", AUTH_VERIFIED: "认证通过", MODEL_QUALIFIED: "模型资格有效", BUDGET_READY: "研究预算可用", RIGHTS_CONSENT_READY: "数据外传与同意", LIVE_READY: "可以发起研究" };
  const STEP_MEANS = {
    KEY_SAVED: "本机保存了这个模型服务的密钥。保存密钥不等于可以调用。",
    AUTH_VERIFIED: "服务确认过这把密钥有效。",
    MODEL_QUALIFIED: "所选模型有当前访问资格，价格卡未过期。",
    BUDGET_READY: "有经 Owner 批准、有上限的研究预算覆盖这次研究。",
    RIGHTS_CONSENT_READY: "允许外传的资料类别已确定；每次提交前由你确认外传预览。",
    LIVE_READY: "以上都满足，且没有停止发送。",
  };
  const STATE = { PASS: "通过", BLOCKED: "受阻", UNKNOWN: "未核实", NOT_REQUIRED: "不需要", PER_TASK: "每次确认" };
  const NEXT = {
    SAVE_KEY: "在「管理密钥与模型」中保存这个服务的密钥。",
    VERIFY: "在「管理密钥与模型」中验证密钥；若验证本身会计费，会先请你确认。",
    SELECT_MODEL: "选择一个已认证、价格卡有效的模型。",
    OWNER_APPROVAL_REQUIRED: "需要 Owner 按现有规则批准；本页不能创建或延长授权。",
    CONFIRM_PER_TASK: "提交研究时，服务器会列出将外传的内容，由你逐次确认。",
    UPDATE_PRICE_CARD: "需要更新模型价格卡后才能计费调用（由工程维护）。",
    NONE: "",
  };
  const MODE_NAME = { FAST: "Fast · 快速", DEEP: "Deep · 深入" };
  const PROVIDER_HEALTH = { openai: "OpenAI", deepseek: "DeepSeek", compatible: "Compatible" };

  // ---------- Derivation from existing endpoints (fallback until /api/research/readiness) ----------
  function step(name, state, extra = {}) { return { step: name, state, code: null, checked_at: null, next_action: "NONE", detail: "", ...extra }; }
  function fromExisting(profilesData, health) {
    const hub = Boolean(health && Array.isArray(health.providers));
    const blockers = profilesData?.dispatch_blockers || {};
    const listed = profilesData?.profiles || [];
    const defaults = profilesData?.default_profile_ids || {};
    const previewModes = profilesData?.preview_mode_availability || {};
    const modes = profilesData?.mode_availability || {};
    const result = { observed_at: health?.observed_at || null, source: "DERIVED_FROM_EXISTING_ENDPOINTS", override: null, profiles: [] };
    if (!profilesData) { result.unavailable = true; return result; }
    if (!listed.length) {
      // No profiles: the server runs a built-in test model (or none at all).
      result.test_model = Boolean(modes.FAST || modes.DEEP);
      return result;
    }
    if (Object.values(blockers).includes("NO_SEND_DEPLOYMENT")) result.override = { code: "NO_SEND_DEPLOYMENT", reason: "本部署禁止对外发送研究请求。" };
    for (const mode of ["FAST", "DEEP"]) {
      const candidates = listed.filter(p => p.mode === mode);
      const profile = candidates.find(p => p.profile_id === defaults[mode]) || candidates.find(p => p.available) || candidates[0];
      if (!profile) continue;
      const blocker = blockers[profile.profile_id] ?? null;
      const row = hub ? health.providers.find(r => r.provider === PROVIDER_HEALTH[profile.provider]) : null;
      const steps = Object.fromEntries(STEPS.map(s => [s, step(s, "UNKNOWN")]));
      // KEY: model-profiles "available" means the key is present (not that it works).
      steps.KEY_SAVED = profile.available ? step("KEY_SAVED", "PASS") : step("KEY_SAVED", "BLOCKED", { code: "MODEL_NOT_CONFIGURED", next_action: "SAVE_KEY" });
      if (row) {
        if (row.auth === "AUTH_VERIFIED" || row.auth === "MODEL_METADATA_ACCESS_VERIFIED") steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "PASS");
        else if (profile.available) steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "BLOCKED", { code: "AUTH_NOT_VERIFIED", next_action: "VERIFY" });
      }
      if (hub && profile.available) {
        // profile_dispatch_blockers checks in this order: key → task window → auth → budget → price card.
        if (blocker === null) {
          steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "PASS");
          steps.BUDGET_READY = step("BUDGET_READY", "PASS");
          steps.MODEL_QUALIFIED = step("MODEL_QUALIFIED", "PASS");
        } else if (blocker === "AUTH_NOT_VERIFIED") {
          steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "BLOCKED", { code: blocker, next_action: "VERIFY" });
        } else if (blocker === "BUDGET_RESERVATION_REQUIRED") {
          steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "PASS");
          steps.BUDGET_READY = step("BUDGET_READY", "BLOCKED", { code: blocker, next_action: "OWNER_APPROVAL_REQUIRED" });
        } else if (blocker === "PRICE_CARD_EXPIRED") {
          steps.AUTH_VERIFIED = step("AUTH_VERIFIED", "PASS");
          steps.BUDGET_READY = step("BUDGET_READY", "PASS");
          steps.MODEL_QUALIFIED = step("MODEL_QUALIFIED", "BLOCKED", { code: blocker, next_action: "UPDATE_PRICE_CARD" });
        } else if (blocker === "NO_SEND_DEPLOYMENT") {
          steps.BUDGET_READY = step("BUDGET_READY", "BLOCKED", { code: blocker, next_action: "OWNER_APPROVAL_REQUIRED" });
        } else if (blocker !== "MODEL_NOT_CONFIGURED") {
          // Task-window admission codes (current-task budget scope, model access, expiry).
          steps.BUDGET_READY = step("BUDGET_READY", "BLOCKED", { code: blocker, next_action: "OWNER_APPROVAL_REQUIRED" });
        }
      }
      steps.RIGHTS_CONSENT_READY = step("RIGHTS_CONSENT_READY", "PER_TASK", { next_action: "CONFIRM_PER_TASK" });
      const live = hub && previewModes[mode] === true && blocker === null && !result.override;
      steps.LIVE_READY = live ? step("LIVE_READY", "PASS")
        : step("LIVE_READY", hub ? "BLOCKED" : "UNKNOWN", { detail: hub ? "" : "未启用 Provider Hub，无法预先核实；提交时由服务器检查。" });
      const ordered = STEPS.map(s => steps[s]);
      const first = ordered.find(s => s.state === "BLOCKED" && s.step !== "LIVE_READY");
      result.profiles.push({ profile_id: profile.profile_id, mode, provider: profile.provider, model: profile.model, steps: ordered,
        first_blocker: live ? null : first?.step || (hub ? "LIVE_READY" : null), cost_cap: null });
    }
    result.hub = hub;
    return result;
  }

  // ---------- Root A0 shape: model-profiles[].readiness + /api/research/admission ----------
  // stages: VERIFIED | MISSING | NOT_VERIFIED | REQUEST_CONFIRMATION_REQUIRED | BLOCKED.
  // first_blocker / next_action are codes. Absent or unknown values never read as ready.
  const STAGE_STATE = { VERIFIED: "PASS", MISSING: "BLOCKED", NOT_VERIFIED: "UNKNOWN", REQUEST_CONFIRMATION_REQUIRED: "PER_TASK", BLOCKED: "BLOCKED" };
  const BLOCKER_STEP = { MODEL_NOT_CONFIGURED: "KEY_SAVED", AUTH_NOT_VERIFIED: "AUTH_VERIFIED", PRICE_CARD_EXPIRED: "MODEL_QUALIFIED",
    api_budget_model_access: "MODEL_QUALIFIED", BUDGET_RESERVATION_REQUIRED: "BUDGET_READY", CURRENT_REVIEWED_PARENT_POLICY_REQUIRED: "BUDGET_READY" };
  const SERVER_NEXT = { SAVE_LOCAL_KEY: "SAVE_KEY", VERIFY_PROVIDER: "VERIFY", REFRESH_REVIEWED_PRICE_QUALIFICATION: "UPDATE_PRICE_CARD",
    REFRESH_MODEL_QUALIFICATION: "REFRESH_MODEL", USE_AN_ADMITTED_LIVE_RUNTIME: "ADMITTED_RUNTIME", REVIEW_PARENT_POLICY: "PARENT_POLICY",
    OPEN_RESEARCH_CONFIRMATION: "CONFIRM_PER_TASK" };
  NEXT.REFRESH_MODEL = "需要刷新模型访问资格（由工程维护）。";
  NEXT.ADMITTED_RUNTIME = "本部署禁止对外发送；需要在已准入的研究运行时中发起。";
  NEXT.PARENT_POLICY = "需要一份经 Owner 批准并独立审查的研究预算策略；本页不能创建或延长。";
  function fromProfileReadiness(profilesData, admission) {
    const listed = (profilesData.profiles || []).filter(p => p.readiness && p.readiness.stages);
    const defaults = profilesData.default_profile_ids || {};
    const previewModes = profilesData.preview_mode_availability || {};
    const result = { observed_at: null, source: "SERVER_PROFILE_READINESS", override: null, profiles: [], admission: admission || null, hub: true };
    if (Object.values(profilesData.dispatch_blockers || {}).includes("NO_SEND_DEPLOYMENT")) result.override = { code: "NO_SEND_DEPLOYMENT", reason: "本部署禁止对外发送研究请求。" };
    for (const mode of ["FAST", "DEEP"]) {
      const candidates = listed.filter(p => p.mode === mode);
      const profile = candidates.find(p => p.profile_id === defaults[mode]) || candidates.find(p => p.available) || candidates[0];
      if (!profile) continue;
      const r = profile.readiness;
      const blockedStep = r.first_blocker ? (BLOCKER_STEP[r.first_blocker] || "BUDGET_READY") : null;
      const steps = STEPS.map(name => {
        const state = STAGE_STATE[r.stages[name]] || "UNKNOWN";
        const s = step(name, state);
        if (name === blockedStep) Object.assign(s, { state: "BLOCKED", code: r.first_blocker, next_action: SERVER_NEXT[r.next_action] || "PARENT_POLICY" });
        if (name === "BUDGET_READY" && r.expires_at) s.detail = `有效至 ${String(r.expires_at).slice(0, 16).replace("T", " ")} UTC`;
        if (state === "PER_TASK") s.next_action = "CONFIRM_PER_TASK";
        return s;
      });
      // LIVE is never PASS from this list: the server still requires the exact per-request confirmation.
      const liveStep = steps.find(s => s.step === "LIVE_READY");
      const previewOk = !r.first_blocker && previewModes[mode] === true && !result.override;
      liveStep.state = previewOk ? "PER_TASK" : "BLOCKED";
      liveStep.detail = previewOk ? "可以发起；每次提交仍需你确认外传预览与费用上限。" : "";
      result.profiles.push({ profile_id: profile.profile_id, mode, provider: profile.provider, model: profile.model, steps,
        first_blocker: previewOk ? null : blockedStep || "LIVE_READY", preview_ready: previewOk, cost_cap: null });
    }
    return result;
  }

  // ---------- Rendering ----------
  const section = el("section", null, "settings-view");
  section.id = "settings"; section.setAttribute("aria-labelledby", "settings-ready-title");
  const head = el("div", null, "settings-head");
  const title = el("h2", "研究就绪", null); title.id = "settings-ready-title";
  const manage = el("button", "管理密钥与模型", "primary"); manage.type = "button";
  manage.addEventListener("click", () => window.dispatchEvent(new CustomEvent("mia-open-providers")));
  manage.hidden = synthetic; // Keyless replay cannot configure or activate providers.
  head.append(title, manage);
  const summary = el("p", "正在读取研究就绪状态…", "settings-summary"); summary.setAttribute("role", "status");
  const ladders = el("div", null, "settings-ladders");
  const note = el("p", "这里只读取状态，不发送任何研究请求。每一步「受阻」都写明原因和下一步；「未核实」表示服务器没有提供证据，不按通过处理。", "small muted");
  section.append(head, summary, ladders, note);
  const runtime = main.querySelector(":scope > .desk-runtime-details");
  if (runtime) runtime.before(section); else main.append(section);

  // One line next to the research submit button.
  const line = el("p", "", "desk-readiness-line small"); line.id = "desk-readiness"; line.setAttribute("role", "status");
  const form = $("desk-question-form");
  if (form) form.after(line);

  function ladder(profile) {
    const box = el("section", null, "ready-ladder");
    box.dataset.mode = profile.mode;
    const h = el("h3", `${MODE_NAME[profile.mode] || profile.mode} · ${profile.model || "服务器默认模型"}`);
    box.append(h);
    const list = el("ol", null, "ready-steps");
    for (const s of profile.steps) {
      const li = el("li", null, "ready-step"); li.dataset.step = s.step; li.dataset.state = s.state;
      const top = el("div", null, "ready-step-top");
      top.append(el("span", STEP_NAME[s.step] || s.step, "ready-step-name"), el("span", STATE[s.state] || s.state, "ready-state"));
      li.append(top, el("p", STEP_MEANS[s.step] || "", "small muted ready-means"));
      const reason = [s.detail, s.code && s.state === "BLOCKED" ? `原因代码 ${s.code}` : "", s.checked_at ? `核实于 ${String(s.checked_at).slice(0, 16).replace("T", " ")} UTC` : ""].filter(Boolean).join(" · ");
      if (reason) li.append(el("p", reason, "small ready-reason"));
      const next = NEXT[s.next_action];
      if (next && s.state !== "PASS") li.append(el("p", `下一步：${next}`, "small ready-next"));
      list.append(li);
    }
    box.append(list);
    if (profile.cost_cap && (profile.cost_cap.per_task_usd || profile.cost_cap.price_card_as_of))
      box.append(el("p", `每次研究上限 ${profile.cost_cap.per_task_usd ? "$" + profile.cost_cap.per_task_usd : "未提供"} · 价格卡 ${profile.cost_cap.price_card_as_of || "日期未提供"}`, "small muted"));
    return box;
  }

  function lineText(state) {
    if (synthetic) return ["合成回放：不调用外部模型，密钥、预算与同意都不适用。", "ok"];
    if (state.unavailable) return ["无法读取研究就绪状态；提交时由服务器检查。", "warn"];
    if (state.override) return [`研究发送已停止：${state.override.reason || state.override.code}`, "blocked"];
    if (state.test_model) return ["本运行使用内置测试模型，不调用外部模型服务。", "ok"];
    const fast = state.profiles.find(p => p.mode === "FAST"), deep = state.profiles.find(p => p.mode === "DEEP");
    const ready = p => p && (p.preview_ready === true || p.steps.find(s => s.step === "LIVE_READY")?.state === "PASS");
    if (ready(fast) && ready(deep)) return ["可以研究：Fast 与 Deep 均已就绪。提交前仍会确认费用上限与外传内容。", "ok"];
    if (ready(fast)) return ["可以发起 Fast；Deep 暂不可用。提交前仍会确认费用上限与外传内容。", "ok"];
    const p = fast || deep;
    if (!p) return ["没有可用的研究模型配置。", "blocked"];
    if (!p.first_blocker) return ["研究就绪未经预先核实；提交时由服务器检查。", "warn"];
    const blocked = p.steps.find(s => s.step === p.first_blocker);
    return [`暂不能发起研究：「${STEP_NAME[p.first_blocker]}」受阻。${NEXT[blocked?.next_action] || ""}`, "blocked"];
  }

  function render(state) {
    ladders.replaceChildren();
    section.dataset.source = state.source || "SERVER";
    if (synthetic) {
      summary.textContent = "合成回放：本页的研究由脚本完成，不调用外部模型；密钥、预算与外传同意不适用。";
    } else if (state.unavailable) {
      summary.textContent = "无法读取模型配置。研究提交时仍由服务器检查，已保存研究不受影响。";
    } else if (state.override) {
      summary.textContent = `研究发送已停止：${state.override.reason || state.override.code}。已保存研究、合成路径和本机预演不受影响。`;
    } else if (state.test_model) {
      summary.textContent = "本运行没有外部模型配置，研究由内置测试模型完成（不调用外部服务、不产生费用）。";
    } else {
      summary.textContent = state.source === "DERIVED_FROM_EXISTING_ENDPOINTS"
        ? "依据现有模型配置与来源健康推导；服务器尚未提供完整就绪清单，无法证明的步骤显示「未核实」。"
        : `服务器就绪清单 · 读取于 ${String(state.observed_at || "").slice(0, 16).replace("T", " ")} UTC`;
      if (state.source === "SERVER_PROFILE_READINESS") summary.textContent = "服务器逐项就绪状态（模型配置接口）。「每次确认」表示可以发起，但每次提交仍要确认外传预览与费用上限。";
      if (state.admission) {
        const a = state.admission, box = el("div", null, "ready-admission");
        box.dataset.status = a.status || "UNKNOWN";
        const ok = a.status === "PREVIEW_QUALIFIED";
        box.append(el("h3", ok ? "研究预算策略：已准入预览" : "研究预算策略：未准入"),
          el("p", [ok ? "可以生成研究预览；实际发送仍需每次确认。" : `受阻：${a.first_blocker || "原因未提供"}`,
            a.expires_at && `有效至 ${String(a.expires_at).slice(0, 16).replace("T", " ")} UTC`,
            a.remaining_attempts != null && `剩余次数 ${a.remaining_attempts}`, a.remaining_usd != null && `剩余额度 $${a.remaining_usd}`].filter(Boolean).join(" · "), "small"));
        if (!ok) box.append(el("p", `下一步：${NEXT.PARENT_POLICY}`, "small ready-next"));
        ladders.append(box);
      }
      state.profiles.forEach(p => ladders.append(ladder(p)));
    }
    const [text, tone] = lineText(state);
    line.textContent = text; line.dataset.tone = tone;
    if (tone !== "ok") { const a = el("a", "查看就绪清单", null); a.href = "#settings"; line.append(" ", a); }
    window.miaReadinessState = state;
    window.dispatchEvent(new CustomEvent("mia-readiness", { detail: state }));
  }

  let loading = null;
  async function load() {
    if (loading) return loading;
    loading = (async () => {
      if (synthetic) return render({ source: "SYNTHETIC", profiles: [] });
      const server = await optional("/api/research/readiness");
      if (server && Array.isArray(server.profiles)) return render({ ...server, source: "SERVER" });
      const [profiles, health, admission] = await Promise.all([optional("/api/research/model-profiles"), optional("/api/provider-source-health"), optional("/api/research/admission")]);
      if (profiles && (profiles.profiles || []).some(p => p.readiness && p.readiness.stages)) return render(fromProfileReadiness(profiles, admission));
      render(fromExisting(profiles, health));
    })().finally(() => { loading = null; });
    return loading;
  }
  void load();
  window.addEventListener("mia-view", event => { if (event.detail?.view === "settings") void load(); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") void load(); });
  window.miaReloadReadiness = load;
})();
