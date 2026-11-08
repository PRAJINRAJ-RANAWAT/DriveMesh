/* Cluster status dashboard: polls /api/v1/cluster/status every 5 s. */
"use strict";

const $ = (id) => document.getElementById(id);

function row(...cells) {
  return DM.el("tr", {}, ...cells.map((c) => DM.el("td", { text: String(c ?? "—") })));
}

function bar(fraction) {
  const outer = DM.el("div", { class: "bar" + (fraction > 0.85 ? " high" : "") });
  const inner = DM.el("span");
  inner.style.width = (Math.min(1, fraction) * 100).toFixed(1) + "%";
  outer.append(inner);
  return outer;
}

async function refresh() {
  let s;
  try { s = await DM.api("GET", "/api/v1/cluster/status"); } catch (_) { return; }
  const sm = $("safe-mode");
  sm.classList.toggle("hidden", !s.safe_mode.active);
  sm.textContent = s.safe_mode.active ? `Safe mode: ${s.safe_mode.reason}. Writes, repair and GC are paused.` : "";
  $("t-ok").textContent = s.replicas.ok || 0;
  $("t-under").textContent = s.under_replicated_chunks;
  $("t-under").classList.toggle("bad", s.under_replicated_chunks > 0);
  $("t-unavail").textContent = s.unavailable_chunks;
  $("t-unavail").classList.toggle("bad", s.unavailable_chunks > 0);
  $("t-backup").textContent = s.last_backup_at ? DM.when(s.last_backup_at + "Z") : "never";

  const cards = $("account-cards");
  cards.replaceChildren();
  if (!s.accounts.length) cards.append(DM.el("p", { class: "muted", text: "No accounts linked yet." }));
  for (const a of s.accounts) {
    const frac = a.capacity_bytes ? (a.used_bytes || 0) / a.capacity_bytes : 0;
    cards.append(DM.el("div", { class: "account" },
      DM.el("div", { class: "acc-head" },
        DM.el("span", { class: "acc-name", text: a.email || a.name }),
        DM.el("span", { class: "badge " + a.state, text: a.state })),
      bar(frac),
      DM.el("div", { class: "acc-meta", text: `${a.provider} · ${a.replicas} chunk copies · ${DM.size(a.free_bytes)} free` }),
      DM.el("div", { class: "acc-meta", text: "last check: " + (a.last_health_at ? DM.when(a.last_health_at + "Z") : "never") }),
      a.last_error ? DM.el("div", { class: "acc-meta error", text: a.last_error }) : ""));
  }
  const states = Object.entries(s.replicas);
  $("replica-states").replaceChildren(...(states.length ? states.map(([k, v]) => row(k, v)) : [row("none yet", "")]));
  const jobs = Object.entries(s.jobs);
  $("jobs").replaceChildren(...(jobs.length ? jobs.map(([k, v]) => row(k, v)) : [row("none", "")]));
  $("dead-jobs").replaceChildren(...(s.dead_jobs.length ? s.dead_jobs.map((j) => row(j.kind, j.error)) : [row("none", "")]));
  $("events").replaceChildren(...s.recent_events.map((e) => row(DM.when(e.ts + "Z"), e.level, e.kind)));
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

(async () => { if (await DM.me()) { refresh(); setInterval(refresh, 5000); } })();
