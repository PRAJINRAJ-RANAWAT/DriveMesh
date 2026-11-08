"use strict";
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const err = document.getElementById("login-error");
  const btn = form.querySelector("button[type=submit]");
  err.textContent = "";
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
    err.textContent = (data.error && data.error.message) || "Sign-in failed";
  } catch (_) {
    err.textContent = "Network error, try again";
  }
  btn.disabled = false;
  btn.textContent = "Sign in";
});
