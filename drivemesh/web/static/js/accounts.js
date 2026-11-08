/* Linked accounts: health, quota bars, linking, unlink with an evacuation warning. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const dialog = $("unlink-dialog");
  let pending = null;

  function card(a) {
    const frac = a.capacity_bytes ? (a.used_bytes || 0) / a.capacity_bytes : 0;
    const provider = a.provider === "gdrive" ? "Google Drive" : a.provider === "local" ? "Local folder" : a.provider;
    return DM.el("article", { class: "acct" },
      DM.el("div", { class: "acct-top" },
        DM.el("div", {}, DM.el("div", { class: "acct-provider", text: provider }), DM.el("div", { class: "acct-name", text: a.email || a.name })),
        DM.badge(a.state)),
      DM.meter(frac, frac > 0.85 ? "warn" : ""),
      DM.el("div", { class: "acct-nums" },
        DM.el("span", { text: `${DM.size(a.used_bytes)} used` }),
        DM.el("span", { text: a.capacity_bytes ? `${DM.size(a.free_bytes)} free of ${DM.size(a.capacity_bytes)}` : "capacity unknown" })),
      DM.el("div", { class: "small muted", text: "Last health check: " + (a.last_health_at ? DM.when(a.last_health_at) : "never") }),
      a.last_error ? DM.el("div", { class: "acct-error", text: a.last_error }) : "",
      DM.el("div", { class: "acct-actions" },
        DM.el("button", { class: "btn btn-ghost btn-sm btn-danger", text: "Unlink", "aria-label": `Unlink ${a.email || a.name}`,
          onclick: () => { pending = a; $("unlink-name").textContent = a.email || a.name; dialog.showModal(); } })));
  }

  async function load() {
    let data;
    try { data = await DM.api("GET", "/api/v1/accounts"); } catch (err) { DM.toast(err.message, true); return; }
    const box = $("account-cards");
    box.replaceChildren(...data.accounts.map(card));
    if (!data.accounts.length) {
      box.append(DM.el("div", { class: "card empty" },
        DM.svg("0 0 72 72", ["M14 22h20v14H14z", "M38 22h20v14H38z", "M26 44h20v14H26z", "M24 36l8 8M48 36l-8 8"]),
        DM.el("h3", { text: "No accounts yet" }),
        DM.el("p", { text: "Link two or more accounts to start. DriveMesh keeps two copies of every chunk on different accounts." })));
    }
  }

  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "confirm" || !pending) return;
    const a = pending;
    pending = null;
    try {
      await DM.api("DELETE", "/api/v1/accounts/" + a.id);
      DM.toast(`${a.email || a.name} is evacuating: its chunks are being copied to your other accounts.`);
      load();
    } catch (err) { DM.toast(err.message, true); }
  });

  (async () => {
    const me = await DM.loadMe();
    if (!me) return;
    $("link-google").classList.toggle("hidden", !me.google_configured);
    $("google-off").classList.toggle("hidden", me.google_configured);
    $("local-form").classList.toggle("hidden", !me.allow_local_backends);
    $("local-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try { await DM.api("POST", "/api/v1/accounts/local", { path: f.path.value, name: f.name.value || null }); f.reset(); DM.toast("Folder linked"); load(); }
      catch (err) { DM.toast(err.message, true); }
    });
    if (new URLSearchParams(location.search).get("linked")) DM.toast("Google account linked");
    load();
  })();
})();
