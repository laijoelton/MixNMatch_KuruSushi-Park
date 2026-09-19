import { api } from "../core/api.js";

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");
const button = document.getElementById("login-btn");

// Only same-site paths are accepted as a return target ("//evil.com" is not).
function safeNext() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  return next.startsWith("/") && !next.startsWith("//") ? next : "/";
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
    await api("/api/auth/login", { method: "POST", body: { username, password }, quiet: true });
    location.href = safeNext();
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
