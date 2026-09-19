import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";
import { confirmAction } from "../core/toast.js";
const me = await initShell();
const tabs = { operations: "logs:view_ops", maintenance: "logs:view_maint", financial: "logs:view_fin", audit: "logs:view_audit", logins: "logs:view_audit", unsigned: "logs:view_audit" };
let tab = Object.keys(tabs).find(t => me.capabilities.includes(tabs[t]));
let page = 1;
const host = document.getElementById("log-tabs");
for (const [name, cap] of Object.entries(tabs)) {
  if (me.capabilities.includes(cap)) host.append(h("button", { class: "btn", text: name, onclick: () => { tab = name; page = 1; load(); } }));
}
async function load() {
  const data = await api(`/api/logs?tab=${tab}&page=${page}&size=50`);
  document.getElementById("log-rows").replaceChildren(...data.items.map(row => h("tr", {},
    h("td", { text: row.received_at || row.occurred_at || new Date(row.at * 1000).toLocaleString() }),
    h("td", { text: row.event_class || row.path || row.username || row.reason }),
    h("td", {}, h("pre", { style: "white-space:pre-wrap;overflow-wrap:anywhere", text: JSON.stringify(row.payload || row, null, 2) })))));
  document.getElementById("log-page").textContent = `${tab} · Page ${page}`;
  document.getElementById("log-prev").disabled = page === 1;
  document.getElementById("log-next").disabled = data.items.length < 50;
}
document.getElementById("log-prev").onclick = () => { page--; load(); };
document.getElementById("log-next").onclick = () => { page++; load(); };
document.getElementById("log-flush").onclick = async () => {
  if (await confirmAction({ title: `Flush ${tab} log?`, message: "Detailed log records will be removed. Financial records and event identifiers are retained.", confirmLabel: "Flush", danger: true })) {
    await api(`/api/logs?tab=${tab}`, { method: "DELETE" }); page = 1; await load();
  }
};
await load();
