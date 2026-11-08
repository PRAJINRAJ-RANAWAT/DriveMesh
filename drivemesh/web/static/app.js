/* DriveMesh file browser. */
"use strict";

const state = { path: "/" };
const $ = (id) => document.getElementById(id);

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

function join(dir, name) { return (dir === "/" ? "" : dir) + "/" + name; }

function renderCrumbs() {
  const nav = $("crumbs");
  nav.replaceChildren();
  const parts = state.path.split("/").filter(Boolean);
  nav.append(DM.el("a", { href: "#", text: "Home", onclick: (e) => { e.preventDefault(); go("/"); } }));
  parts.forEach((p, i) => {
    const target = "/" + parts.slice(0, i + 1).join("/");
    nav.append(DM.el("span", { class: "sep", text: "/" }));
    nav.append(DM.el("a", { href: "#", text: p, onclick: (e) => { e.preventDefault(); go(target); } }));
  });
}

async function load() {
  renderCrumbs();
  let data;
  try { data = await DM.api("GET", "/api/v1/fs?path=" + encodeURIComponent(state.path)); }
  catch (err) { toast(err.message, true); return; }
  const rows = $("rows");
  rows.replaceChildren();
  for (const d of data.folders) {
    rows.append(DM.el("tr", {},
      DM.el("td", {}, DM.el("a", { href: "#", class: "folder", text: d.name + "/",
        onclick: (e) => { e.preventDefault(); go(d.path); } })),
      DM.el("td", { text: "–" }), DM.el("td", { text: "" }), DM.el("td", { text: "" }),
      DM.el("td", { class: "actions" },
        DM.el("button", { class: "ghost", text: "Rename", onclick: () => rename(d.path, d.name) }),
        DM.el("button", { class: "ghost danger", text: "Delete", onclick: () => removeFolder(d.path) }))));
  }
  for (const f of data.files) {
    const protection = f.scheme === "ec" ? `RS(${f.ec[0]},${f.ec[1]})` : `${f.rf}× replicas`;
    const dl = DM.el("a", { href: "/api/v1/files/content?path=" + encodeURIComponent(f.path),
      text: f.name, download: f.name });
    rows.append(DM.el("tr", {},
      DM.el("td", {}, dl),
      DM.el("td", { text: DM.size(f.size) }),
      DM.el("td", { text: `${protection} · ${f.chunks} chunks · v${f.version}` }),
      DM.el("td", { text: DM.when(f.committed_at) }),
      DM.el("td", { class: "actions" },
        DM.el("button", { class: "ghost", text: "Rename", onclick: () => rename(f.path, f.name) }),
        DM.el("button", { class: "ghost danger", text: "Delete", onclick: () => removeFile(f.path) }))));
  }
  $("empty").classList.toggle("hidden", data.folders.length + data.files.length > 0);
}

function go(path) { state.path = path; history.replaceState(null, "", "#" + path); load(); }

async function rename(path, name) {
  const next = prompt("New name", name);
  if (!next || next === name) return;
  const dst = path.slice(0, path.lastIndexOf("/") + 1) + next;
  try { await DM.api("POST", "/api/v1/fs/move", { src: path, dst }); load(); }
  catch (err) { toast(err.message, true); }
}

async function removeFile(path) {
  if (!confirm(`Delete ${path}? Replicas are purged in the background.`)) return;
  try { await DM.api("DELETE", "/api/v1/files?path=" + encodeURIComponent(path)); toast("Deleted"); load(); }
  catch (err) { toast(err.message, true); }
}

async function removeFolder(path) {
  if (!confirm(`Delete folder ${path} and everything in it?`)) return;
  try {
    const r = await DM.api("DELETE", "/api/v1/folders?recursive=true&path=" + encodeURIComponent(path));
    toast(`Deleted folder (${r.files_deleted} files)`); load();
  } catch (err) { toast(err.message, true); }
}

function upload(file) {
  const bar = DM.el("progress", { max: "100", value: "0" });
  const label = DM.el("span", { text: file.name });
  const row = DM.el("div", { class: "upload" }, label, bar);
  $("uploads").append(row);
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/api/v1/files/content?path=" + encodeURIComponent(join(state.path, file.name)));
  xhr.setRequestHeader("X-CSRF-Token", DM.csrf);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = Math.round(100 * e.loaded / e.total); };
  xhr.onload = () => {
    if (xhr.status === 201) { label.textContent = file.name + " ✓"; setTimeout(() => row.remove(), 2500); load(); }
    else {
      let msg = xhr.statusText;
      try { msg = JSON.parse(xhr.responseText).error.message; } catch (_) { /* keep status */ }
      label.textContent = `${file.name}: ${msg}`; row.classList.add("error");
    }
  };
  xhr.onerror = () => { label.textContent = file.name + ": network error"; row.classList.add("error"); };
  xhr.send(file); // raw body: streamed through the server's chunk pipeline, no multipart buffering
}

async function loadAccounts(me) {
  const list = $("accounts");
  list.replaceChildren();
  let data;
  try { data = await DM.api("GET", "/api/v1/accounts"); } catch (err) { toast(err.message, true); return; }
  if (!data.accounts.length) list.append(DM.el("li", { class: "muted", text: "No accounts linked yet." }));
  for (const a of data.accounts) {
    const pct = a.capacity_bytes ? Math.round(100 * (a.used_bytes || 0) / a.capacity_bytes) : null;
    const meter = DM.el("meter", { min: "0", max: "100", value: String(pct ?? 0) });
    list.append(DM.el("li", {},
      DM.el("div", { class: "acc-head" },
        DM.el("span", { class: "badge " + a.state, text: a.state }),
        DM.el("strong", { text: a.email || a.name }),
        DM.el("button", { class: "ghost danger small", text: "Unlink", onclick: () => unlink(a) })),
      meter,
      DM.el("div", { class: "muted small",
        text: `${a.provider} · ${DM.size(a.used_bytes)} used of ${DM.size(a.capacity_bytes)}` +
              (a.last_error ? ` · ${a.last_error}` : "") })));
  }
  $("link-google").classList.toggle("hidden", !me.google_configured);
  $("local-box").classList.toggle("hidden", !me.allow_local_backends);
}

async function unlink(a) {
  if (!confirm(`Unlink ${a.email || a.name}? Its data is first copied to other accounts.`)) return;
  try { await DM.api("DELETE", "/api/v1/accounts/" + a.id); toast("Account is draining"); loadAccounts(await DM.me()); }
  catch (err) { toast(err.message, true); }
}

async function init() {
  const me = await DM.me();
  if (!me) return;
  $("who").textContent = me.user.username;
  if (location.hash.length > 1) state.path = decodeURIComponent(location.hash.slice(1));
  $("logout").addEventListener("click", async () => { await DM.api("POST", "/api/v1/auth/logout"); location.href = "/login"; });
  $("new-folder").addEventListener("click", async () => {
    const name = prompt("Folder name");
    if (!name) return;
    try { await DM.api("POST", "/api/v1/folders", { path: join(state.path, name) }); load(); }
    catch (err) { toast(err.message, true); }
  });
  $("file-input").addEventListener("change", (e) => { for (const f of e.target.files) upload(f); e.target.value = ""; });
  const dz = $("dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); for (const f of e.dataTransfer.files) upload(f); });
  $("local-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await DM.api("POST", "/api/v1/accounts/local", { path: f.path.value, name: f.name.value || null }); f.reset(); loadAccounts(me); }
    catch (err) { toast(err.message, true); }
  });
  if (new URLSearchParams(location.search).get("linked")) toast("Google account linked");
  load();
  loadAccounts(me);
}

init();
