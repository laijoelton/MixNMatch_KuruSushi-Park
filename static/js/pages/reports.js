import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";
await initShell();
const data = await api("/api/reports/daily");
function card(label, value) { return h("section", { class: "kpi" }, h("div", { class: "kpi-label", text: label }), h("div", { class: "kpi-value", text: value })); }
document.getElementById("r-cards").replaceChildren(
  card("CO mitigation events", data.co_mitigation_events), card("Preventive repairs", data.preventive_repairs),
  card("Unexpected breakdowns", data.unexpected_breakdowns), card("Open vehicle alerts", data.ghost_car_events_open));
document.getElementById("r-throughput").replaceChildren(...data.throughput_by_zone.map(row => h("tr", {}, h("td", { text: row.zone }), h("td", { text: row.sessions }))));
if (data.revenue) {
  document.getElementById("r-revenue-wrap").hidden = false;
  document.getElementById("r-revenue-cards").replaceChildren(...Object.entries(data.revenue).map(([label, value]) => card(label.replaceAll("_", " "), Number(value).toFixed(2))));
}
