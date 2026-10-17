"use strict";

let csvPreview = null;

function setEntryMode(mode) {
  const csv = mode === "csv";
  if (!csv) editEntry();
  $("entry-fields").hidden = csv;
  $("entry-review").hidden = true;
  $("entry-footer").hidden = csv;
  $("csv-panel").hidden = !csv;
  $("csv-footer").hidden = !csv;
  document.querySelector(".legacy-option").hidden = false;
  $("entry-mode-manual").setAttribute("aria-pressed", String(!csv));
  $("entry-mode-csv").setAttribute("aria-pressed", String(csv));
  message("entry-error", "");
}

function clearCsvPreview() {
  csvPreview = null;
  $("csv-preview").replaceChildren();
  $("csv-preview").hidden = true;
  $("csv-confirm").disabled = true;
}

function csvError(error) {
  const detail = error.detail || {};
  const code = detail.code || "UNAVAILABLE";
  const reasons = {
    INVALID_FILE: "文件为空或超过 1 MB，请使用 UTF-8 编码的 CSV。",
    INVALID_HEADER: "表头不符合模板。请保留 account、symbol、quantity、avg_cost 等字段；不要加入现价。",
    INVALID_ROW: "这一行的字段不完整或格式不正确。",
    INVALID_ACCOUNT: "请为每行填写不超过 80 字的账户名称。",
    INVALID_CASH_ROW: "现金请每个账户单独填写一行，同一账户只能有一条现金行。",
    INVALID_NUMBER: "股数须大于零；每股成本与现金不能为负数。",
    DUPLICATE_POSITION: "同一账户的同一股票请合并为一行，填写总股数和平均成本。",
    CONFLICTING_CLASSIFICATION: "同一股票的行业与主题需要保持一致。",
    INVALID_CLASSIFICATION: "行业或主题名称过长。",
    TOO_MANY_ACCOUNTS: "一次最多导入 50 个账户。",
    EMPTY_PORTFOLIO: "请至少提供一个账户或现金行。",
    MALFORMED_CSV: "CSV 引号或换行格式不完整。",
    PREVIEW_CHANGED: "文件内容已变化，请重新预览后确认。",
    PORTFOLIO_CHANGED: "组合已在别处更新，请刷新页面后重新预览。",
  };
  const prefix = detail.row ? `第 ${detail.row} 行：` : "";
  return prefix + (reasons[code] || "暂时无法处理，请核对模板后重试；原有组合未被替换。");
}

function showCsvPreview(data) {
  const target = $("csv-preview");
  target.replaceChildren(el("h3", "导入预览"),
    el("p", `${data.accounts.length} 个账户 · ${data.positions.length} 条持仓。确认前不会保存。`, "muted"));
  for (const account of data.accounts) {
    const count = data.positions.filter((position) => position.account_id === account.account_id).length;
    const row = el("div", null, "csv-account-preview");
    row.append(el("strong", account.name), el("span", `${count} 条持仓 · 现金 $${account.cash}`));
    target.append(row);
  }
  if (data.positions.length) {
    const details = el("details", null, "csv-position-preview");
    details.append(el("summary", `查看全部 ${data.positions.length} 条持仓`));
    const list = el("div", null, "csv-position-list");
    const accounts = new Map(data.accounts.map((account) => [account.account_id, account.name]));
    for (const position of data.positions)
      list.append(el("p", `${accounts.get(position.account_id)} · ${position.symbol} · ${position.quantity} 股 · 成本 $${position.avg_cost}`));
    details.append(list); target.append(details);
  }
  target.hidden = false;
}

async function previewCsv() {
  const source = $("csv-text").value;
  clearCsvPreview(); message("csv-error", "");
  $("csv-preview-button").disabled = true;
  try {
    const data = await api("/api/portfolio/csv/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: source }),
    });
    csvPreview = { text: source, source_hash: data.source_hash,
      expected_snapshot_id: data.expected_snapshot_id };
    showCsvPreview(data);
    $("csv-confirm").disabled = false;
  } catch (error) {
    message("csv-error", csvError(error), true);
  } finally {
    $("csv-preview-button").disabled = false;
  }
}

$("entry-mode-manual").addEventListener("click", () => setEntryMode("manual"));
$("entry-mode-csv").addEventListener("click", () => setEntryMode("csv"));
$("csv-text").addEventListener("input", clearCsvPreview);
$("csv-choose").addEventListener("click", () => $("csv-file").click());
$("csv-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  clearCsvPreview(); message("csv-error", "");
  try {
    if (file.size > 1_000_000) throw new Error("too_large");
    $("csv-text").value = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    await previewCsv();
  } catch {
    message("csv-error", "无法读取此文件；请使用不超过 1 MB 的 UTF-8 CSV。", true);
  } finally {
    event.target.value = "";
  }
});
$("csv-preview-button").addEventListener("click", previewCsv);
$("csv-confirm").addEventListener("click", async () => {
  if (!csvPreview || csvPreview.text !== $("csv-text").value) {
    clearCsvPreview(); message("csv-error", "内容已变化，请重新预览。", true); return;
  }
  $("csv-confirm").disabled = true; busy(true);
  try {
    await api("/api/portfolio/csv/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(csvPreview),
    });
    $("entry-dialog").close();
    await loadPortfolio(); clearInspection();
    $("answer-state").textContent = "等待研究";
    $("result").replaceChildren(el("h3", "组合已更新"), el("p", "现在可以研究新组合的集中风险。", "muted"));
    message("import-status", "CSV 已导入，新持仓快照已保存。");
  } catch (error) {
    message("csv-error", csvError(error), true);
  } finally {
    busy(false);
    $("csv-confirm").disabled = !csvPreview;
  }
});
$("entry-dialog").addEventListener("close", () => {
  $("csv-text").value = "";
  clearCsvPreview();
  message("csv-error", "");
});
