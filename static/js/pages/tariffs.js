import { api } from "../core/api.js";
import { h } from "../core/dom.js";
import { initShell } from "../core/shell.js";
const me = await initShell();
const data = await api("/api/tariffs");
const fields = new Map();
const host = document.getElementById("tariff-fields");
for (const [key, value] of Object.entries(data.settings)) {
  let input;
  if (typeof value === "string") {
    const values = key === "billing_basis" ? ["planned", "measured"] : ["round", "ceil", "exact"];
    input = h("select", { class: "select", id: key }, values.map(v => h("option", { value: v, text: v, selected: v === value })));
  } else if (typeof value === "boolean") input = h("input", { type: "checkbox", checked: value, id: key });
  else input = h("input", { class: "input", type: "number", min: 0, step: "any", value, required: true, id: key });
  input.disabled = !me.capabilities.includes("fin:write_tariff");
  fields.set(key, { input, type: typeof value });
  host.append(h("div", { class: "field" }, h("label", { for: key, text: key.replaceAll("_", " ") }), input));
}
document.getElementById("tariff-form").onsubmit = async event => {
  event.preventDefault();
  const body = {};
  for (const [key, { input, type }] of fields) body[key] = type === "boolean" ? input.checked : type === "number" ? Number(input.value) : input.value;
  await api("/api/tariffs", { method: "PUT", body });
  document.getElementById("tariff-result").textContent = "Tariffs saved. New invoices use these settings.";
};
