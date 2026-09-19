import { api } from "../core/api.js";
import { clear, emptyState, h, plate, setText } from "../core/dom.js";
import { dateTime, money } from "../core/format.js";
import { initShell } from "../core/shell.js";

// Admin view of money: revenue, fines, and every payment with a verdict.
// The docs warn some cars send fake payments; the dispatcher compares each
// payment_made amount with the invoice and holds the car when they differ.

await initShell();

const REFRESH_MS = 5000;
const FILTERS = [["all", "All"], ["valid", "Verified"], ["suspect", "Suspect"]];
let filter = "all";
let payments = [];

const kpiHost = document.getElementById("pay-kpis");
const kpi = (label) => {
  const value = h("span", { text: "—" });
  const sub = h("div", { class: "kpi-sub" });
  const el = h("section", { class: "kpi" }, h("div", { class: "kpi-label", text: label }), h("div", { class: "kpi-value" }, value), sub);
  kpiHost.append(el);
  return { el, value, sub };
};
const revenue = kpi("Revenue (verified)");
const net = kpi("Net after fines");
const suspect = kpi("Suspect payments");
const fines = kpi("Fines");

const chips = FILTERS.map(([value, label]) => h("button", {
  type: "button", class: `chip${value === filter ? " is-on" : ""}`, text: label,
  onclick: () => { filter = value; chips.forEach((c, i) => c.classList.toggle("is-on", FILTERS[i][0] === filter)); renderRows(); },
}));
document.getElementById("pay-filters").append(...chips);

function reason(row) {
  if (row.valid) return "Amount matches invoice";
  if (row.expected == null) return "No invoice issued for this car";
  return `Expected ${money(row.expected)}, got ${money(row.amount)}`;
}

function renderRows() {
  const body = document.getElementById("pay-rows");
  const shown = payments.filter((p) => filter === "all" || (filter === "valid" ? p.valid : !p.valid));
  setText(document.getElementById("pay-count"), shown.length || "");
  clear(body);
  if (!shown.length) {
    body.append(h("tr", {}, h("td", { colspan: 6 }, emptyState(payments.length ? "Nothing in this filter" : "No payments yet",
      payments.length ? null : "Payments appear when cars pay at the exit."))));
    return;
  }
  for (const row of shown) {
    body.append(h("tr", {},
      h("td", { class: "num", style: "text-align:left", text: row.server_datetime || "—" }),
      h("td", {}, plate(row.plate)),
      h("td", { class: "num", text: money(row.expected) }),
      h("td", { class: "num", text: money(row.amount) }),
      h("td", {}, h("span", { class: `tag ${row.valid ? "free" : "fault"}`, text: row.valid ? "Verified" : "Suspect" })),
      h("td", { class: "muted", text: reason(row) })));
  }
}

async function load() {
  try {
    const [stats, rows] = await Promise.all([api("/api/stats", { quiet: true }), api("/api/payments?limit=200", { quiet: true })]);
    payments = rows;
    setText(revenue.value, money(stats.revenue));
    setText(revenue.sub, `${stats.completed_sessions} completed stays`);
    setText(net.value, money(stats.net));
    net.el.classList.toggle("is-bad", stats.net < 0);
    setText(suspect.value, stats.suspect_payments);
    setText(suspect.sub, stats.suspect_payments ? "Cars held at the exit" : "None detected");
    suspect.el.classList.toggle("is-bad", stats.suspect_payments > 0);
    setText(fines.value, money(stats.total_fines));
    setText(fines.sub, `${stats.penalty_count} penalties`);

    const list = document.getElementById("fines");
    clear(list);
    if (!stats.fines_by_reason?.length) list.append(emptyState("No fines", "Nothing has been penalised."));
    for (const f of stats.fines_by_reason || []) {
      list.append(h("div", { class: "list-row" },
        h("span", { class: "sev bad" }),
        h("span", { class: "grow", text: f.reason, title: f.reason }),
        h("span", { class: "num muted", text: `×${f.count}` }),
        h("span", { class: "num", text: `−${money(f.total)}` })));
    }
    renderRows();
  } catch {
    /* api() reports errors; keep showing the last data */
  }
}

await load();
setInterval(load, REFRESH_MS);
