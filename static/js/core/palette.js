import { clear, h } from "./dom.js";

// Ctrl/⌘+K quick search (Linear / Vercel pattern). Each page supplies the
// searchable items and what "pick" does; pages are always included.

const PAGES = [
  { label: "Live operations", kind: "page", href: "/" },
  { label: "History", kind: "page", href: "/history" },
  { label: "Payments", kind: "page", href: "/payments", cap: "fin:view" },
  { label: "Admin", kind: "page", href: "/admin", cap: "admin:users" },
];

let itemsProvider = () => [];
let pickHandler = () => {};
let capabilities = [];

export function configurePalette({ items, onPick, capabilities: caps }) {
  if (items) itemsProvider = items;
  if (onPick) pickHandler = onPick;
  if (caps) capabilities = caps;
}

export function initPalette() {
  const host = document.getElementById("palette");
  const button = document.getElementById("palette-btn");
  if (!host) return;

  const input = h("input", { type: "search", placeholder: "Search plates, bays, gates, pages…", "aria-label": "Search" });
  const list = h("div", { class: "palette-list", role: "listbox" });
  host.append(h("div", { class: "palette-box", role: "dialog", "aria-label": "Quick search" }, input, list));

  let results = [];
  let active = 0;

  function allItems() {
    const pages = PAGES.filter((p) => !p.cap || capabilities.includes(p.cap));
    const items = [...itemsProvider()].sort((a, b) =>
      a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: "base" }));
    return [...items, ...pages];
  }

  function render() {
    const query = input.value.trim().toLowerCase().replace(/\s+/g, "");
    results = allItems()
      .filter((item) => !query || item.label.toLowerCase().replace(/\s+/g, "").includes(query))
      .slice(0, 30);
    active = Math.min(active, Math.max(0, results.length - 1));
    clear(list);
    if (!results.length) list.append(h("div", { class: "empty", text: "No matches" }));
    results.forEach((item, index) => {
      list.append(h("div", {
        class: `palette-item${index === active ? " is-active" : ""}`, role: "option",
        onmousemove: () => { if (active !== index) { active = index; render(); } },
        onclick: () => pick(item),
      }, item.plate ? h("span", { class: "plate", text: item.label }) : h("span", { text: item.label }),
         h("span", { class: "kind", text: item.kind })));
    });
  }

  function open() {
    host.hidden = false;
    input.value = "";
    active = 0;
    render();
    input.focus();
  }

  function close() {
    host.hidden = true;
  }

  function pick(item) {
    close();
    if (item.href) location.href = item.href;
    else pickHandler(item);
  }

  input.addEventListener("input", () => { active = 0; render(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { active = Math.min(results.length - 1, active + 1); render(); event.preventDefault(); }
    if (event.key === "ArrowUp") { active = Math.max(0, active - 1); render(); event.preventDefault(); }
    if (event.key === "Enter" && results[active]) pick(results[active]);
    if (event.key === "Escape") close();
  });
  host.addEventListener("click", (event) => { if (event.target === host) close(); });
  button?.addEventListener("click", open);
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      host.hidden ? open() : close();
    }
  });
}
