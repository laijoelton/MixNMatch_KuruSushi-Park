import { api } from "../core/api.js";
import { wireClear } from "../core/clear.js";
import { clear, h, plate } from "../core/dom.js";
import { detailList, openDrawer } from "../core/drawer.js";
import { dateTime, money } from "../core/format.js";
import { initShell } from "../core/shell.js";

// Search completed stays. Filtering and paging happen in SQL on the server;
// the URL mirrors the filters so a view can be refreshed or shared.

const me = await initShell();
const canFinance = me.capabilities.includes("fin:view");

const PAGE_SIZE = 25;
const STATUS = canFinance ? [["", "All"], ["paid", "Paid"], ["suspect", "Suspect"], ["unpaid", "Unpaid"]] : [];
const form = document.getElementById("filters");
const rows = document.getElementById("rows");
const summary = document.getElementById("result-summary");
const prev = document.getElementById("prev");
const next = document.getElementById("next");
const statusGroup = document.getElementById("f-status");

const query = new URLSearchParams(location.search);
let status = canFinance ? query.get("status") || "" : "";
let page = Math.max(1, Number(query.get("page")) || 1);
form.plate.value = query.get("plate") || "";
form.spot.value = query.get("spot") || "";
form.from.value = query.get("from") || "";
form.to.value = query.get("to") || "";

const statusChips = STATUS.map(([value, label]) => h("button", {
  type: "button", class: `chip${value === status ? " is-on" : ""}`, text: label, "aria-pressed": String(value === status),
  onclick: () => { status = value; refreshChips(); page = 1; load(); },
}));
statusGroup.append(...statusChips);

function refreshChips() {
  statusChips.forEach((chip, i) => {
    const on = STATUS[i][0] === status;
    chip.classList.toggle("is-on", on);
    chip.setAttribute("aria-pressed", String(on));
  });
}

// Local calendar day -> UTC ISO range, matching how completed_at is stored.
function dayBoundary(value, endOfDay) {
  if (!value) return "";
  const [y, m, d] = value.split("-").map(Number);
  const date = endOfDay ? new Date(y, m - 1, d, 23, 59, 59, 999) : new Date(y, m - 1, d);
  return date.toISOString();
}

function statusTag(row) {
  if (row.payment_ok === 1) return h("span", { class: "tag free", text: "Paid" });
  if (row.payment_ok === 0) return h("span", { class: "tag fault", text: "Suspect" });
  return h("span", { class: "tag reserved", text: "Unpaid" });
}

function skeleton() {
  clear(rows);
  for (let i = 0; i < 6; i++) {
    rows.append(h("tr", { class: "skeleton" }, Array.from({ length: 9 }, () => h("td", {}, h("span")))));
  }
}

let requestId = 0;
async function load() {
  const params = new URLSearchParams();
  if (form.plate.value.trim()) params.set("plate", form.plate.value.trim());
  if (form.spot.value.trim()) params.set("spot", form.spot.value.trim());
  if (status) params.set("status", status);
  if (form.from.value) params.set("from", form.from.value);
  if (form.to.value) params.set("to", form.to.value);
  if (page > 1) params.set("page", page);
  history.replaceState(null, "", `${location.pathname}${params.toString() ? `?${params}` : ""}`);

  const apiParams = new URLSearchParams(params);
  apiParams.set("page", page);
  apiParams.set("size", PAGE_SIZE);
  if (form.from.value) apiParams.set("from", dayBoundary(form.from.value, false));
  if (form.to.value) apiParams.set("to", dayBoundary(form.to.value, true));

  const mine = ++requestId;
  skeleton();
  let data;
  try {
    data = await api(`/api/history/search?${apiParams}`);
  } catch {
    if (mine !== requestId) return;
    clear(rows).append(h("tr", {}, h("td", { colspan: 9 },
      h("div", { class: "empty" }, h("b", { text: "Could not load history" }),
        h("button", { class: "btn small", type: "button", text: "Try again", onclick: load })))));
    summary.textContent = "—";
    return;
  }
  if (mine !== requestId) return;  // a newer search already started

  clear(rows);
  if (!data.items.length) {
    const filtered = [...params.keys()].some((k) => k !== "page");
    rows.append(h("tr", {}, h("td", { colspan: 9 }, h("div", { class: "empty" },
      h("b", { text: filtered ? "No stays match these filters" : "No completed stays yet" }),
      h("span", { text: filtered ? "Try a shorter plate or a wider date range." : "Stays appear here once a car has paid and left." })))));
  }
  for (const row of data.items) {
    rows.append(h("tr", { class: "clickable", tabindex: 0, onclick: () => showTimeline(row), onkeydown: (e) => e.key === "Enter" && showTimeline(row) },
      h("td", {}, plate(row.plate)),
      h("td", { text: row.car_type || "—" }),
      h("td", { class: "num", style: "text-align:left", text: row.spot || "—" }),
      h("td", { text: dateTime(row.arrived_at) }),
      h("td", { text: dateTime(row.left_spot_at || row.completed_at) }),
      h("td", { class: "num", text: row.minutes != null ? `${Number(row.minutes).toFixed(1)} min` : "—" }),
      canFinance ? h("td", { class: "num", text: money((row.parking_cost || 0) + (row.charging_cost || 0)) }) : null,
      canFinance ? h("td", { class: "num", text: money(row.paid_amount) }) : null,
      canFinance ? h("td", {}, statusTag(row)) : null));
  }

  const first = data.total ? (data.page - 1) * data.size + 1 : 0;
  const last = Math.min(data.total, data.page * data.size);
  summary.textContent = `${first}–${last} of ${data.total} stays`;
  prev.disabled = data.page <= 1;
  next.disabled = last >= data.total;
}

async function showTimeline(row) {
  const list = h("ol", { class: "timeline" });
  openDrawer({
    kicker: "Stay timeline",
    title: row.plate,
    body: [
      detailList([
        ["Bay", row.spot || "—"],
        ["Entry → exit", `${row.entry_gate || "—"} → ${row.exit_gate || "—"}`],
        ["Actual parked time", row.minutes != null ? `${Number(row.minutes).toFixed(1)} min` : "—"],
        ["Booked stay", row.planned_minutes != null ? `${row.planned_minutes} min` : "—"],
        canFinance ? ["Charged", money((row.parking_cost || 0) + (row.charging_cost || 0))] : null,
        canFinance ? ["Paid", money(row.paid_amount)] : null,
        canFinance ? ["Verdict", statusTag(row)] : null,
      ]),
      h("h3", { class: "drawer-kicker", style: "margin:8px 0 0", text: "Events" }),
      list,
    ],
  });
  try {
    const events = await api(`/api/history/timeline?plate=${encodeURIComponent(row.plate)}`);
    if (!events.length) list.append(h("li", {}, h("div", { class: "t-title", text: "No raw events stored for this plate" })));
    for (const event of events) list.append(timelineItem(event));
  } catch {
    list.append(h("li", { class: "fault" }, h("div", { class: "t-title", text: "Could not load events" })));
  }
}

function timelineItem(event) {
  const p = event.payload || {};
  let title = event.event_class;
  let tone = "";
  if (event.event_class === "car_spot_action") title = `${p.Direction === "CarIn" ? "Reached" : "Left"} ${p.SpotName} (${p.SpotType})`;
  if (event.event_class === "payment_made") { title = `Paid ${p.Amount}`; tone = "money"; }
  if (event.event_class === "penalty") { title = `Penalty: ${p.Reason}`; tone = "fault"; }
  return h("li", { class: tone },
    h("div", { class: "t-title", text: title }),
    h("div", { class: "t-meta", text: `#${event.sequence_id ?? "—"} · ${p.ServerDateTime || dateTime(event.received_at)}` }));
}

let debounce;
form.addEventListener("input", (event) => {
  if (event.target.type === "date") return;
  clearTimeout(debounce);
  debounce = setTimeout(() => { page = 1; load(); }, 300);
});
form.addEventListener("change", (event) => { if (event.target.type === "date") { page = 1; load(); } });
form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; load(); });
form.addEventListener("reset", () => setTimeout(() => { status = ""; refreshChips(); page = 1; load(); }));
prev.addEventListener("click", () => { page -= 1; load(); });
next.addEventListener("click", () => { page += 1; load(); });
wireClear("clear-data", "history", "history", () => { page = 1; return load(); });

load();
