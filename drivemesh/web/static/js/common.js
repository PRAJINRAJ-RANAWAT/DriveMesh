/* Shared helpers for the signed-in app. DOM is built with textContent / createElement only. */
"use strict";

const DM = {
  csrf: null,
  me: null,
  SVG: "http://www.w3.org/2000/svg",

  el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c !== null && c !== undefined && c !== "") node.append(c);
    return node;
  },

  svg(viewBox, paths, cls = "") {
    const s = document.createElementNS(DM.SVG, "svg");
    s.setAttribute("viewBox", viewBox);
    s.setAttribute("aria-hidden", "true");
    if (cls) s.setAttribute("class", cls);
    for (const d of paths) {
      const p = document.createElementNS(DM.SVG, "path");
      p.setAttribute("d", d);
      s.append(p);
    }
    return s;
  },

  async loadMe() {
    const res = await fetch("/api/v1/auth/me");
    if (res.status === 401) { location.href = "/login"; return null; }
    const data = await res.json();
    DM.csrf = data.csrf_token;
    DM.me = data;
    const who = document.getElementById("who");
    if (who) who.textContent = data.user.username;
    const logout = document.getElementById("logout");
    if (logout) logout.addEventListener("click", async () => { await DM.api("POST", "/api/v1/auth/logout"); location.href = "/"; });
    return data;
  },

  async api(method, url, body) {
    const headers = { "X-CSRF-Token": DM.csrf || "" };
    let payload;
    if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    const res = await fetch(url, { method, headers, body: payload });
    if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
    const text = await res.text();
    const data = text ? JSON.parse(text) : {};
    if (!res.ok) {
      const err = new Error((data.error && data.error.message) || res.statusText);
      err.code = data.error && data.error.code;
      throw err;
    }
    return data;
  },

  size(n) {
    if (n === null || n === undefined) return "–";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + " " + units[i];
  },

  when(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  },

  plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; },

  meter(fraction, cls = "") {
    const outer = DM.el("div", { class: "meter " + cls, role: "presentation" });
    const inner = DM.el("span");
    inner.style.width = (Math.max(0, Math.min(1, fraction || 0)) * 100).toFixed(1) + "%";
    outer.append(inner);
    return outer;
  },

  badge(state, text) { return DM.el("span", { class: "badge " + state, text: text || state.replace("_", "-") }); },

  /* Human wording for audit events. */
  EVENTS: {
    login: ["Signed in", ""], file_committed: ["File stored", "good"], file_deleted: ["File deleted", ""],
    file_purged: ["Old copies purged", ""], folder_deleted: ["Folder deleted", ""],
    account_linked: ["Account linked", "good"], account_relinked: ["Account re-linked", "good"],
    account_state: ["Account health changed", "warning"], account_draining: ["Account evacuating", "warning"],
    account_removed: ["Account removed", "warning"], replica_missing: ["Missing copy detected", "warning"],
    replica_corrupt: ["Corrupt copy detected", "warning"], repair_pass: ["Repair pass", "good"],
    chunks_unavailable: ["Chunks unavailable", "critical"], metadata_backup: ["Metadata backed up", "good"],
    metadata_restored: ["Metadata restored", "warning"], safe_mode_left: ["Left safe mode", "good"],
    safe_mode_entered: ["Entered safe mode", "critical"], upload_aborted: ["Upload aborted", "warning"],
    upload_abandoned: ["Abandoned upload cleaned", "warning"], orphans_swept: ["Orphans cleaned", ""],
    job_dead: ["Job failed permanently", "critical"], balancer_pass: ["Rebalanced", "good"],
    keys_rewrapped: ["Keys rotated", "good"], token_revoked: ["Token revoked", ""],
  },

  activityItem(e) {
    const [label, tone] = DM.EVENTS[e.kind] || [e.kind.replaceAll("_", " "), ""];
    const level = e.level === "critical" || e.level === "error" ? e.level : e.level === "warning" ? "warning" : tone;
    return DM.el("li", {}, DM.el("span", { class: "dot " + (level || "") }), DM.el("span", { text: label }),
      DM.el("time", { datetime: e.ts, text: DM.when(e.ts + "Z") }));
  },

  toast(msg, isError = false) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.remove("hidden");
    clearTimeout(DM._t);
    DM._t = setTimeout(() => t.classList.add("hidden"), 5000);
  },
};
