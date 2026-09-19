import { toast } from "./toast.js";

// fetch() wrapper: JSON in and out, one place for error handling.
//   401 -> back to sign-in (the session expired or was revoked)
//   other errors -> a toast with the server's reason, then throw
export async function api(path, { method = "GET", body, quiet = false } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    if (!quiet) toast("Dispatcher unreachable — is the server running?", "error");
    throw new Error("network");
  }

  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
    throw new Error("unauthenticated");
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    const message = detail || `Request failed (${response.status})`;
    if (!quiet) toast(message, response.status === 403 ? "warn" : "error");
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

// Run an action from a button: busy state while pending, toast on success.
// Returns the result, or null when it failed (api() already said why).
export async function runAction(button, request, successMessage) {
  if (button) {
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
  }
  try {
    const result = await request();
    if (successMessage) {
      toast(typeof successMessage === "function" ? successMessage(result) : successMessage, "ok");
    }
    return result;
  } catch {
    return null;
  } finally {
    if (button) {
      button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }
}
