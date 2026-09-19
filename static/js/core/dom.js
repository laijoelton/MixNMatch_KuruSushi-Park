// Tiny DOM helpers. Data only ever reaches the page as text nodes or
// attribute values - never through innerHTML - so a plate or penalty reason
// containing markup is displayed, not executed.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

// SVG elements need the SVG namespace; same attribute rules as h().
const SVG_NS = "http://www.w3.org/2000/svg";
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

function applyAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function setText(el, value) {
  const text = value == null ? "" : String(value);
  if (el.textContent !== text) el.textContent = text;
}

// Reconcile a list against its container by key. Existing nodes are updated
// in place and only moved when the order changes, so scroll position, hover
// and focus survive the 1-second live refresh.
export function keyedList(container, items, keyOf, render, update) {
  const existing = new Map();
  for (const child of [...container.children]) {
    if (child.dataset.key != null) existing.set(child.dataset.key, child);
    else child.remove();  // e.g. a previous empty state
  }
  let previous = null;
  for (const item of items) {
    const key = String(keyOf(item));
    let el = existing.get(key);
    if (el) {
      existing.delete(key);
      if (update) update(el, item);
    } else {
      el = render(item);
      el.dataset.key = key;
    }
    const expected = previous ? previous.nextSibling : container.firstChild;
    if (el !== expected) container.insertBefore(el, expected);
    previous = el;
  }
  for (const stale of existing.values()) stale.remove();
}

// Show an empty state in a keyed container (removed by the next keyedList call).
export function showEmpty(container, title, detail) {
  clear(container).append(emptyState(title, detail));
}

export function emptyState(title, detail) {
  return h("div", { class: "empty" }, h("b", { text: title }), detail ? h("span", { text: detail }) : null);
}

export function plate(text) {
  return h("span", { class: "plate", text: text || "—" });
}
