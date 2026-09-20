import { emptyState, h, keyedList, setText } from "../core/dom.js";
import { clock } from "../core/format.js";

// Human-readable event feed with category filters. The dispatcher's activity
// log is free text, so each line is classified by its wording.

const RULES = [
  { test: /^PENALTY/i, category: "faults", icon: "!", tone: "fault" },
  { test: /suspect|mismatch|never paid|refused/i, category: "money", icon: "?", tone: "warn" },
  { test: /charged|payment accepted|\bpaid\b|releasing/i, category: "money", icon: "$", tone: "money" },
  // Something actually failed.
  { test: /broken|stuck|failed|cannot|unable/i, category: "faults", icon: "⚙", tone: "fault" },
  // Planned work is not a fault: preventive maintenance in red reads as an
  // error to an operator, and it is the system working as intended.
  { test: /preventive|queued|scheduled|repair|maintenance|fixed/i, category: "faults", icon: "⚙", tone: "warn" },
  { test: /\bCO\b|fan/i, category: "faults", icon: "≋", tone: "warn" },
  { test: /dispatch|parked|vacated|left the|arriv|check-in|entry|exit|lot full|no available/i, category: "cars", icon: "▸", tone: "car" },
];

const FILTERS = [["all", "All"], ["cars", "Cars"], ["money", "Money"], ["faults", "Faults"]];

function classify(message) {
  return RULES.find((rule) => rule.test.test(message)) || { category: "other", icon: "·", tone: "" };
}

export function createFeed(filtersEl, listEl) {
  let filter = "all";
  let latest = [];

  const chips = FILTERS.map(([value, label]) => {
    const chip = h("button", { type: "button", class: `chip${value === filter ? " is-on" : ""}`, text: label,
      onclick: () => {
        filter = value;
        chips.forEach((c) => c.classList.toggle("is-on", c === chip));
        update(latest);
      } });
    return chip;
  });
  filtersEl.append(...chips);

  function update(activity) {
    latest = activity || [];
    const items = latest
      .map((entry) => ({ ...entry, kind: classify(entry.message) }))
      .filter((entry) => filter === "all" || entry.kind.category === filter);
    if (!items.length) {
      listEl.replaceChildren(emptyState(latest.length ? "Nothing in this category" : "No activity yet"));
      return;
    }
    keyedList(listEl, items, (entry) => `${entry.at}|${entry.message}`, render);
  }

  function render(entry) {
    const tone = entry.level === "error" ? "fault" : entry.kind.tone;
    const text = h("span", { class: "grow" });
    setText(text, entry.message);
    text.title = entry.message;
    return h("div", { class: "list-row" },
      h("span", { class: `feed-icon ${tone}`, "aria-hidden": "true", text: entry.kind.icon }),
      text,
      h("span", { class: "time", text: clock(entry.at) }));
  }

  return { update };
}
