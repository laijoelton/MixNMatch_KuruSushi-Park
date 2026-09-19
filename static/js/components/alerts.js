import { emptyState, h, keyedList, setText } from "../core/dom.js";
import { spotState } from "../core/format.js";

// "Needs attention" inbox (Samsara pattern): everything a human should look
// at, derived from the live snapshot + stats, most severe first. Each item
// jumps to the thing it is about.

const RANK = { bad: 0, warn: 1, info: 2 };

export function deriveAlerts(snapshot, stats, { isAdmin } = {}) {
  const alerts = [];
  const add = (key, sev, title, detail, target) => alerts.push({ key, sev, title, detail, target });

  for (const gate of snapshot.barriers || []) {
    // The main gate is operator-only and there is no sensor in front of it:
    // while it is shut, cars queue outside and the dispatcher never hears of them.
    if (gate.main_gate && gate.state !== "Open" && gate.state !== "Opening") {
      add(`main-gate-${gate.name}`, "bad", `Main gate (${gate.name}) is closed — no cars can enter`, "Open it from the gate panel to admit cars", { kind: "gate", name: gate.name });
    }
    if (gate.broken) add(`gate-broken-${gate.name}`, "bad", `Gate ${gate.name} is broken`, "Repair it — it cannot open or close", { kind: "gate", name: gate.name });
    else if (gate.under_maintenance) add(`gate-maint-${gate.name}`, "warn", `Gate ${gate.name} under repair`, "Do not operate until fixed", { kind: "gate", name: gate.name });
    else if (gate.repair_pending) add(`gate-pending-${gate.name}`, "warn", `Gate ${gate.name} repair queued`, "Excluded from operation until the repair starts", { kind: "gate", name: gate.name });
  }
  for (const spot of snapshot.spots || []) {
    if (spot.purpose !== "Park") continue;
    if (spot.broken) add(`bay-broken-${spot.name}`, "bad", `Bay ${spot.name} is broken`, spot.occupant_plate ? "Repair waits until the car leaves" : "Queue a repair", { kind: "spot", name: spot.name });
    else if (spot.under_maintenance) add(`bay-maint-${spot.name}`, "warn", `Bay ${spot.name} under repair`, "Excluded from dispatch", { kind: "spot", name: spot.name });
    else if (spot.repair_pending) add(`bay-pending-${spot.name}`, "warn", `Bay ${spot.name} repair queued`, "Excluded from dispatch until the repair starts", { kind: "spot", name: spot.name });
  }
  for (const [name] of Object.entries(snapshot.deferred_repairs || {})) {
    add(`deferred-${name}`, "warn", `Repair pending: ${name}`, "Starts automatically when the bay is empty", { kind: "spot", name });
  }
  for (const fan of snapshot.fans || []) {
    if (fan.broken) add(`fan-${fan.name}`, "bad", `Fan ${fan.name} is broken`, `Zone ${fan.zone || "—"} ventilation reduced`, { kind: "fan", name: fan.name });
    else if (fan.under_maintenance) add(`fan-maint-${fan.name}`, "warn", `Fan ${fan.name} under repair`, "Unavailable for ventilation control", { kind: "fan", name: fan.name });
    else if (fan.repair_pending) add(`fan-pending-${fan.name}`, "warn", `Fan ${fan.name} repair queued`, "Unavailable until the repair starts", { kind: "fan", name: fan.name });
  }
  for (const light of snapshot.lights || []) {
    if (light.broken || light.under_maintenance) add(`light-${light.name}`, "warn", `Light ${light.name} unavailable`, "Inspect the fixture; simulator has no light repair command", null);
  }
  for (const zone of snapshot.zones || []) {
    const level = zone.danger_level || "Safe";
    if (level === "High" || level === "Critical") add(`co-${zone.name}`, "bad", `CO ${level} in ${zone.name}`, `${Number(zone.co_level).toFixed(1)} — fans must run`, { kind: "zone", name: zone.name });
    else if (level === "Mid") add(`co-${zone.name}`, "warn", `CO rising in ${zone.name}`, `${Number(zone.co_level).toFixed(1)}`, { kind: "zone", name: zone.name });
  }

  // Zone closed for gate maintenance (4.26): no new cars until both gates are fixed.
  for (const [zone, info] of Object.entries(snapshot.zone_maintenance || {})) {
    const waiting = (info.todo || []).join(" and ");
    add(`zone-maint-${zone}`, "warn", `${zone} closed for gate maintenance`,
      `${info.trigger} needs repair. New cars go to other zones; reopens when ${waiting || "its gates"} ${info.todo?.length > 1 ? "are" : "is"} fixed`,
      { kind: "zone", name: zone });
  }
  for (const row of snapshot.neglected_vehicles || []) {
    add(`neglect-${row.plate}`, "warn", `Vehicle ${row.plate} never reached a bay`, row.reason, null);
  }
  for (const gate of snapshot.barriers || []) {
    if (gate.hold_reason) add(`hold-${gate.name}`, "warn", `${gate.name}: ${gate.hold_reason}`, "Review the vehicle or gate hold", { kind: "gate", name: gate.name });
  }
  const park = (snapshot.spots || []).filter((sp) => sp.purpose === "Park");
  // The dispatcher syncs bays once at startup; if the simulator had no level
  // running then, it holds zero bays and treats every arrival as "lot full".
  if (!park.length) {
    const waiting = (snapshot.sessions || []).length;
    add("no-bays", "bad", "No level running",
      `${waiting ? `${waiting} car${waiting > 1 ? "s" : ""} waiting. ` : ""}Start a level in the simulator — the dashboard loads it automatically.`,
      isAdmin ? { kind: "page", href: "/admin" } : null);
  }
  if (park.length && park.every((sp) => spotState(sp) !== "free")) {
    add("lot-full", "bad", "Car park is full", "New arrivals cannot be given a bay", null);
  }

  if (stats?.suspect_payments > 0) {
    add("suspect", "warn", `${stats.suspect_payments} suspect payment${stats.suspect_payments > 1 ? "s" : ""}`,
      "Amount did not match the invoice — car held at exit", isAdmin ? { kind: "page", href: "/payments" } : null);
  }
  if (stats?.sequence_gaps > 0) {
    add("gaps", "warn", `${stats.sequence_gaps} gap${stats.sequence_gaps > 1 ? "s" : ""} in the event stream`,
      "Some webhooks never arrived — consider a manual sync", isAdmin ? { kind: "page", href: "/admin" } : null);
  }
  if (stats?.unprocessed_events > 0) {
    add("unprocessed", "bad", `${stats.unprocessed_events} accepted webhook${stats.unprocessed_events > 1 ? "s" : ""} need processing`,
      "A handler failed; the event remains retryable and is preserved in the event log", isAdmin ? { kind: "page", href: "/logs" } : null);
  }

  return alerts.sort((a, b) => RANK[a.sev] - RANK[b.sev]);
}

export function createAlerts(listEl, countEl, { onOpen } = {}) {
  function update(alerts) {
    setText(countEl, alerts.length || "");
    if (!alerts.length) {
      listEl.replaceChildren(emptyState("All clear", "No faults, air-quality or payment issues right now."));
      return;
    }
    keyedList(listEl, alerts, (a) => a.key, render, fill);
  }

  function render(alert) {
    const refs = { sev: h("span"), title: h("div", { class: "alert-title" }), detail: h("div", { class: "alert-detail" }) };
    const el = h("div", {
      class: "list-row clickable", tabindex: 0, role: "button",
      onclick: () => el._alert.target && onOpen?.(el._alert.target),
      onkeydown: (event) => { if (event.key === "Enter" && el._alert.target) onOpen?.(el._alert.target); },
    }, refs.sev, h("div", { class: "grow" }, refs.title, refs.detail));
    el._refs = refs;
    fill(el, alert);
    return el;
  }

  function fill(el, alert) {
    el._alert = alert;
    el._refs.sev.className = `sev ${alert.sev}`;
    setText(el._refs.title, alert.title);
    setText(el._refs.detail, alert.detail);
  }

  return { update };
}
