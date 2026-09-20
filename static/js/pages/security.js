import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";

// Two tables over one run: what the door refused (invalid / duplicated /
// tampered / unhandled) and what happened on site (double parking, payments
// asked for again, exits re-routed). Both are scoped to the current run, so a
// count here is something that is happening now, not history.
await initShell();

const KIND_LABEL = {
  invalid_signature: "Tampered or unsigned",
  duplicate_event: "Duplicate delivery",
  invalid_json: "Malformed request",
  unknown_event_class: "Unknown event type",
  handler_failure: "Processing failure",
  double_park: "Double parking",
  crowded_bay: "Two cars in one bay",
  payment_retry: "Payment re-requested",
  payment_unresolved: "Payment unresolved",
  exit_failover: "Exit re-routed",
};
const label = (kind) => KIND_LABEL[kind] || kind;

let secKind = "";
let secPage = 1;
let incKind = "";
let incPage = 1;

function filterRow(host, kinds, current, onPick) {
  host.replaceChildren(...[["", "All"], ...kinds].map(([kind, text]) =>
    h("button", {
      class: `btn${current === kind ? " is-active" : ""}`, text,
      onclick: () => onPick(kind),
    })));
}

async function loadSecurity() {
  const data = await api(`/api/security?kind=${encodeURIComponent(secKind)}&page=${secPage}&size=50`);
  const totals = data.totals || [];
  const gaps = data.sequence_gaps || {};
  document.getElementById("sec-count").textContent = totals.reduce((n, r) => n + (r.occurrences || 0), 0) || "";

  const cards = totals.map((row) => h("div", { class: "summary-card has-faults" },
    h("div", { class: "summary-title", text: label(row.kind) }),
    h("div", { class: "summary-figure num" }, h("b", { text: String(row.occurrences) })),
    h("div", { class: "summary-detail muted", text: `${row.rows} distinct · last ${row.last_seen?.slice(11, 19) || "—"}` })));
  cards.push(h("div", { class: `summary-card${gaps.rows ? " has-faults" : ""}` },
    h("div", { class: "summary-title", text: "Sequence gaps" }),
    h("div", { class: "summary-figure num" }, h("b", { text: String(gaps.missing || 0) })),
    h("div", { class: "summary-detail muted", text: `${gaps.rows || 0} break(s) in the event stream` })));
  document.getElementById("sec-summary").replaceChildren(...cards);

  filterRow(document.getElementById("sec-kinds"), totals.map((r) => [r.kind, label(r.kind)]), secKind,
    (kind) => { secKind = kind; secPage = 1; loadSecurity(); });

  document.getElementById("sec-rows").replaceChildren(...data.items.map((row) => h("tr", {},
    h("td", { text: label(row.kind) }),
    h("td", { text: row.event_class || row.event_id || "—" }),
    h("td", { text: row.detail || "" }),
    h("td", { class: "num", text: String(row.occurrences) }),
    h("td", { text: (row.first_seen || "").replace("T", " ").slice(0, 19) }),
    h("td", { text: (row.last_seen || "").replace("T", " ").slice(0, 19) }))));
  if (!data.items.length) {
    document.getElementById("sec-rows").replaceChildren(
      h("tr", {}, h("td", { colspan: 6, text: "Nothing has been refused this run." })));
  }
  document.getElementById("sec-page").textContent = `${data.total} row(s) · page ${secPage}`;
  document.getElementById("sec-prev").disabled = secPage === 1;
  document.getElementById("sec-next").disabled = data.items.length < 50;
}

async function loadIncidents() {
  const data = await api(`/api/incidents?kind=${encodeURIComponent(incKind)}&page=${incPage}&size=50`);
  const open = (data.by_kind || []).reduce((n, r) => n + (r.open || 0), 0);
  document.getElementById("inc-count").textContent = open || "";
  filterRow(document.getElementById("inc-kinds"),
    (data.by_kind || []).map((r) => [r.kind, `${label(r.kind)} (${r.rows})`]), incKind,
    (kind) => { incKind = kind; incPage = 1; loadIncidents(); });

  document.getElementById("inc-rows").replaceChildren(...data.items.map((row) => h("tr", {},
    h("td", { text: (row.occurred_at || "").replace("T", " ").slice(0, 19) }),
    h("td", { text: label(row.kind) }),
    h("td", { text: row.plate || "—" }),
    h("td", { text: row.detail || "" }),
    h("td", { text: row.resolved_at ? `Resolved ${row.resolved_at.slice(11, 19)}` : "Open" }))));
  if (!data.items.length) {
    document.getElementById("inc-rows").replaceChildren(
      h("tr", {}, h("td", { colspan: 5, text: "No incidents this run." })));
  }
  document.getElementById("inc-page").textContent = `${data.total} row(s) · page ${incPage}`;
  document.getElementById("inc-prev").disabled = incPage === 1;
  document.getElementById("inc-next").disabled = data.items.length < 50;
}

document.getElementById("sec-prev").onclick = () => { secPage--; loadSecurity(); };
document.getElementById("sec-next").onclick = () => { secPage++; loadSecurity(); };
document.getElementById("inc-prev").onclick = () => { incPage--; loadIncidents(); };
document.getElementById("inc-next").onclick = () => { incPage++; loadIncidents(); };

await Promise.all([loadSecurity(), loadIncidents()]);
setInterval(() => { loadSecurity(); loadIncidents(); }, 15000);
