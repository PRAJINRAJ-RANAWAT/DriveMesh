"use strict";
(() => {
  const form = document.getElementById("login-form");
  const err = document.getElementById("login-error");
  const btn = form.querySelector("button[type=submit]");
  const original = [...btn.childNodes].map((n) => n.cloneNode(true));
  function showError(msg) { err.textContent = msg; err.classList.remove("hidden"); }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.classList.add("hidden");
    if (!form.username.value.trim() || !form.password.value) {
      showError("Enter your username and password.");
      (form.username.value.trim() ? form.password : form.username).focus();
      return;
    }
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const res = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: form.username.value, password: form.password.value }),
      });
      if (res.ok) { location.href = "/app"; return; }
      const data = await res.json().catch(() => ({}));
      showError(res.status === 429 ? "Too many attempts. Wait a minute and try again."
        : res.status === 401 ? "That username and password don't match." : (data.error && data.error.message) || "Sign-in failed.");
      form.password.select();
    } catch (_) {
      showError("Can't reach the server. Check your connection and try again.");
    }
    btn.disabled = false;
    btn.replaceChildren(...original.map((n) => n.cloneNode(true)));
  });
})();
