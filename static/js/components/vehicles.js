import { emptyState, h, keyedList, plate, setText } from "../core/dom.js";
import { PHASE_LABEL, naturalCompare } from "../core/format.js";

// Vehicles currently on site, grouped by where they are in their stay.

const PHASE_ORDER = ["ARRIVED", "ASSIGNED", "PARKED", "EXIT_REQUESTED", "AT_EXIT", "CHARGED"];
const PHASE_TAG = { ARRIVED: "accent", ASSIGNED: "accent", PARKED: "", EXIT_REQUESTED: "reserved", AT_EXIT: "reserved", CHARGED: "reserved" };

export function createVehicles(listEl, countEl, { onPick } = {}) {
  function update(sessions) {
    setText(countEl, sessions.length || "");
    if (!sessions.length) {
      listEl.replaceChildren(emptyState("No vehicles on site", "Cars appear here as soon as they reach an entry."));
      return;
    }
    const ordered = [...sessions].sort((a, b) =>
      PHASE_ORDER.indexOf(a.phase) - PHASE_ORDER.indexOf(b.phase) || naturalCompare(a.plate, b.plate));
    keyedList(listEl, ordered, (x) => x.plate, render, fill);
  }

  function render(session) {
    const refs = { plate: plate(session.plate), where: h("span", { class: "grow muted" }), phase: h("span", { class: "tag" }) };
    const el = h("div", {
      class: "list-row clickable", tabindex: 0, role: "button",
      onclick: () => onPick?.(el._session),
      onkeydown: (event) => { if (event.key === "Enter") onPick?.(el._session); },
    }, refs.plate, refs.where, refs.phase);
    el._refs = refs;
    fill(el, session);
    return el;
  }

  function fill(el, session) {
    el._session = session;
    const r = el._refs;
    setText(r.plate, session.plate);
    const where = session.assigned_spot
      ? `Bay ${session.assigned_spot}`
      : session.exit_gate || session.entry_gate || "—";
    setText(r.where, where);
    r.phase.className = `tag ${PHASE_TAG[session.phase] || ""}`;
    setText(r.phase, session.paid ? "Paid" : PHASE_LABEL[session.phase] || session.phase);
  }

  return { update };
}
