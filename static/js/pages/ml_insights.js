import { h, plate } from "../core/dom.js";
import { timeAgo } from "../core/format.js";
import { connect, subscribe } from "../core/live.js";
import { initShell } from "../core/shell.js";

const me = await initShell({ usesLiveSocket: true });

function row(...cells) {
  return h("tr", {}, ...cells.map(c => h("td", {}, c)));
}

function render(insights) {
  const ventilation = [...(insights.ventilation || [])].sort((a, b) => a.minutes_to_threshold - b.minutes_to_threshold);
  const ventBody = document.getElementById("mi-ventilation");
  ventBody.replaceChildren(...ventilation.map(v => row(
    v.zone,
    h("span", { class: `ml-metric ${v.minutes_to_threshold <= 10 ? "is-bad" : ""}`,
      text: v.minutes_to_threshold >= 9999 ? "Stable" : `${v.minutes_to_threshold} min → ${v.predicted_ppm} ppm` }),
  )));
  if (!ventilation.length) ventBody.replaceChildren(row(h("span", { text: "No zone CO data yet." })));

  if ("components" in insights) {
    const components = insights.components || []; // already sorted by days_to_failure server-side
    const compBody = document.getElementById("mi-components");
    compBody.replaceChildren(...components.map(c => row(
      c.name, c.type,
      h("span", { class: `ml-metric ${c.days_to_failure <= 3 ? "is-bad" : ""}`, text: `${c.days_to_failure}d` }),
      `${Math.round(c.failure_probability * 100)}%`,
    )));
    if (!components.length) compBody.replaceChildren(row(h("span", { text: "No component wear data yet." })));
  }

  const anomalies = insights.anomalies || [];
  const anomBody = document.getElementById("mi-anomalies");
  anomBody.replaceChildren(...anomalies.map(a => row(
    plate(a.plate), a.gate || "—",
    a.fallback_charge != null ? `$${Number(a.fallback_charge).toFixed(2)}` : "—",
    timeAgo(a.resolved_at),
  )));
  if (!anomalies.length) anomBody.replaceChildren(row(h("span", { text: "No resolved ghost cars yet." })));
}

subscribe(snap => {
  if (snap.role && snap.role !== me.role) { location.reload(); return; }
  render(snap.ml_insights || {});
});
connect();
