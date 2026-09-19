import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";
await initShell();
const form = document.getElementById("p-filters");
async function load() {
  const rows = await api(`/api/penalties?code=${encodeURIComponent(form.code.value)}`);
  document.getElementById("p-rows").replaceChildren(...rows.map(r => h("tr", {},
    ...[r.server_datetime, r.reason, r.type, r.component_name, Number(r.fine_amount).toFixed(2)].map(text => h("td", { text })))));
  document.getElementById("p-summary").textContent = `${rows.length} penalties`;
}
form.onsubmit = event => { event.preventDefault(); load(); };
form.onreset = () => setTimeout(load, 0);
await load();
