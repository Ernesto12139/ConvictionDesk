"use strict";
// Loaded synchronously before stylesheets (CSP: same-origin script only) so the
// first paint already uses the chosen theme. Preference is auto | dark | light;
// the rendered attribute is only dark | light. Never touches business requests.
(() => {
  const KEY = "mia.theme.v1";
  const choices = ["auto", "dark", "light"];
  const system = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  let choice = "auto";
  try { choice = localStorage.getItem(KEY) || "auto"; } catch {}
  if (!choices.includes(choice)) choice = "auto";
  function apply() {
    const theme = choice === "auto" ? (system?.matches ? "dark" : "light") : choice;
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.dataset.themePreference = choice;
    root.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#101419" : "#f6f7f4");
  }
  apply();
  system?.addEventListener?.("change", () => { if (choice === "auto") apply(); });
  document.addEventListener("DOMContentLoaded", () => {
    // Re-apply once the meta tag exists; select(s) mirror the stored preference.
    apply();
    for (const select of document.querySelectorAll("[data-theme-control]")) {
      select.value = choice;
      select.addEventListener("change", () => {
        choice = choices.includes(select.value) ? select.value : "auto";
        try { localStorage.setItem(KEY, choice); } catch {}
        document.querySelectorAll("[data-theme-control]").forEach(other => { other.value = choice; });
        apply();
      });
    }
  });
})();
