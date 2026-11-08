/* Theme: runs in <head> (external file: the CSP forbids inline scripts) so the right theme is
   applied before first paint. Preference: saved choice, else the OS setting. */
"use strict";
(() => {
  const root = document.documentElement;
  let saved = null;
  try { saved = localStorage.getItem("dm-theme"); } catch (_) { /* storage blocked */ }
  const system = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  root.dataset.theme = saved === "dark" || saved === "light" ? saved : system;

  function sync() {
    const dark = root.dataset.theme === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.setAttribute("aria-pressed", String(dark));
      b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      b.title = dark ? "Light theme" : "Dark theme";
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#1A1A1A" : "#F5F3EE");
  }
  document.addEventListener("DOMContentLoaded", () => {
    sync();
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("dm-theme", root.dataset.theme); } catch (_) { /* ignore */ }
      sync();
    }));
  });
})();
