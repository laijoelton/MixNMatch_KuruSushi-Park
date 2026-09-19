import { api } from "./api.js";
import { h, setText } from "./dom.js";
import { configurePalette, initPalette } from "./palette.js";
import { toast } from "./toast.js";

// Shared page chrome: who is signed in, what they may see, and the system
// status pills. Every page calls initShell() first.

const HEALTH_INTERVAL_MS = 10000;
let banners = new Map();

export async function initShell({ usesLiveSocket = false } = {}) {
  const me = await api("/api/me");
  setText(document.getElementById("user-name"), me.username);
  setText(document.getElementById("user-role"), me.role);
  setText(document.getElementById("user-avatar"), me.username.slice(0, 2));

  const isAdmin = me.role === "admin";
  for (const el of document.querySelectorAll("[data-role='admin']")) el.hidden = !isAdmin;
  document.body.dataset.role = me.role;

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", quiet: true }).catch(() => {});
    location.href = "/login";
  });

  const params = new URLSearchParams(location.search);
  if (params.has("denied")) {
    toast("That page needs the admin role.", "warn");
    history.replaceState(null, "", location.pathname);
  }

  configurePalette({ admin: isAdmin });
  initPalette();
  pollHealth(usesLiveSocket);
  return me;
}

// /healthz is public and cheap: it tells us whether commands really go out.
async function pollHealth(usesLiveSocket) {
  const autopilotPill = document.getElementById("autopilot-pill");
  const connPill = document.getElementById("conn-pill");
  const tick = async () => {
    try {
      const health = await fetch("/healthz", { credentials: "same-origin" }).then((r) => r.json());
      autopilotPill.hidden = false;
      autopilotPill.className = `pill ${health.autopilot ? "is-live" : "is-warn"}`;
      setText(autopilotPill, health.autopilot ? "Autopilot on" : "Dry-run");
      setBanner("autopilot", health.autopilot ? null : {
        kind: "warn",
        text: ["Dry-run mode — ", h("b", { text: "no commands are sent to the simulator" }), ". Set AUTOPILOT=true to operate the park."],
      });
      if (!usesLiveSocket) {
        connPill.className = "pill is-live";
        setText(connPill, "Online");
      }
    } catch {
      if (!usesLiveSocket) {
        connPill.className = "pill is-bad";
        setText(connPill, "Offline");
      }
    }
  };
  await tick();
  setInterval(tick, HEALTH_INTERVAL_MS);
}

// Page-level notices, keyed so each condition shows at most once.
export function setBanner(key, banner) {
  const host = document.getElementById("banners");
  const existing = banners.get(key);
  if (!banner) {
    if (existing) existing.remove();
    banners.delete(key);
    return;
  }
  const el = h("div", { class: `banner ${banner.kind}`, role: banner.kind === "bad" ? "alert" : "status" }, h("span", {}, banner.text));
  if (existing) existing.replaceWith(el);
  else host.append(el);
  banners.set(key, el);
}

export function setLevelPill(level) {
  const pill = document.getElementById("level-pill");
  pill.hidden = !level;
  setText(pill, level ? `Level ${level.replace("lvl", "")}` : "");
}
