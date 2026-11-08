/* File browser: folder tree, file list with replica health, drag-and-drop uploads with
   per-file progress and per-chunk status. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const state = { path: params.get("path") || "/" };
  const FOLDER = ["M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6l1.8 2h6.6a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5z"];
  const FILE = ["M5 2.5h6.5L15.5 6.5v11H5zM11.5 2.5v4h4"];
  const HEALTH = { healthy: "2 copies", repairing: "repairing", under_replicated: "1 copy", unavailable: "unavailable" };

  const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;

  /* ---- folder tree (lazy) ---- */
  function treeItem(name, path) {
    const kids = DM.el("ul", { class: "hidden" });
    const toggle = DM.el("button", { class: "tree-toggle", "aria-expanded": "false", "aria-label": `Show folders inside ${name}`, text: "›" });
    const link = DM.el("a", { class: "tree-link", href: "/files?path=" + encodeURIComponent(path), text: name,
      "aria-current": path === state.path ? "true" : undefined,
      onclick: (e) => { e.preventDefault(); go(path); } });
    toggle.addEventListener("click", async () => {
      const open = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!open));
      kids.classList.toggle("hidden", open);
      if (!open && !kids.dataset.loaded) {
        kids.dataset.loaded = "1";
        try {
          const data = await DM.api("GET", "/api/v1/fs?path=" + encodeURIComponent(path));
          kids.replaceChildren(...data.folders.map((d) => treeItem(d.name, d.path)));
          if (!data.folders.length) kids.append(DM.el("li", { class: "small muted", text: "no subfolders" }));
        } catch (err) { DM.toast(err.message, true); }
      }
    });
    return DM.el("li", { "data-path": path }, DM.el("div", { class: "tree-row" }, toggle, link), kids);
  }
  function renderTree() {
    const root = treeItem("My mesh", "/");
    $("tree").replaceChildren(root);
    root.querySelector(".tree-toggle").click();
  }
  function markTree() {
    document.querySelectorAll(".tree-link").forEach((a) => {
      const li = a.closest("li");
      if (li && li.dataset.path === state.path) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    });
  }

  function renderCrumbs() {
    const nav = $("crumbs");
    nav.replaceChildren();
    const parts = state.path.split("/").filter(Boolean);
    nav.append(DM.el("a", { href: "/files", text: "My mesh", onclick: (e) => { e.preventDefault(); go("/"); } }));
    parts.forEach((p, i) => {
      const target = "/" + parts.slice(0, i + 1).join("/");
      nav.append(DM.el("span", { class: "sep", text: "/", "aria-hidden": "true" }),
        DM.el("a", { href: "/files?path=" + encodeURIComponent(target), text: p, onclick: (e) => { e.preventDefault(); go(target); } }));
    });
  }

  function renderEmpty() {
    const box = $("empty");
    const noAccounts = !(DM.me && DM.accountsCount);
    box.replaceChildren(
      DM.svg("0 0 72 72", noAccounts ? ["M14 22h20v14H14z", "M38 22h20v14H38z", "M26 44h20v14H26z", "M24 36l8 8M48 36l-8 8"]
        : ["M12 22a4 4 0 0 1 4-4h12l5 5h23a4 4 0 0 1 4 4v23a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4z", "M36 32v14M29 39h14"]),
      DM.el("h3", { text: noAccounts ? "Link an account first" : state.path === "/" ? "Your mesh is empty" : "This folder is empty" }),
      DM.el("p", { text: noAccounts ? "DriveMesh needs at least two storage accounts to keep two copies of every chunk."
        : "Drop files above. Each one is split, encrypted and copied to two accounts." }),
      noAccounts ? DM.el("a", { class: "btn btn-primary btn-sm", href: "/accounts", text: "Link an account" }) : "");
    box.classList.remove("hidden");
  }

  async function load() {
    renderCrumbs();
    markTree();
    let data;
    try { data = await DM.api("GET", "/api/v1/fs?path=" + encodeURIComponent(state.path)); }
    catch (err) { DM.toast(err.message, true); return; }
    const rows = $("rows");
    rows.replaceChildren();
    for (const d of data.folders) {
      rows.append(DM.el("tr", {},
        DM.el("td", {}, DM.el("a", { class: "file-name folder", href: "/files?path=" + encodeURIComponent(d.path),
          onclick: (e) => { e.preventDefault(); go(d.path); } }, DM.svg("0 0 20 20", FOLDER), DM.el("span", { text: d.name }))),
        DM.el("td", { class: "muted", text: "—" }), DM.el("td", { class: "col-health" }), DM.el("td", { class: "col-mod" }),
        DM.el("td", { class: "actions" },
          DM.el("button", { class: "btn btn-ghost btn-sm", text: "Rename", "aria-label": `Rename folder ${d.name}`, onclick: () => rename(d.path, d.name) }),
          DM.el("button", { class: "btn btn-ghost btn-sm btn-danger", text: "Delete", "aria-label": `Delete folder ${d.name}`, onclick: () => removeFolder(d.path) }))));
    }
    for (const f of data.files) {
      const scheme = f.scheme === "ec" ? `RS(${f.ec[0]},${f.ec[1]})` : `${f.rf}× replicated`;
      rows.append(DM.el("tr", {},
        DM.el("td", {}, DM.el("a", { class: "file-name", download: f.name, href: "/api/v1/files/content?path=" + encodeURIComponent(f.path),
          "aria-label": `Download ${f.name}` }, DM.svg("0 0 20 20", FILE),
          DM.el("span", {}, document.createTextNode(f.name), DM.el("span", { class: "file-meta", text: `${scheme} · ${DM.plural(f.chunks, "chunk")}` })))),
        DM.el("td", { text: DM.size(f.size) }),
        DM.el("td", { class: "col-health" }, DM.badge(f.health, f.scheme === "ec" && f.health === "healthy" ? "all shards" : HEALTH[f.health])),
        DM.el("td", { class: "col-mod muted", text: DM.when(f.committed_at) }),
        DM.el("td", { class: "actions" },
          DM.el("a", { class: "btn btn-ghost btn-sm act-download", download: f.name, href: "/api/v1/files/content?path=" + encodeURIComponent(f.path), text: "Download" }),
          DM.el("button", { class: "btn btn-ghost btn-sm btn-danger", text: "Delete", "aria-label": `Delete ${f.name}`, onclick: () => removeFile(f.path) }))));
    }
    const empty = data.folders.length + data.files.length === 0;
    $("file-table").classList.toggle("hidden", empty);
    if (empty) renderEmpty(); else $("empty").classList.add("hidden");
  }

  function go(path) {
    state.path = path;
    history.replaceState(null, "", path === "/" ? "/files" : "/files?path=" + encodeURIComponent(path));
    load();
  }

  async function rename(path, name) {
    const next = prompt("New name", name);
    if (!next || next === name) return;
    try { await DM.api("POST", "/api/v1/fs/move", { src: path, dst: path.slice(0, path.lastIndexOf("/") + 1) + next }); load(); renderTree(); }
    catch (err) { DM.toast(err.message, true); }
  }
  async function removeFile(path) {
    if (!confirm(`Delete ${path}? Its encrypted copies are purged from every account.`)) return;
    try { await DM.api("DELETE", "/api/v1/files?path=" + encodeURIComponent(path)); DM.toast("Deleted"); load(); }
    catch (err) { DM.toast(err.message, true); }
  }
  async function removeFolder(path) {
    if (!confirm(`Delete folder ${path} and everything in it?`)) return;
    try { const r = await DM.api("DELETE", "/api/v1/folders?recursive=true&path=" + encodeURIComponent(path)); DM.toast(`Deleted folder (${DM.plural(r.files_deleted, "file")})`); load(); renderTree(); }
    catch (err) { DM.toast(err.message, true); }
  }

  /* ---- uploads with per-chunk status ---- */
  const MAX_CELLS = 120;
  function upload(file, overwrite = false) {
    const cs = (DM.me && DM.me.chunk_size) || 8 * 1024 * 1024;
    const nChunks = Math.max(1, Math.ceil(file.size / cs));
    const per = Math.ceil(nChunks / MAX_CELLS);      // chunks represented by each square
    const nCells = Math.ceil(nChunks / per);
    const cells = Array.from({ length: nCells }, () => DM.el("span", { class: "chunk" }));
    const status = DM.el("span", { class: "upload-state", text: "Queued" });
    const meter = DM.meter(0, "accent");
    const card = DM.el("div", { class: "upload" },
      DM.el("div", { class: "upload-head" }, DM.el("span", { class: "upload-name", text: file.name, title: file.name }), status),
      meter,
      DM.el("div", { class: "chunks", role: "img", "aria-label": `${DM.plural(nChunks, "chunk")}` }, ...cells),
      DM.el("div", { class: "chunk-legend" },
        DM.el("span", {}, DM.el("span", { class: "chunk" }), document.createTextNode("waiting")),
        DM.el("span", {}, DM.el("span", { class: "chunk sending" }), document.createTextNode("sending")),
        DM.el("span", {}, DM.el("span", { class: "chunk sent" }), document.createTextNode("received")),
        DM.el("span", {}, DM.el("span", { class: "chunk stored" }), document.createTextNode("stored ×2")),
        per > 1 ? DM.el("span", { text: `each square = ${per} chunks` }) : ""));
    $("uploads").prepend(card);
    const fill = meter.firstChild;
    const setCells = (loaded) => {
      const doneChunks = Math.floor(loaded / cs);
      cells.forEach((c, i) => {
        const first = i * per, last = Math.min(nChunks, (i + 1) * per) - 1;
        c.className = "chunk " + (doneChunks > last ? "sent" : doneChunks >= first && loaded > first * cs ? "sending" : "");
      });
    };
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/v1/files/content?path=" + encodeURIComponent(join(state.path, file.name)) + (overwrite ? "&overwrite=true" : ""));
    xhr.setRequestHeader("X-CSRF-Token", DM.csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = (100 * e.loaded) / e.total;
      fill.style.width = pct.toFixed(1) + "%";
      setCells(e.loaded);
      status.textContent = pct >= 100 ? "Encrypting & replicating…" : `${Math.floor(pct)}% · ${DM.size(e.loaded)} of ${DM.size(e.total)}`;
    };
    xhr.onload = () => {
      if (xhr.status === 201) {
        const info = JSON.parse(xhr.responseText);
        card.classList.add("done");
        fill.style.width = "100%";
        cells.forEach((c) => { c.className = "chunk stored"; });
        status.textContent = `Stored · ${DM.plural(info.chunks, "chunk")} × ${info.rf} copies`;
        setTimeout(() => card.remove(), 6000);
        load();
        return;
      }
      let msg = xhr.statusText, code = "";
      try { const err = JSON.parse(xhr.responseText).error; msg = err.message; code = err.code; } catch (_) { /* keep status */ }
      card.classList.add("failed");
      cells.forEach((c) => { if (!c.classList.contains("sent")) c.className = "chunk failed"; });
      status.textContent = code === "already_exists" ? "A file with this name already exists" : msg;
      if (code === "already_exists") {
        status.append(DM.el("button", { class: "btn btn-sm", text: "Replace", onclick: () => { card.remove(); upload(file, true); } }));
      }
    };
    xhr.onerror = () => { card.classList.add("failed"); status.textContent = "Network error"; };
    xhr.send(file);
  }

  async function init() {
    if (!(await DM.loadMe())) return;
    try { DM.accountsCount = (await DM.api("GET", "/api/v1/accounts")).accounts.length; } catch (_) { DM.accountsCount = 0; }
    renderTree();
    load();
    const input = $("file-input"), dz = $("dropzone");
    $("upload-btn").addEventListener("click", () => input.click());
    dz.addEventListener("click", () => input.click());
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => { for (const f of input.files) upload(f); input.value = ""; });
    const area = document.querySelector(".files-card");
    area.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
    area.addEventListener("dragleave", (e) => { if (!area.contains(e.relatedTarget)) dz.classList.remove("over"); });
    area.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); for (const f of e.dataTransfer.files) upload(f); });
    $("new-folder").addEventListener("click", async () => {
      const name = prompt("Folder name");
      if (!name) return;
      try { await DM.api("POST", "/api/v1/folders", { path: join(state.path, name) }); load(); renderTree(); }
      catch (err) { DM.toast(err.message, true); }
    });
    if (location.hash === "#upload") { dz.focus(); dz.classList.add("over"); setTimeout(() => dz.classList.remove("over"), 1500); }
  }
  init();
})();
