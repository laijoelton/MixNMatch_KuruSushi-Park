import { api, runAction } from "../core/api.js";
import { h, plate } from "../core/dom.js";
import { closeDrawer, detailList, isDrawerOpen, openDrawer, refreshDrawer } from "../core/drawer.js";
import { GATE_STATE_LABEL, PHASE_LABEL, SPOT_STATE_LABEL, TYPE_GLYPH, gateState, spotState, timeAgo } from "../core/format.js";
import { connect, onConnection, snapshot as currentSnapshot, subscribe } from "../core/live.js";
import { configurePalette } from "../core/palette.js";
import { initShell, setBanner, setLevelPill } from "../core/shell.js";
import { toast } from "../core/toast.js";
import { createAlerts, deriveAlerts } from "../components/alerts.js";
import { createFeed } from "../components/feed.js";
import { createKpis } from "../components/kpis.js";
import { createTwin } from "../components/twin.js";
import { createVehicles } from "../components/vehicles.js";
import { createZones } from "../components/zones.js";

const STATS_INTERVAL_MS = 5000;

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

subscribe(async (snap) => {
  if (snap.role && snap.role !== me.role) { location.reload(); return; }
  await loadGeometry(snap);
  twin.update(snap);
  kpis.update(snap, stats, canFinance);
  zones.update(snap);
  alerts.update(deriveAlerts(snap, stats, { isAdmin }));
  vehicles.update(snap.sessions || []);
  feed.update(snap.activity || []);
  renderWear(snap.wear || []);
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

await loadStats();
setInterval(loadStats, STATS_INTERVAL_MS);
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
  const out = state === "fault";
  const reason = out ? (gate.repair_pending ? "This gate is unavailable while its repair is queued." : "Operating a gate that is broken or under repair is penalised.") : null;
  const call = (verb) => api(`/api/manual/barrier/${encodeURIComponent(name)}/${verb}`, { method: "POST" });

  const openBtn = h("button", { class: "btn", type: "button", text: "Open / release hold", disabled: out || (gate.state === "Open" && !gate.operator_override),
    onclick: () => runAction(openBtn, () => call("open"), `Opening ${name}`) });
  const closeBtn = h("button", { class: "btn", type: "button", text: "Hold closed", disabled: out || (gate.state === "Closed" && gate.operator_override),
    onclick: () => runAction(closeBtn, () => call("close"), `Closing ${name}`) });
  const repairBtn = h("button", { class: `btn ${gate.broken ? "primary" : "ghost"}`, type: "button", text: "Repair",
    disabled: gate.under_maintenance || gate.repair_pending,
    onclick: () => runAction(repairBtn, () => api(`/api/manual/repair/${encodeURIComponent(name)}`, { method: "POST" }), `Repair queued for ${name}`) });

  return {
    kicker: "Barrier gate",
    title: name,
    signature: JSON.stringify([gate.state, gate.broken, gate.under_maintenance, gate.repair_pending, gate.hold_reason]),
    body: detailList([
      ["Position", h("span", { class: `tag ${state === "open" ? "free" : state === "fault" ? "fault" : state === "moving" ? "reserved" : ""}`, text: gate.state })],
      ["Condition", gate.broken ? "Broken" : gate.under_maintenance ? "Under repair" : gate.repair_pending ? "Repair queued" : "Good"],
      ["Zone", gate.zone || "Perimeter"],
      ["Service state", gate.hold_reason || GATE_STATE_LABEL[state]],
    ]),
    actions: [...(canGate ? [openBtn, closeBtn] : []), ...(canRepair ? [repairBtn] : []), reasonLine(reason)],
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

document.addEventListener("keydown", (event) => {
  if (event.key === "f" && !event.ctrlKey && !event.metaKey && !/input|textarea|select/i.test(event.target.tagName)) twin.fit();
});
window.addEventListener("beforeunload", closeDrawer);

window.addEventListener("park-alert", event => {
  const alert = event.detail;
  if (alert.alert_type !== "UNREGISTERED_VEHICLE_EXIT") return;
  const content = [h("b", { text: `Unregistered vehicle ${alert.plate} at ${alert.gate}. ` }), "Awaiting staff clearance."];
  if (canGate) {
    const button = h("button", { class: "btn small", text: "Authorize fallback invoice", onclick: async () => {
      await runAction(button, () => api("/api/ghost-car/override", { method: "POST", body: { ghost_id: alert.ghost_id } }), "Invoice attempted; awaiting payment");
    } });
    content.push(button);
  }
  setBanner(`ghost-${alert.ghost_id}`, { kind: "bad", text: content });
});
const ghosts = await api("/api/ghost-cars?resolved=false");
for (const ghost of ghosts) window.dispatchEvent(new CustomEvent("park-alert", { detail: {
  type: "alert", alert_type: "UNREGISTERED_VEHICLE_EXIT", plate: ghost.plate, gate: ghost.gate, ghost_id: ghost.id,
} }));
