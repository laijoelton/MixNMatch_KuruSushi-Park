import { api } from "../core/api.js";

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");
const button = document.getElementById("login-btn");

// Only same-site paths are accepted as a return target ("//evil.com" is not).
function safeNext() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  try {
    const target = new URL(next, location.origin);
    return target.origin === location.origin ? target.pathname + target.search + target.hash : "/";
  } catch { return "/"; }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = form.username.value.trim();
  const password = form.password.value;
  if (!username || !password) {
    errorEl.textContent = "Enter your username and password.";
    return;
  }

  errorEl.textContent = "";
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    const result = await api("/api/auth/login", { method: "POST", body: { username, password }, quiet: true });
    form.hidden = true;
    const panel = document.createElement("section");
    const title = document.createElement("h2"); title.textContent = "Recent sign-in attempts"; panel.append(title);
    for (const attempt of result.prior_attempts || []) {
      const row = document.createElement("p");
      row.textContent = `${attempt.occurred_at} ? ${attempt.ip || "Unknown IP"} ? ${attempt.success ? "Successful" : "Failed"}`;
      panel.append(row);
    }
    if (!result.prior_attempts.length) { const p = document.createElement("p"); p.textContent = "No previous attempts."; panel.append(p); }
    const link = document.createElement("a"); link.href = safeNext(); link.className = "btn primary"; link.textContent = "Continue to dashboard";
    panel.append(link); form.after(panel);
  } catch (error) {
    errorEl.textContent = error.status === 401
      ? "Invalid username or password."
      : "Could not reach the server. Try again in a moment.";
    form.password.select();
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
});
