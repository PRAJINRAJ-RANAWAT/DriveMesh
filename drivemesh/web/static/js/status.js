/* Cluster status: polls /api/v1/cluster/status every 5 s. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const row = (...cells) => DM.el("tr", {}, ...cells.map((c) => DM.el("td", { text: String(c ?? "—") })));

  async function refresh() {
    let s;
    try { s = await DM.api("GET", "/api/v1/cluster/status"); } catch (_) { return; }
    const sm = $("safe-mode");
    sm.classList.toggle("hidden", !s.safe_mode.active);
    sm.textContent = s.safe_mode.active ? `Safe mode: ${s.safe_mode.reason}. Writes, repair and GC are paused.` : "";
    $("t-ok").textContent = s.replicas.ok || 0;
    $("t-under").textContent = s.under_replicated_chunks;
    $("t-unavail").textContent = s.unavailable_chunks;
    $("t-unavail").classList.toggle("bad", s.unavailable_chunks > 0);
    $("t-backup").textContent = s.last_backup_at ? DM.when(s.last_backup_at + "Z") : "never";
    const cards = $("account-cards");
    cards.replaceChildren(...(s.accounts.length ? s.accounts.map((a) => {
      const frac = a.capacity_bytes ? (a.used_bytes || 0) / a.capacity_bytes : 0;
      return DM.el("article", { class: "acct" },
        DM.el("div", { class: "acct-top" }, DM.el("div", { class: "acct-name", text: a.email || a.name }), DM.badge(a.state)),
        DM.meter(frac, frac > 0.85 ? "warn" : ""),
        DM.el("div", { class: "acct-nums" }, DM.el("span", { text: `${a.replicas} chunk copies` }), DM.el("span", { text: `${DM.size(a.free_bytes)} free` })),
        a.last_error ? DM.el("div", { class: "acct-error", text: a.last_error }) : "");
    }) : [DM.el("p", { class: "muted", text: "No accounts linked yet." })]));
    const states = Object.entries(s.replicas);
    $("replica-states").replaceChildren(...(states.length ? states.map(([k, v]) => row(k, v)) : [row("none yet", "")]));
    const jobs = Object.entries(s.jobs);
    $("jobs").replaceChildren(...(jobs.length ? jobs.map(([k, v]) => row(k, v)) : [row("none", "")]));
    $("dead-jobs").replaceChildren(...(s.dead_jobs.length ? s.dead_jobs.map((j) => row(j.kind, j.error)) : [row("none", "")]));
    $("events").replaceChildren(...s.recent_events.slice(0, 12).map(DM.activityItem));
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  }

  (async () => { if (await DM.loadMe()) { refresh(); setInterval(refresh, 5000); } })();
})();
