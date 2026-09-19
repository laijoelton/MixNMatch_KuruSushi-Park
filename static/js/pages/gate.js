import { h, keyedList, setText } from "../core/dom.js";
import { TYPE_GLYPH } from "../core/format.js";

// Driver kiosk at the entrance. Public (no sign-in), so it reads the public
// bay list over REST instead of the operator WebSocket. Only bays that suit
// the chosen vehicle type can be picked - the same rule the dispatcher uses
// (a bay for "Any" car, or one reserved for this car's type).

const POLL_MS = 2000;
const bayHost = document.getElementById("k-bays");
const form = document.getElementById("k-form");
const submit = document.getElementById("k-submit");
const plateInput = document.getElementById("k-plate");
const gateSelect = document.getElementById("k-gate");
const statusPill = document.getElementById("kiosk-status");
const result = document.getElementById("k-result");

let carType = "Normal";
let selected = null;
let bays = [];
const zoneGrids = new Map();

function suits(bay) {
  return bay.car_type === "Any" || bay.car_type === carType;
}

for (const button of document.querySelectorAll("#k-type button")) {
  button.addEventListener("click", () => {
    carType = button.dataset.type;
    for (const b of document.querySelectorAll("#k-type button")) {
      b.classList.toggle("is-on", b === button);
      b.setAttribute("aria-pressed", String(b === button));
    }
    render();
  });
}

function render() {
  if (selected && !bays.some((b) => b.name === selected && b.available && suits(b))) selected = null;

  const zones = [...new Set(bays.map((b) => b.zone || "Car park"))];
  for (const [zone, section] of zoneGrids) if (!zones.includes(zone)) { section.remove(); zoneGrids.delete(zone); }
  if (!bays.length) {
    bayHost.replaceChildren(h("div", { class: "empty" }, h("b", { text: "No bays available yet" }), h("span", { text: "Please wait — the car park is starting up." })));
    zoneGrids.clear();
  } else if (!zoneGrids.size) {
    bayHost.replaceChildren();
  }

  for (const zone of zones) {
    if (!zoneGrids.has(zone)) {
      const grid = h("div", { class: "bay-grid" });
      const section = h("section", { class: "bay-zone" }, h("h3", { text: zone }), grid);
      section._grid = grid;
      bayHost.append(section);
      zoneGrids.set(zone, section);
    }
    const inZone = bays.filter((b) => (b.zone || "Car park") === zone);
    keyedList(zoneGrids.get(zone)._grid, inZone, (b) => b.name, renderBay, fillBay);
  }

  submit.disabled = !selected;
  setText(submit, selected ? `Check in to bay ${selected}` : "Select a bay");
}

// Buttons are reused across polls, so a tap is never lost to a refresh.
function renderBay(bay) {
  const el = h("button", { type: "button", class: "bay" },
    h("span", { class: "name" }), h("span", { class: "t", "aria-hidden": "true" }));
  el.addEventListener("click", () => {
    if (el.disabled) return;
    selected = el._bay.name;
    render();
  });
  fillBay(el, bay);
  return el;
}

function fillBay(el, bay) {
  el._bay = bay;
  const usable = bay.available && suits(bay);
  el.disabled = !usable;
  el.classList.toggle("is-selected", bay.name === selected);
  setText(el.querySelector(".name"), bay.name);
  setText(el.querySelector(".t"), TYPE_GLYPH[bay.car_type] || "");
  el.setAttribute("aria-label", `Bay ${bay.name}${bay.car_type !== "Any" ? `, ${bay.car_type}` : ""}${usable ? "" : ", unavailable"}`);
}

async function poll() {
  try {
    const response = await fetch("/api/gate/bays");
    if (!response.ok) throw new Error();
    bays = await response.json();
    statusPill.className = "pill is-live";
    setText(statusPill, `${bays.filter((b) => b.available && suits(b)).length} free`);
    render();
  } catch {
    statusPill.className = "pill is-bad";
    setText(statusPill, "Offline");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const plate = plateInput.value.trim().toUpperCase();
  if (!selected) return;
  if (!plate) {
    plateInput.focus();
    showResult(false, "Enter your number plate first.");
    return;
  }
  submit.setAttribute("aria-busy", "true");
  submit.disabled = true;
  try {
    const response = await fetch("/api/gate/checkin", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plate, gate: gateSelect.value, spot: selected, car_type: carType }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "That bay was just taken — please pick another.");
    showResult(true, `Drive to bay ${data.target}. The gate is opening.`);
    plateInput.value = "";
    selected = null;
  } catch (error) {
    showResult(false, error.message === "Failed to fetch" ? "The system is offline. Please ask staff for help." : error.message);
  } finally {
    submit.removeAttribute("aria-busy");
    await poll();
  }
});

function showResult(ok, message) {
  result.replaceChildren(h("div", { class: `result-card${ok ? "" : " error"}` }, h("b", { text: ok ? "You're checked in" : "Not checked in" }), h("div", { text: message })));
}

await poll();
setInterval(poll, POLL_MS);
