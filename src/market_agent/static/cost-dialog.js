"use strict";
// In-app replacement for window.confirm on actions that may incur model cost.
// Resolves true only on an explicit click of the confirm button; Esc, backdrop,
// the cancel button and a second concurrent request all resolve false, so the
// caller never sends its POST unless the person confirmed this exact action.
(() => {
  let pending = null;
  function build() {
    const dialog = document.createElement("dialog");
    dialog.id = "mia-cost-dialog";
    dialog.className = "mia-cost-dialog";
    dialog.setAttribute("aria-labelledby", "mia-cost-title");
    dialog.setAttribute("aria-describedby", "mia-cost-body mia-cost-fee");
    dialog.innerHTML =
      '<h2 id="mia-cost-title"></h2>' +
      '<p id="mia-cost-body"></p>' +
      '<dl id="mia-cost-facts" class="mia-cost-facts" hidden></dl>' +
      '<p id="mia-cost-fee" class="mia-cost-fee">可能产生模型费用。取消不会提交任何请求。</p>' +
      '<div class="mia-cost-actions"><button type="button" data-cost="cancel">取消</button>' +
      '<button type="button" class="primary" data-cost="confirm"></button></div>';
    document.body.append(dialog);
    const finish = value => {
      if (!pending) return;
      const { resolve, invoker } = pending;
      pending = null;
      if (dialog.open) dialog.close();
      if (invoker?.isConnected) invoker.focus({ preventScroll: true });
      resolve(value);
    };
    dialog.querySelector('[data-cost="cancel"]').addEventListener("click", () => finish(false));
    dialog.querySelector('[data-cost="confirm"]').addEventListener("click", () => finish(true));
    // Esc fires "cancel"; any other close path also counts as not confirmed.
    dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
    dialog.addEventListener("close", () => finish(false));
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      // Clicks on the dialog's own padding also target it; only the backdrop cancels.
      const box = dialog.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) finish(false);
    });
    return dialog;
  }
  window.miaConfirmCost = ({ title, body, confirmLabel = "确认", facts = [] }) => {
    if (pending) return Promise.resolve(false);
    const dialog = document.getElementById("mia-cost-dialog") || build();
    dialog.querySelector("#mia-cost-title").textContent = title;
    dialog.querySelector("#mia-cost-body").textContent = body;
    dialog.querySelector('[data-cost="confirm"]').textContent = confirmLabel;
    const list = dialog.querySelector("#mia-cost-facts");
    list.replaceChildren();
    for (const [label, value] of facts) {
      const row = document.createElement("div");
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label; dd.textContent = value;
      row.append(dt, dd); list.append(row);
    }
    list.hidden = !facts.length;
    return new Promise(resolve => {
      pending = { resolve, invoker: document.activeElement };
      dialog.showModal();
      // Cancel is the default focus: Enter alone never confirms a paid action.
      dialog.querySelector('[data-cost="cancel"]').focus();
    });
  };
})();
