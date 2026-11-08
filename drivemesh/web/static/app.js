/* DriveMesh file browser. DOM is built with textContent only (no innerHTML with data). */
"use strict";

const state = { path: "/", accounts: [] };
const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(kind) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("class", kind === "folder" ? "ico folder" : "ico");
  p.setAttribute("d", kind === "folder"
    ? "M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6l1.8 2h6.6a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5z"
    : "M5 2.5h6.5L15.5 6.5v11H5zM11.5 2.5v4h4");
  svg.append(p);
  return svg;
}

function bigIcon(paths) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 72 72");
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("class", "ico");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 4500);
}

function bar(fraction, extraClass = "") {
  const outer = DM.el("div", { class: "bar " + extraClass });
  const inner = DM.el("span");
  inner.style.width = Math.max(0, Math.min(100, fraction * 100)).toFixed(1) + "%";
  outer.append(inner);
  return outer;
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function join(dir, name) { return (dir === "/" ? "" : dir) + "/" + name; }

function renderCrumbs() {
  const nav = $("crumbs");
  nav.replaceChildren();
  const parts = state.path.split("/").filter(Boolean);
  nav.append(DM.el("a", { href: "#/", text: "My mesh", onclick: (e) => { e.preventDefault(); go("/"); } }));
  parts.forEach((p, i) => {
    const target = "/" + parts.slice(0, i + 1).join("/");
    nav.append(DM.el("span", { class: "sep", text: "/" }));
    nav.append(DM.el("a", { href: "#" + target, text: p, onclick: (e) => { e.preventDefault(); go(target); } }));
  });
}

function renderEmpty(kind) {
  const box = $("empty");
  box.replaceChildren();
  if (kind === "no-accounts") {
    box.append(
      bigIcon(["M14 22h20v14H14z", "M38 22h20v14H38z", "M26 44h20v14H26z", "M24 36l8 8M48 36l-8 8"]),
      DM.el("h3", { text: "Link your first storage account" }),
      DM.el("p", { text: "DriveMesh needs at least two accounts to keep two copies of every chunk." }),
    );
  } else {
    box.append(
      bigIcon(["M12 22a4 4 0 0 1 4-4h12l5 5h23a4 4 0 0 1 4 4v23a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4z", "M36 32v14M29 39h14"]),
      DM.el("h3", { text: state.path === "/" ? "Your mesh is empty" : "This folder is empty" }),
      DM.el("p", { text: "Drop files here or use Upload. Each one is split, encrypted and replicated." }),
      DM.el("button", { class: "btn-primary btn-sm", text: "Upload a file", onclick: () => $("file-input").click() }),
    );
  }
  box.classList.remove("hidden");
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
      DM.el("td", {}, DM.el("a", { href: "#" + d.path, class: "file-name", onclick: (e) => { e.preventDefault(); go(d.path); } },
        icon("folder"), DM.el("span", { text: d.name }))),
      DM.el("td", { class: "muted", text: "—" }),
      DM.el("td", { class: "col-prot" }),
      DM.el("td", { class: "col-mod" }),
      DM.el("td", { class: "actions" },
        DM.el("button", { class: "btn-ghost btn-sm", text: "Rename", onclick: () => rename(d.path, d.name) }),
        DM.el("button", { class: "btn-ghost btn-sm btn-danger", text: "Delete", onclick: () => removeFolder(d.path) }))));
  }
  for (const f of data.files) {
    const protection = f.scheme === "ec" ? `RS(${f.ec[0]},${f.ec[1]})` : `${f.rf} copies`;
    rows.append(DM.el("tr", {},
      DM.el("td", {}, DM.el("a", { class: "file-name", download: f.name, title: "Download",
        href: "/api/v1/files/content?path=" + encodeURIComponent(f.path) }, icon("file"), DM.el("span", { text: f.name }))),
      DM.el("td", { text: DM.size(f.size) }),
      DM.el("td", { class: "col-prot" }, DM.el("span", { class: "pill", text: `${protection} · ${plural(f.chunks, "chunk")}` })),
      DM.el("td", { class: "col-mod muted", text: DM.when(f.committed_at) }),
      DM.el("td", { class: "actions" },
        DM.el("button", { class: "btn-ghost btn-sm", text: "Rename", onclick: () => rename(f.path, f.name) }),
        DM.el("button", { class: "btn-ghost btn-sm btn-danger", text: "Delete", onclick: () => removeFile(f.path) }))));
  }
  const empty = data.folders.length + data.files.length === 0;
  $("file-table").classList.toggle("hidden", empty);
  if (!state.accounts.length) renderEmpty("no-accounts");
  else if (empty) renderEmpty("folder");
  else $("empty").classList.add("hidden");
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
  if (!confirm(`Delete ${path}? Its copies are purged from every account in the background.`)) return;
  try { await DM.api("DELETE", "/api/v1/files?path=" + encodeURIComponent(path)); toast("Deleted"); refresh(); }
  catch (err) { toast(err.message, true); }
}

async function removeFolder(path) {
  if (!confirm(`Delete folder ${path} and everything in it?`)) return;
  try {
    const r = await DM.api("DELETE", "/api/v1/folders?recursive=true&path=" + encodeURIComponent(path));
    toast(`Deleted folder (${r.files_deleted} files)`); refresh();
  } catch (err) { toast(err.message, true); }
}

function upload(file, overwrite = false) {
  const name = DM.el("span", { class: "upload-name", text: file.name });
  const status = DM.el("span", { class: "upload-state", text: "Starting…" });
  const progress = bar(0, "bar-accent");
  const card = DM.el("div", { class: "upload" }, DM.el("div", { class: "upload-head" }, name, status), progress);
  $("uploads").append(card);
  const fill = progress.firstChild;
  const xhr = new XMLHttpRequest();
  const target = join(state.path, file.name);
  xhr.open("PUT", "/api/v1/files/content?path=" + encodeURIComponent(target) + (overwrite ? "&overwrite=true" : ""));
  xhr.setRequestHeader("X-CSRF-Token", DM.csrf);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = (100 * e.loaded) / e.total;
    fill.style.width = pct.toFixed(1) + "%";
    status.textContent = pct >= 100 ? "Encrypting & replicating…" : `${Math.floor(pct)}% · ${DM.size(e.loaded)} of ${DM.size(e.total)}`;
  };
  xhr.onload = () => {
    if (xhr.status === 201) {
      const info = JSON.parse(xhr.responseText);
      card.classList.add("done");
      fill.style.width = "100%";
      status.textContent = `Stored · ${plural(info.chunks, "chunk")} × ${info.rf} copies`;
      setTimeout(() => card.remove(), 4000);
      refresh();
    } else {
      let msg = xhr.statusText;
      let code = "";
      try { const err = JSON.parse(xhr.responseText).error; msg = err.message; code = err.code; } catch (_) { /* keep status */ }
      card.classList.add("failed");
      status.textContent = code === "already_exists" ? "A file with this name already exists" : msg;
      if (code === "already_exists") {
        status.append(" · ", DM.el("button", { class: "btn-ghost btn-sm", text: "Replace",
          onclick: () => { card.remove(); upload(file, true); } }));
      }
    }
  };
  xhr.onerror = () => { card.classList.add("failed"); status.textContent = "Network error"; };
  xhr.send(file); // raw body: streamed through the server's chunk pipeline, no multipart buffering
}

async function loadAccounts(me) {
  const list = $("accounts");
  list.replaceChildren();
  let data;
  try { data = await DM.api("GET", "/api/v1/accounts"); } catch (err) { toast(err.message, true); return; }
  state.accounts = data.accounts;
  $("acc-count").textContent = String(data.accounts.length);
  if (!data.accounts.length) {
    list.append(DM.el("li", { class: "muted small", text: "No accounts linked yet. Link at least two to get replication." }));
  }
  for (const a of data.accounts) {
    const frac = a.capacity_bytes ? (a.used_bytes || 0) / a.capacity_bytes : 0;
    list.append(DM.el("li", { class: "account" },
      DM.el("div", { class: "acc-head" },
        DM.el("span", { class: "acc-name", text: a.email || a.name, title: a.email || a.name }),
        DM.el("span", { class: "badge " + a.state, text: a.state }),
        DM.el("button", { class: "btn-ghost btn-sm btn-danger", text: "Unlink", "aria-label": `Unlink ${a.email || a.name}`, onclick: () => unlink(a) })),
      bar(frac, frac > 0.85 ? "high" : ""),
      DM.el("div", { class: "acc-meta",
        text: `${a.provider === "gdrive" ? "Google Drive" : a.provider} · ${DM.size(a.used_bytes)} of ${DM.size(a.capacity_bytes)}` +
              (a.last_error ? ` · ${a.last_error}` : "") })));
  }
  $("link-google").classList.toggle("hidden", !me.google_configured);
  $("local-box").classList.toggle("hidden", !me.allow_local_backends);
}

async function loadSummary() {
  let s;
  try { s = await DM.api("GET", "/api/v1/cluster/status"); } catch (_) { return; }
  const healthy = s.accounts.filter((a) => ["active", "full", "draining"].includes(a.state)).length;
  $("s-accounts").textContent = `${healthy} / ${s.accounts.length}`;
  $("s-used").textContent = DM.size(s.stored_bytes || 0);
  $("s-replicas").textContent = String(s.replicas.ok || 0);
  $("s-under").textContent = String(s.under_replicated_chunks);
  $("s-under").classList.toggle("bad", s.under_replicated_chunks > 0);
}

async function unlink(a) {
  if (!confirm(`Unlink ${a.email || a.name}? Its data is first copied to your other accounts.`)) return;
  try { await DM.api("DELETE", "/api/v1/accounts/" + a.id); toast("Account is draining: copies are moving to your other accounts"); refresh(); }
  catch (err) { toast(err.message, true); }
}

let me = null;
async function refresh() {
  await loadAccounts(me);
  await Promise.all([load(), loadSummary()]);
}

async function init() {
  me = await DM.me();
  if (!me) return;
  $("who").textContent = me.user.username;
  if (location.hash.length > 1) state.path = decodeURIComponent(location.hash.slice(1));
  $("logout").addEventListener("click", async () => { await DM.api("POST", "/api/v1/auth/logout"); location.href = "/"; });
  $("new-folder").addEventListener("click", async () => {
    const name = prompt("Folder name");
    if (!name) return;
    try { await DM.api("POST", "/api/v1/folders", { path: join(state.path, name) }); load(); }
    catch (err) { toast(err.message, true); }
  });
  $("upload-btn").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { for (const f of e.target.files) upload(f); e.target.value = ""; });
  const main = document.querySelector(".layout > section");
  const dz = $("dropzone");
  main.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  main.addEventListener("dragleave", (e) => { if (!main.contains(e.relatedTarget)) dz.classList.remove("over"); });
  main.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); for (const f of e.dataTransfer.files) upload(f); });
  $("local-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await DM.api("POST", "/api/v1/accounts/local", { path: f.path.value, name: f.name.value || null }); f.reset(); refresh(); }
    catch (err) { toast(err.message, true); }
  });
  if (new URLSearchParams(location.search).get("linked")) toast("Google account linked");
  refresh();
}

init();
