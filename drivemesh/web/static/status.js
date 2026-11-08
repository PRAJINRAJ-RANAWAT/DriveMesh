/* Cluster status dashboard: polls /api/v1/cluster/status every 5 s. */
"use strict";

const $ = (id) => document.getElementById(id);

function row(...cells) { return DM.el("tr", {}, ...cells.map((c) => DM.el("td", { text: String(c ?? "–") }))); }

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
  $("t-jobs").textContent = (s.jobs.queued || 0) + (s.jobs.running || 0);
  $("t-backup").textContent = s.last_backup_at ? DM.when(s.last_backup_at + "Z") : "never";

  const cards = $("account-cards");
  cards.replaceChildren();
  for (const a of s.accounts) {
    const pct = a.capacity_bytes ? Math.round(100 * (a.used_bytes || 0) / a.capacity_bytes) : 0;
    cards.append(DM.el("div", { class: "card" },
      DM.el("div", { class: "acc-head" },
        DM.el("span", { class: "badge " + a.state, text: a.state }),
        DM.el("strong", { text: a.email || a.name })),
      DM.el("meter", { min: "0", max: "100", value: String(pct) }),
      DM.el("div", { class: "small muted", text: `${a.provider} · ${a.replicas} replicas · ${DM.size(a.free_bytes)} free` }),
      DM.el("div", { class: "small muted", text: "last check: " + (a.last_health_at ? DM.when(a.last_health_at + "Z") : "never") }),
      a.last_error ? DM.el("div", { class: "small error", text: a.last_error }) : ""));
  }
  $("replica-states").replaceChildren(...Object.entries(s.replicas).map(([k, v]) => row(k, v)));
  $("dead-jobs").replaceChildren(...(s.dead_jobs.length ? s.dead_jobs.map((j) => row(j.kind, j.error)) : [row("none", "")]));
  $("events").replaceChildren(...s.recent_events.map((e) => row(DM.when(e.ts + "Z"), e.level, e.kind, e.account_id || e.file_id || "")));
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

(async () => { if (await DM.me()) { refresh(); setInterval(refresh, 5000); } })();
