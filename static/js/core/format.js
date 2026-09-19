// Formatting helpers and the one status vocabulary every view shares.
// Timestamps arrive as epoch seconds (live state) or ISO strings (database).

function toDate(value) {
  if (value == null || value === "") return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function clock(value) {
  const date = toDate(value);
  return date ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}

export function dateTime(value) {
  const date = toDate(value);
  return date
    ? date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "—";
}

export function timeAgo(value) {
  const date = toDate(value);
  if (!date) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

export function money(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(2);
}

export function minutes(value) {
  if (value == null) return "—";
  const n = Number(value);
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} min`;
}

export function naturalCompare(a, b) {
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

export function spotState(spot) {
  if (spot.broken || spot.under_maintenance || spot.status === "BROKEN" || spot.status === "MAINTENANCE") return "fault";
  if (spot.status === "OCCUPIED") return "occupied";
  if (spot.status === "RESERVED") return "reserved";
  return "free";
}

export const SPOT_STATE_LABEL = { free: "Free", occupied: "Occupied", reserved: "Reserved", fault: "Out of service" };

export function gateState(barrier) {
  if (barrier.broken || barrier.under_maintenance) return "fault";
  if (barrier.state === "Open") return "open";
  if (barrier.state === "Closed") return "closed";
  return "moving";
}

export const GATE_STATE_LABEL = { open: "Open", closed: "Closed", moving: "Moving", fault: "Out of service" };

export const PHASE_LABEL = {
  ARRIVED: "At entry", ASSIGNED: "Driving in", PARKED: "Parked", EXIT_REQUESTED: "Leaving",
  AT_EXIT: "At exit", CHARGED: "Awaiting payment", COMPLETED: "Done",
};

export const TYPE_GLYPH = { Electric: "⚡", Accessible: "♿" };
