"use strict";

let entryBase = null;
let reviewedEntry = null;

function entryField(title, key, value, options = {}) {
  const labelNode = el("label", title);
  const input = el("input");
  input.name = key;
  input.value = value ?? "";
  input.required = !options.optional;
  input.maxLength = options.maxLength || 80;
  if (options.numeric) {
    input.inputMode = "decimal";
    input.pattern = options.positive ? "(?:[0-9]+(?:\\.[0-9]+)?)" : "[0-9]+(?:\\.[0-9]+)?";
    input.title = "请填写非负数字，例如 12.50";
  }
  labelNode.append(input);
  return labelNode;
}
function syncAccountOptions() {
  const accounts = [...$("entry-accounts").children].map((row) => ({
    id: row.dataset.accountId, name: row.querySelector('[name="name"]').value || "未命名账户",
  }));
  for (const select of $("entry-positions").querySelectorAll("select")) {
    const selected = select.value;
    select.replaceChildren(...accounts.map((account) => {
      const option = el("option", account.name);
      option.value = account.id;
      return option;
    }));
    if (accounts.some((account) => account.id === selected)) select.value = selected;
  }
}
function addEntryAccount(account = {}) {
  const row = el("div", null, "entry-account");
  row.dataset.accountId = account.account_id || crypto.randomUUID();
  row.append(entryField("账户名称", "name", account.name || ""),
    entryField("现金 · USD", "cash", account.cash ?? "0", { numeric: true }));
  row.querySelector('[name="name"]').addEventListener("input", syncAccountOptions);
  const remove = el("button", "移除", "quiet-button");
  remove.type = "button";
  remove.addEventListener("click", () => {
    if ($("entry-accounts").children.length === 1) {
      message("entry-error", "请至少保留一个账户。", true); return;
    }
    if ([...$("entry-positions").querySelectorAll("select")].some((select) => select.value === row.dataset.accountId)) {
      message("entry-error", "此账户仍有持仓，请先移除或转移对应的持仓行。", true); return;
    }
    row.remove(); syncAccountOptions();
  });
  row.append(remove); $("entry-accounts").append(row); syncAccountOptions();
}
function addEntryPosition(position = {}) {
  const row = el("div", null, "entry-position");
  row._original = position;
  const account = el("label", "账户");
  const select = el("select"); select.name = "account_id"; select.required = true;
  account.append(select);
  row.append(account, entryField("股票代码", "symbol", position.symbol, { maxLength: 15 }),
    entryField("持有股数", "quantity", position.quantity, { numeric: true, positive: true }),
    entryField("每股成本 · USD", "avg_cost", position.avg_cost, { numeric: true }));
  const remove = el("button", "移除", "quiet-button"); remove.type = "button";
  remove.addEventListener("click", () => row.remove()); row.append(remove);
  const classification = el("details", null, "entry-classification");
  classification.append(el("summary", "行业与主题（可选）"));
  const override = entryBase?.instrument_overrides?.[position.symbol] || {};
  const fields = el("div", null, "classification-fields");
  fields.append(entryField("行业", "sector", override.sector, { optional: true }),
    entryField("主题", "theme", override.theme, { optional: true }));
  classification.append(fields); row.append(classification);
  $("entry-positions").append(row); syncAccountOptions();
  if (position.account_id) select.value = position.account_id;
}
function openPortfolioEntry() {
  if (state.busy) return;
  setEntryMode("manual");
  entryBase = state.snapshot; reviewedEntry = null;
  $("entry-title").textContent = entryBase ? "管理你的组合" : "建立你的组合";
  $("entry-accounts").replaceChildren(); $("entry-positions").replaceChildren();
  $("entry-search").value = "";
  for (const account of entryBase?.accounts || [{ name: "我的账户", cash: "0" }]) addEntryAccount(account);
  for (const position of entryBase?.positions || [{}]) addEntryPosition(position);
  editEntry(); $("entry-dialog").showModal();
}
function editEntry() {
  reviewedEntry = null; $("entry-fields").hidden = false; $("entry-review").hidden = true;
  document.querySelector(".legacy-option").hidden = false;
  $("entry-back").hidden = true; $("entry-submit").textContent = "核对持仓";
  message("entry-error", "");
}
function collectEntry() {
  const accounts = [...$("entry-accounts").children].map((row) => ({
    account_id: row.dataset.accountId,
    name: row.querySelector('[name="name"]').value.trim(),
    cash: row.querySelector('[name="cash"]').value.trim(),
  }));
  const overrides = structuredClone(entryBase?.instrument_overrides || {});
  const classifications = new Map();
  const positions = [...$("entry-positions").children].map((row) => {
    const value = (name) => row.querySelector(`[name="${name}"]`).value.trim();
    const symbol = value("symbol").toUpperCase();
    if (!/^[A-Z][A-Z0-9.^/\-]{0,14}$/.test(symbol)) throw new Error("请填写有效的股票代码，例如 AAPL。");
    if (!/[1-9]/.test(value("quantity"))) throw new Error("持有股数必须大于零。");
    const classification = { sector: value("sector") || null, theme: value("theme") || null };
    const prior = classifications.get(symbol) || { sector: null, theme: null };
    if ((prior.sector && classification.sector && prior.sector !== classification.sector) ||
        (prior.theme && classification.theme && prior.theme !== classification.theme))
      throw new Error("同一股票在不同账户的行业与主题应保持一致。");
    classifications.set(symbol, {
      sector: prior.sector || classification.sector,
      theme: prior.theme || classification.theme,
    });
    return { position_id: `${value("account_id")}:${symbol}`, account_id: value("account_id"),
      symbol, quantity: value("quantity"), avg_cost: value("avg_cost"),
      user_stage: row._original.user_stage || "hold", note: row._original.note || "" };
  });
  if (new Set(accounts.map((row) => row.name)).size !== accounts.length) throw new Error("请为每个账户使用不同名称。");
  if (new Set(positions.map((row) => row.position_id)).size !== positions.length) throw new Error("同一账户的同一股票请合并为一行，填写总股数和平均成本。");
  for (const [symbol, classification] of classifications)
    overrides[symbol] = { ...overrides[symbol], ...classification };
  return { expected_snapshot_id: entryBase?.snapshot_id || null, accounts, positions,
    watchlist: entryBase?.watchlist || [], instrument_overrides: overrides };
}
$("manage-portfolio").addEventListener("click", openPortfolioEntry);
$("entry-close").addEventListener("click", () => $("entry-dialog").close());
$("entry-back").addEventListener("click", editEntry);
$("add-account").addEventListener("click", () => addEntryAccount());
$("add-position").addEventListener("click", () => { $("entry-search").value = ""; addEntryPosition();
  for (const row of $("entry-positions").children) row.hidden = false;
  $("entry-positions").lastElementChild.querySelector('[name="symbol"]').focus();
});
$("entry-search").addEventListener("input", () => {
  const term = $("entry-search").value.trim().toUpperCase();
  for (const row of $("entry-positions").children)
    row.hidden = !row.querySelector('[name="symbol"]').value.toUpperCase().includes(term);
});
$("entry-form").addEventListener("invalid", () => {
  $("entry-search").value = "";
  for (const row of $("entry-positions").children) row.hidden = false;
}, true);
$("import-button").addEventListener("click", () => $("entry-dialog").close());
$("entry-form").addEventListener("submit", async (event) => {
  event.preventDefault(); message("entry-error", "");
  if (!$("csv-panel").hidden) return;
  if (!reviewedEntry) {
    try { reviewedEntry = collectEntry(); } catch (error) { message("entry-error", error.message, true); return; }
    const review = $("entry-review"); review.replaceChildren(el("h3", "核对后保存"),
      el("p", `${reviewedEntry.accounts.length} 个账户 · ${reviewedEntry.positions.length} 条持仓`));
    for (const account of reviewedEntry.accounts) {
      review.append(el("h4", account.name), el("p", `现金 $${account.cash}`, "muted small"));
      for (const position of reviewedEntry.positions.filter((row) => row.account_id === account.account_id))
        review.append(el("p", `${position.symbol} · ${position.quantity} 股 · 每股成本 $${position.avg_cost}`, "review-row"));
    }
    $("entry-fields").hidden = true; review.hidden = false; $("entry-back").hidden = false;
    document.querySelector(".legacy-option").hidden = true;
    $("entry-submit").textContent = "确认保存"; $("entry-submit").focus(); return;
  }
  $("entry-submit").disabled = true; $("entry-back").disabled = true;
  try {
    await api("/api/portfolio", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reviewedEntry) });
    $("entry-dialog").close(); busy(true);
    await loadPortfolio(); clearInspection();
    $("result").replaceChildren(el("h3", "组合已更新"), el("p", "运行组合集中风险，查看新持仓的分析。", "muted"));
    $("answer-state").textContent = "等待研究";
    message("import-status", "持仓已保存。");
  } catch (error) {
    message("entry-error", error.status === 409 ? "持仓已在别处更新。请关闭窗口并刷新后重试，以免覆盖新内容。" : "未能保存。请返回检查账户、股数和每股成本；原有持仓未被替换。", true);
  } finally {
    busy(false); $("entry-submit").disabled = false; $("entry-back").disabled = false;
  }
});
