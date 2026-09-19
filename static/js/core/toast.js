import { h } from "./dom.js";

const LIFETIME_MS = { info: 3500, ok: 3500, warn: 5000, error: 6500 };
const SEVERITY = { info: "info", ok: "info", warn: "warn", error: "bad" };

export function toast(message, kind = "info", { dismissible = false, lifetimeMs } = {}) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const remove = () => {
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 220);
  };
  const el = h("div", { class: `toast ${kind}` },
    h("span", { class: `sev ${SEVERITY[kind] || "info"}` }),
    h("span", { class: "grow", text: message }),
    dismissible ? h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss", text: "✕", onclick: remove }) : null);
  host.append(el);
  while (host.children.length > 4) host.firstChild.remove();
  setTimeout(remove, lifetimeMs ?? LIFETIME_MS[kind] ?? 4000);
}

// Promise-based confirm dialog for actions with side effects.
export function confirmAction({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "confirm" },
      h("div", { class: "d-body" }, h("h3", { text: title }), h("p", { text: message })),
      h("div", { class: "d-actions" },
        h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: () => dialog.close("cancel") }),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", text: confirmLabel,
                      onclick: () => dialog.close("ok") })));
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok");
      dialog.remove();
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}
