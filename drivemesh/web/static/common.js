/* Shared helpers. All DOM is built with textContent / createElement: no innerHTML with data. */
"use strict";

const DM = {
  csrf: null,

  el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) node.append(c);
    return node;
  },

  async me() {
    const res = await fetch("/api/v1/auth/me");
    if (res.status === 401) { location.href = "/login"; return null; }
    const data = await res.json();
    DM.csrf = data.csrf_token;
    return data;
  },

  async api(method, url, body, extraHeaders = {}) {
    const headers = { "X-CSRF-Token": DM.csrf || "", ...extraHeaders };
    let payload = body;
    if (body !== undefined && !(body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(url, { method, headers, body: payload });
    if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
    const text = await res.text();
    const data = text ? JSON.parse(text) : {};
    if (!res.ok) throw new Error((data.error && data.error.message) || res.statusText);
    return data;
  },

  size(n) {
    if (n === null || n === undefined) return "–";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  },

  when(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return d.toLocaleString();
  },
};
