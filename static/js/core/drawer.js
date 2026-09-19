import { append, clear, h } from "./dom.js";

// Right-hand detail panel (Samsara / Stripe pattern): select something,
// read its details, act on it, close with Esc or the X.

const drawer = () => document.getElementById("drawer");
let onCloseCallback = null;
let lastFocus = null;

export function openDrawer({ kicker, title, body, actions = [], onClose } = {}) {
  const el = drawer();
  if (onCloseCallback && onCloseCallback !== onClose) onCloseCallback();
  onCloseCallback = onClose || null;
  if (!el.classList.contains("is-open")) lastFocus = document.activeElement;

  clear(el);
  append(el, [
    h("div", { class: "drawer-head" },
      h("div", { class: "grow" },
        h("div", { class: "drawer-kicker", text: kicker || "" }),
        h("h2", { class: "drawer-title", id: "drawer-title", text: title || "" })),
      h("button", { class: "btn ghost small", type: "button", "aria-label": "Close details", text: "✕", onclick: closeDrawer })),
    h("div", { class: "drawer-body" }, body),
    actions.length ? h("div", { class: "drawer-actions" }, actions) : null,
  ]);
  el.classList.add("is-open");
  el.setAttribute("aria-hidden", "false");
  el.focus({ preventScroll: true });
}

// Replace only the body/actions (used for live refresh of an open drawer).
export function refreshDrawer({ body, actions }) {
  const el = drawer();
  if (!el.classList.contains("is-open")) return;
  const bodyEl = el.querySelector(".drawer-body");
  if (bodyEl && body) append(clear(bodyEl), body);
  const actionsEl = el.querySelector(".drawer-actions");
  if (actionsEl && actions) append(clear(actionsEl), actions);
}

export function closeDrawer() {
  const el = drawer();
  if (!el.classList.contains("is-open")) return;
  el.classList.remove("is-open");
  el.setAttribute("aria-hidden", "true");
  const callback = onCloseCallback;
  onCloseCallback = null;
  if (callback) callback();
  if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
}

export function isDrawerOpen() {
  return drawer().classList.contains("is-open");
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && isDrawerOpen() && !document.querySelector("dialog[open]")) closeDrawer();
});

// Definition list for drawer bodies: [["Zone", "ZONE1"], ["Occupant", plateEl], ...]
export function detailList(rows) {
  return h("dl", { class: "kv" }, rows.filter(Boolean).flatMap(([label, value]) => [
    h("dt", { text: label }),
    h("dd", {}, value instanceof Node ? value : String(value ?? "—")),
  ]));
}
