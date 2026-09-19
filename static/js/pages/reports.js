import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";
import { connect, subscribe } from "../core/live.js";
const me = await initShell();
let loading = false;
let lastUpdated = 0;
function card(label, value) { return h("section", { class: "kpi" }, h("div", { class: "kpi-label", text: label }), h("div", { class: "kpi-value", text: value })); }
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const data = await api("/api/reports/daily");
    document.getElementById("r-cards").replaceChildren(
      card("CO mitigation events", data.co_mitigation_events), card("Preventive repairs", data.preventive_repairs),
      card("Unexpected breakdowns", data.unexpected_breakdowns), card("Open vehicle alerts", data.ghost_car_events_open));
    document.getElementById("r-throughput").replaceChildren(...data.throughput_by_zone.map(row =>
      h("tr", {}, h("td", { text: row.zone }), h("td", { text: row.sessions }))));
    if (!data.throughput_by_zone.length) document.getElementById("r-throughput").replaceChildren(
      h("tr", {}, h("td", { colspan: 2, text: "No completed parking sessions today." })));
    document.getElementById("r-revenue-wrap").hidden = !data.revenue;
    document.getElementById("r-revenue-cards").replaceChildren();
    if (data.revenue) {
      document.getElementById("r-revenue-cards").replaceChildren(...Object.entries(data.revenue).map(([label, value]) =>
        card(label.replaceAll("_", " "), Number(value).toFixed(2))));
    }
    lastUpdated = Date.now();
    document.getElementById("r-updated").textContent = `Updated ${new Date(lastUpdated).toLocaleTimeString()} · refreshes while connected`;
  } catch {
    document.getElementById("r-updated").textContent = "Could not refresh. Showing the last report; try again.";
  } finally {
    loading = false;
  }
}
document.getElementById("r-refresh").addEventListener("click", refresh);
await refresh();
subscribe(snap => {
  if (snap.role && snap.role !== me.role) { location.reload(); return; }
  if (Date.now() - lastUpdated >= 5000) void refresh();
});
connect();
