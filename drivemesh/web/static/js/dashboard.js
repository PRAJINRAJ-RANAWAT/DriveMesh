/* Overview: pooled capacity, per-account usage, replica health, repair queue, activity. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  async function refresh() {
    let s;
    try { s = await DM.api("GET", "/api/v1/cluster/status"); } catch (err) { DM.toast(err.message, true); return; }
    const sm = $("safe-mode");
    sm.classList.toggle("hidden", !s.safe_mode.active);
    sm.textContent = s.safe_mode.active ? `Safe mode: ${s.safe_mode.reason}. Uploads and repairs are paused until accounts respond.` : "";

    const cap = s.capacity;
    $("k-capacity").textContent = DM.size(cap.total_bytes);
    $("k-capacity-bar").style.width = cap.total_bytes ? ((100 * cap.used_bytes) / cap.total_bytes).toFixed(1) + "%" : "0%";
    const local = s.accounts.some((a) => a.provider === "local");
    $("k-capacity-sub").textContent = `${DM.size(cap.used_bytes)} used · ${DM.size(cap.free_bytes)} free · ${DM.plural(s.accounts.length, "account")}` +
      (local ? " · local folders on one disk are counted once" : "");
    $("k-stored").textContent = DM.size(s.stored_bytes);

    const ok = s.replicas.ok || 0, pending = s.replicas.pending || 0, under = s.under_replicated_chunks, bad = s.unavailable_chunks;
    $("k-healthy").textContent = bad ? "Degraded" : under ? "Healing" : "Healthy";
    $("k-healthy").classList.toggle("bad", bad > 0);
    $("k-health-sub").textContent = `${ok} healthy ${ok === 1 ? "copy" : "copies"}${pending ? ` · ${pending} being written` : ""}`;
    const queued = (s.jobs.queued || 0) + (s.jobs.running || 0);
    $("k-queue").textContent = String(under + queued);
    $("k-queue-sub").textContent = `${DM.plural(under, "chunk")} to repair · ${DM.plural(queued, "job")} queued`;

    const total = ok + pending + under || 1;
    $("hb-ok").style.width = ((100 * ok) / total).toFixed(1) + "%";
    $("hb-rep").style.width = ((100 * pending) / total).toFixed(1) + "%";
    $("hb-under").style.width = ((100 * under) / total).toFixed(1) + "%";
    $("h-ok").textContent = ok; $("h-rep").textContent = pending; $("h-under").textContent = under; $("h-bad").textContent = bad;

    const list = $("usage");
    list.replaceChildren();
    $("usage-empty").classList.toggle("hidden", s.accounts.length > 0);
    for (const a of s.accounts) {
      const frac = a.capacity_bytes ? (a.used_bytes || 0) / a.capacity_bytes : 0;
      list.append(DM.el("li", { class: "usage-row" },
        DM.el("div", { class: "usage-top" },
          DM.el("span", { class: "usage-name", text: a.email || a.name, title: a.email || a.name }),
          DM.badge(a.state)),
        DM.meter(frac, frac > 0.85 ? "warn" : ""),
        DM.el("div", { class: "usage-top" },
          DM.el("span", { class: "usage-nums", text: `${DM.size(a.used_bytes)} used` }),
          DM.el("span", { class: "usage-nums", text: `${DM.size(a.free_bytes)} free` }))));
    }
    const act = $("activity");
    act.replaceChildren(...(s.recent_events.length ? s.recent_events.slice(0, 10).map(DM.activityItem)
      : [DM.el("li", {}, DM.el("span"), DM.el("span", { class: "muted", text: "Nothing yet. Upload a file to get started." }))]));
    $("updated").textContent = "live · " + new Date().toLocaleTimeString();
  }

  (async () => { if (await DM.loadMe()) { refresh(); setInterval(refresh, 10000); } })();
})();
