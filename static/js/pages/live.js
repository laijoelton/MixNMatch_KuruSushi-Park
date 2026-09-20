import { api, runAction } from "../core/api.js";
import { h, plate } from "../core/dom.js";
import { closeDrawer, detailList, isDrawerOpen, openDrawer, refreshDrawer } from "../core/drawer.js";
import { GATE_STATE_LABEL, PHASE_LABEL, SPOT_STATE_LABEL, TYPE_GLYPH, gateState, spotState, timeAgo } from "../core/format.js";
import { connect, onConnection, snapshot as currentSnapshot, subscribe } from "../core/live.js";
import { configurePalette } from "../core/palette.js";
import { initShell, setBanner, setLevelPill } from "../core/shell.js";
import { confirmAction, toast } from "../core/toast.js";
import { createAlerts, deriveAlerts } from "../components/alerts.js";
import { createFeed } from "../components/feed.js";
import { createKpis } from "../components/kpis.js";
import { createTwin } from "../components/twin.js";
import { createVehicles } from "../components/vehicles.js";
import { createZones } from "../components/zones.js";

const STATS_INTERVAL_MS = 5000;
// Wear is no longer in the live frame (it was half of it and it barely moves).
const WEAR_INTERVAL_MS = 30000;

const me = await initShell({ usesLiveSocket: true });
const isAdmin = me.capabilities.includes("admin:users");
const canFinance = me.capabilities.includes("fin:view");
const canRepair = me.capabilities.includes("maint:control");
const canGate = me.capabilities.includes("ops:control_gates");
let stats = null;
let geometryKey = null;     // which spot set the drawn geometry belongs to
let drawerTarget = null;    // {kind, name} shown in the drawer
let drawerSignature = null; // re-render the drawer only when its subject changes

// ------------------------------------------------------------------ widgets
const kpis = createKpis(document.getElementById("kpis"));
const twin = createTwin(document.getElementById("twin-panel"), { onSelect: showDetails });
const zones = createZones(document.getElementById("zones"), {
  onFocus: (name) => { twin.focusZone(name); document.getElementById("twin-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); },
  onGate: (name) => showDetails("gate", name),
});
const alerts = createAlerts(document.getElementById("attention-list"), document.getElementById("attention-count"), {
  onOpen: (target) => {
    if (target.kind === "page") location.href = target.href;
    else if (target.kind === "zone") twin.focusZone(target.name);
    else showDetails(target.kind, target.name);
  },
});
const vehicles = createVehicles(document.getElementById("vehicles-list"), document.getElementById("vehicles-count"), {
  onPick: (session) => (session.assigned_spot ? showDetails("spot", session.assigned_spot) : toast(`${session.plate} has no bay yet`)),
});
const feed = createFeed(document.getElementById("feed-filters"), document.getElementById("feed-list"));

configurePalette({
  items: () => {
    const snap = currentSnapshot();
    if (!snap) return [];
    return [
      ...(snap.sessions || []).map((x) => ({ label: x.plate, kind: "vehicle", plate: true, target: x.assigned_spot ? ["spot", x.assigned_spot] : null })),
      ...(snap.spots || []).filter((x) => x.purpose === "Park").map((x) => ({ label: x.name, kind: "bay", target: ["spot", x.name] })),
      ...(snap.barriers || []).map((x) => ({ label: x.name, kind: "gate", target: ["gate", x.name] })),
    ];
  },
  onPick: (item) => (item.target ? showDetails(...item.target) : toast(`${item.label} has no bay yet`)),
});

// ------------------------------------------------------------------ data flow
async function loadGeometry(snap) {
  const key = (snap?.spots || []).map((x) => x.name).sort().join(",");
  if (key === geometryKey) return;
  geometryKey = key;
  try {
    const geo = await api("/api/twin", { quiet: true });
    twin.setGeometry(geo, snap);
    setLevelPill(geo.level);
    twin.update(snap);
  } catch {
    twin.setGeometry(null, snap);
  }
}

async function loadStats() {
  try {
    stats = await api("/api/stats", { quiet: true });
  } catch {
    /* keep the last figures; the connection pill already shows trouble */
  }
}

async function loadWear() {
  if (!me.capabilities.includes("maint:view")) return;
  try {
    const data = await api("/api/wear", { quiet: true });
    renderWear(data.items || []);
    renderComponentSummary(data.summary || []);
  } catch {
    /* keep the last table */
  }
}

subscribe(async (snap) => {
  if (snap.role && snap.role !== me.role) { location.reload(); return; }
  await loadGeometry(snap);
  twin.update(snap);
  kpis.update(snap, stats, canFinance);
  zones.update(snap);
  alerts.update(deriveAlerts(snap, stats, { isAdmin }));
  vehicles.update(snap.sessions || []);
  feed.update(snap.activity || []);

  updateFullBanner(snap);
  if (drawerTarget && isDrawerOpen()) showDetails(drawerTarget.kind, drawerTarget.name, { refresh: true });
});

onConnection((state, lastUpdateAt) => {
  const pill = document.getElementById("conn-pill");
  pill.className = `pill ${state === "live" ? "is-live" : state === "reconnecting" ? "is-bad" : ""}`;
  pill.textContent = state === "live" ? "Live" : state === "reconnecting" ? "Reconnecting…" : "Connecting…";
  document.body.classList.toggle("is-stale", state === "reconnecting");
  setBanner("connection", state === "reconnecting" ? {
    kind: "bad",
    text: ["Connection to the dispatcher lost — ", h("b", { text: "showing data from " + (lastUpdateAt ? timeAgo(lastUpdateAt / 1000) : "before the drop") }), ". Retrying automatically."],
  } : null);
});

function updateFullBanner(snap) {
  const park = (snap.spots || []).filter((x) => x.purpose === "Park");
  const full = park.length > 0 && park.every((x) => spotState(x) !== "free");
  setBanner("full", full ? { kind: "bad", text: [h("b", { text: "Car park full. " }), "No free bays — arriving cars must be turned away."] } : null);
}

// Free flow. Level 3 congestion (cars queueing at 8 entrances, paid cars with
// no route out) clears fastest by not gating traffic at all; one gate at a time
// is not a speed a human can work at with 19 of them.
const openAllBtn = document.getElementById("gates-open-all");
if (openAllBtn) {
  openAllBtn.onclick = async () => {
    if (!(await confirmAction({
      title: "Open every gate?",
      message: "All healthy gates are held open until you press All automatic. Cars enter and leave freely; broken gates and gates under repair are skipped.",
      confirmLabel: "Open all gates",
    }))) return;
    runAction(openAllBtn, async () => {
      const r = await api("/api/gates/open-all", { method: "POST" });
      toast(`${r.opened.length} gate(s) held open${r.skipped.length ? `, ${r.skipped.length} skipped` : ""}`);
    }, "Opening every gate");
  };
}
const autoAllBtn = document.getElementById("gates-auto-all");
if (autoAllBtn) {
  autoAllBtn.onclick = () => runAction(autoAllBtn, async () => {
    const r = await api("/api/gates/auto-all", { method: "POST" });
    toast(`${r.automatic.length} gate(s) back on automatic`);
  }, "Handing the gates back");
}

await loadStats();
setInterval(loadStats, STATS_INTERVAL_MS);
await loadWear();
setInterval(loadWear, WEAR_INTERVAL_MS);
connect();

// ------------------------------------------------------------------ details drawer
function showDetails(kind, name, { refresh = false } = {}) {
  const snap = currentSnapshot();
  if (!snap) return;
  const view = kind === "spot" ? spotView(snap, name) : kind === "gate" ? gateView(snap, name) : kind === "fan" ? fanView(snap, name) : null;
  if (!view) {
    if (!refresh) toast(`${name} is not in the live data yet`, "warn");
    return;
  }
  if (refresh) {
    if (view.signature !== drawerSignature) {
      drawerSignature = view.signature;
      refreshDrawer(view);
    }
    return;
  }
  drawerTarget = { kind, name };
  drawerSignature = view.signature;
  if (kind === "spot" || kind === "gate") twin.select(kind, name);
  openDrawer({ ...view, onClose: () => { drawerTarget = null; twin.select(null); } });
}

function reasonLine(text) {
  return text ? h("div", { class: "reason", text }) : null;
}

function spotView(snap, name) {
  const spot = (snap.spots || []).find((x) => x.name === name);
  if (!spot) return null;
  const state = spotState(spot);
  const session = (snap.sessions || []).find((x) => x.assigned_spot === name);
  const occupied = Boolean(spot.occupant_plate) || state === "occupied" || state === "reserved";

  // Repairing an occupied bay is penalised, and one already under repair can't be re-queued.
  let blocked = null;
  if (occupied) blocked = "A car holds this bay — repairing it now is penalised.";
  else if (spot.under_maintenance) blocked = "Already under repair.";
  else if (spot.repair_pending) blocked = "Repair already queued.";

  const repair = h("button", { class: `btn ${spot.broken ? "primary" : ""}`, type: "button", disabled: Boolean(blocked),
    text: spot.broken ? "Repair bay" : "Preventive repair",
    onclick: () => runAction(repair, () => api(`/api/manual/repair/${encodeURIComponent(name)}`, { method: "POST" }), `Repair queued for ${name}`) });

  return {
    kicker: "Parking bay",
    title: name,
    signature: JSON.stringify([state, spot.occupant_plate, spot.broken, spot.under_maintenance, spot.repair_pending, session?.phase]),
    body: detailList([
      ["Status", h("span", { class: `tag ${state === "free" ? "free" : state === "fault" ? "fault" : state === "reserved" ? "reserved" : ""}`, text: SPOT_STATE_LABEL[state] })],
      ["Type", `${TYPE_GLYPH[spot.car_type] ? TYPE_GLYPH[spot.car_type] + " " : ""}${spot.car_type || "Any"}`],
      ["Zone", spot.zone || "—"],
      ["Vehicle", spot.occupant_plate ? plate(spot.occupant_plate) : "—"],
      session ? ["Stay", PHASE_LABEL[session.phase] || session.phase] : null,
      session ? ["Entered via", session.entry_gate || "—"] : null,
      ["Condition", spot.broken ? "Broken" : spot.under_maintenance ? "Under repair" : spot.repair_pending ? "Repair queued" : "Good"],
    ]),
    actions: canRepair ? [repair, reasonLine(blocked)] : [],
  };
}

// "How much of the site is working" - the question the per-item table below
// stops answering once there are 250 bays and 19 gates.
function renderComponentSummary(rows) {
  const host = document.getElementById("component-summary");
  if (!host) return;
  host.replaceChildren(...rows.map((row) => {
    const faults = row.broken + row.under_maintenance + row.repair_pending;
    return h("div", { class: `summary-card${faults ? " has-faults" : ""}` },
      h("div", { class: "summary-title", text: row.family }),
      h("div", { class: "summary-figure num" }, h("b", { text: String(row.available) }),
        h("span", { class: "muted", text: ` / ${row.total} available` })),
      h("div", { class: "summary-detail muted", text: faults
        ? `${row.broken} broken · ${row.under_maintenance} under repair · ${row.repair_pending} queued`
        : "All available" }));
  }));
}

function renderWear(rows) {
  const host = document.getElementById("wear-rows");
  const sorted = [...rows].sort((a, b) => Number(b.broken) - Number(a.broken) || b.wear_percent - a.wear_percent || a.name.localeCompare(b.name));
  host.replaceChildren(...sorted.map(row => h("tr", {},
    ...[row.name, row.type, row.broken ? "Broken" : row.under_maintenance ? "Under repair" : row.repair_pending ? "Repair queued" : "Available",
      row.cycle_count, (row.runtime_seconds / 3600).toFixed(2), row.repair_supported ? `${row.wear_percent}%` : "Tracked only"]
      .map(text => h("td", { text })))));
  if (!sorted.length) host.replaceChildren(h("tr", {}, h("td", { colspan: 6, text: "No component usage received yet." })));
}

function gateView(snap, name) {
  const gate = (snap.barriers || []).find((x) => x.name === name);
  if (!gate) return null;
  const state = gateState(gate);
  // 4.33: staff commands win. Only a broken gate or one actually under repair
  // refuses (operating it is penalised); a merely queued repair is cancelled.
  const out = gate.broken || gate.under_maintenance;
  const reason = out ? "Operating a gate that is broken or under repair is penalised." : null;
  const call = (verb) => api(`/api/manual/barrier/${encodeURIComponent(name)}/${verb}`, { method: "POST" });
  const waiting = gate.held_plates || [];

  const openBtn = h("button", { class: `btn${waiting.length ? " primary" : ""}`, type: "button",
    text: waiting.length ? `Open & let ${waiting.join(", ")} out` : "Open (hold open)",
    disabled: out || (gate.operator_open && !waiting.length),
    onclick: () => runAction(openBtn, () => call("open"), `Opening ${name}`) });
  const closeBtn = h("button", { class: "btn", type: "button", text: "Hold closed", disabled: out || gate.operator_override,
    onclick: () => runAction(closeBtn, () => call("close"), `Closing ${name}`) });
  const autoBtn = h("button", { class: "btn ghost", type: "button", text: "Automatic",
    disabled: !(gate.operator_open || gate.operator_override),
    onclick: () => runAction(autoBtn, () => call("auto"), `${name} back on automatic`) });
  const repairBtn = h("button", { class: `btn ${gate.broken ? "primary" : "ghost"}`, type: "button", text: "Repair",
    disabled: gate.under_maintenance || gate.repair_pending,
    onclick: () => runAction(repairBtn, () => api(`/api/manual/repair/${encodeURIComponent(name)}`, { method: "POST" }), `Repair queued for ${name}`) });

  // Cars held here for paying the wrong amount. Re-invoicing is a staff call,
  // not an automatic one: the simulator fines "Car has already paid" at RM50.
  const unpaid = waiting.filter((p) => {
    const session = (snap.sessions || []).find((x) => x.plate === p);
    return session && session.charged && !session.paid;
  });
  const payBtns = unpaid.map((p) => {
    const btn = h("button", { class: "btn ghost", type: "button", text: `Ask ${p} to pay again`,
      title: "Re-invoices the car. Fined RM50 if the simulator considers it already paid.",
      onclick: () => runAction(btn, () => api(`/api/sessions/${encodeURIComponent(p)}/request-payment`, { method: "POST" }), `Payment re-requested from ${p}`) });
    return btn;
  });

  return {
    kicker: "Barrier gate",
    title: name,
    signature: JSON.stringify([gate.state, gate.broken, gate.under_maintenance, gate.repair_pending, gate.hold_reason, unpaid]),
    body: detailList([
      ["Position", h("span", { class: `tag ${state === "open" ? "free" : state === "fault" ? "fault" : state === "moving" ? "reserved" : ""}`, text: gate.state })],
      ["Condition", gate.broken ? "Broken" : gate.under_maintenance ? "Under repair" : gate.repair_pending ? "Repair queued" : "Good"],
      ["Opens since repair", String(gate.opens_since_repair ?? 0)],
      ...(gate.failure_prediction ? [["Breaks on next open",
        `${Math.round(gate.failure_prediction.failure_probability * 100)}% (${gate.failure_prediction.source === "model"
          ? `learned from ${gate.failure_prediction.samples} of its own repairs`
          : "opens heuristic — not enough history yet"})`]] : []),
      ["Zone", gate.zone || "Perimeter"],
      ["Service state", gate.hold_reason || GATE_STATE_LABEL[state]],
    ]),
    actions: [...(canGate ? [openBtn, closeBtn, autoBtn, ...payBtns] : []), ...(canRepair ? [repairBtn] : []),
              reasonLine(reason), reasonLine(payBtns.length
                ? "Re-invoicing is fined RM50 if the car already paid — check the amount first."
                : null)],
  };
}

function fanView(snap, name) {
  const fan = (snap.fans || []).find((x) => x.name === name);
  if (!fan) return null;
  const zone = (snap.zones || []).find((z) => z.name === fan.zone);
  const repairBtn = h("button", { class: "btn", type: "button", text: "Repair", disabled: fan.under_maintenance || fan.repair_pending,
    onclick: () => runAction(repairBtn, () => api(`/api/manual/repair/${encodeURIComponent(name)}`, { method: "POST" }), `Repair queued for ${name}`) });
  return {
    kicker: "Exhaust fan",
    title: name,
    signature: JSON.stringify([fan.is_on, fan.broken, fan.under_maintenance, fan.repair_pending, zone?.co_level]),
    body: detailList([
      ["Running", fan.is_on ? "Yes" : "No"],
      ["Condition", fan.broken ? "Broken" : fan.under_maintenance ? "Under repair" : fan.repair_pending ? "Repair queued" : "Good"],
      ["Zone", fan.zone || "—"],
      ["Zone CO", zone ? `${Number(zone.co_level).toFixed(1)} (${zone.danger_level || "Safe"})` : "—"],
    ]),
    actions: [...(canRepair ? [repairBtn] : []), reasonLine("Fans switch automatically with CO level.")],
  };
}

// ------------------------------------------------------------------ predictive alert toasts
// The full ML telemetry (ventilation forecast / component health / anomaly
// log) lives on its own nav page (/ml-insights, pages/ml_insights.js). The
// bell here only signals that something predictive just fired.
const PREDICTIVE_ALERT_TYPES = new Set(["PREDICTIVE_CO_WARNING", "PREDICTIVE_MAINTENANCE_WARNING", "GHOST_CAR_RESOLVED"]);

function predictiveAlertMessage(alert) {
  if (alert.alert_type === "PREDICTIVE_CO_WARNING") {
    return `⚠️ Zone ${alert.zone}: CO predicted to hit ${alert.predicted_ppm}ppm in ${alert.minutes_to_threshold} min — fans switch on above 50`;
  }
  if (alert.alert_type === "PREDICTIVE_MAINTENANCE_WARNING") {
    return `🔧 ${alert.component} (${alert.component_type}) predicted to fail in ${alert.days_to_failure}d (${Math.round(alert.failure_probability * 100)}% risk)`;
  }
  if (alert.alert_type === "GHOST_CAR_RESOLVED") {
    return alert.fallback_charge != null
      ? `✅ Ghost car ${alert.plate} resolved — invoiced $${Number(alert.fallback_charge).toFixed(2)}`
      : `✅ Ghost car ${alert.plate} resolved`;
  }
  return null;
}

const MAX_NOTIFICATIONS = 20;
let unreadCount = 0;
const notifications = []; // most recent first: {message, time}

function bumpUnread() {
  unreadCount += 1;
  const badge = document.getElementById("ml-bell-badge");
  badge.hidden = false;
  badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
}

function pushNotification(message) {
  notifications.unshift({ message, time: Date.now() });
  notifications.length = Math.min(notifications.length, MAX_NOTIFICATIONS);
  renderBellPopover();
}

function renderBellPopover() {
  const list = document.getElementById("ml-bell-list");
  if (!notifications.length) {
    list.replaceChildren(h("div", { class: "bell-empty", text: "No notifications yet." }));
    return;
  }
  list.replaceChildren(...notifications.map(n => h("div", { class: "bell-item" },
    h("span", { text: n.message }),
    h("span", { class: "time", text: new Date(n.time).toLocaleTimeString() }))));
}

renderBellPopover();
const bellPopover = document.getElementById("ml-bell-popover");
document.getElementById("ml-bell-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  unreadCount = 0;
  document.getElementById("ml-bell-badge").hidden = true;
  bellPopover.hidden = !bellPopover.hidden;
});
document.addEventListener("click", (event) => {
  if (!bellPopover.hidden && !event.target.closest(".bell-wrap")) bellPopover.hidden = true;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "f" && !event.ctrlKey && !event.metaKey && !/input|textarea|select/i.test(event.target.tagName)) twin.fit();
});
window.addEventListener("beforeunload", closeDrawer);

// Ghost cars and held vehicles: the gate is ringed orange on the map and listed
// under Needs attention; staff open it from the gate drawer (4.33).
window.addEventListener("park-alert", event => {
  const alert = event.detail;
  if (PREDICTIVE_ALERT_TYPES.has(alert.alert_type)) {
    const message = predictiveAlertMessage(alert);
    if (message) {
      toast(message, alert.alert_type === "GHOST_CAR_RESOLVED" ? "ok" : "warn", { dismissible: true, lifetimeMs: 15000 });
      pushNotification(message);
      bumpUnread();
    }
    return;
  }
});
