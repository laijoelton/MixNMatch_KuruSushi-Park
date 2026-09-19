import { clear, h, s } from "../core/dom.js";
import { GATE_STATE_LABEL, SPOT_STATE_LABEL, TYPE_GLYPH, gateState, naturalCompare, spotState } from "../core/format.js";

// Digital twin of the car park, drawn in the simulator's own world
// coordinates (from /api/twin) so it matches the game screen. Geometry is
// drawn once per level; each live snapshot only flips CSS classes.
//
//   createTwin(panel, { onSelect(kind, name) }) ->
//     { setGeometry(geo), update(snapshot), focusZone(name|null), fit(), select(kind, name) }

const BAY_W = 80;           // uniform footprint: the level files' Width/Height are inconsistent
const BAY_H = 170;
const PADDING = 160;
const MIN_PIXELS_PER_UNIT_FOR_LABELS = 0.32;

export function createTwin(panel, { onSelect } = {}) {
  const zoneChips = h("div", { class: "zone-chips", style: "display:flex;gap:6px;flex-wrap:wrap" });
  const zoomIn = h("button", { type: "button", "aria-label": "Zoom in", text: "+" });
  const zoomOut = h("button", { type: "button", "aria-label": "Zoom out", text: "−" });
  const fitBtn = h("button", { type: "button", class: "fit", text: "Fit" });
  const toolbar = h("div", { class: "twin-toolbar" },
    h("h2", { class: "sr-only", text: "Car park map" }),
    zoneChips, h("div", { class: "spacer" }),
    h("div", { class: "zoom-group", role: "group", "aria-label": "Zoom" }, zoomOut, zoomIn, fitBtn));

  const svg = s("svg", { class: "twin-root", role: "img", "aria-label": "Live car park map" });
  const legend = h("div", { class: "twin-legend", "aria-hidden": "true" },
    legendItem("var(--free)", "Free"), legendItem("var(--occupied)", "Occupied"),
    legendItem("var(--reserved)", "Reserved"), legendItem("var(--fault)", "Out of service"),
    h("span", { text: "⚡ EV" }), h("span", { text: "♿ Accessible" }));
  const notice = h("div", { class: "twin-notice", hidden: true });
  const stage = h("div", { class: "twin-stage" }, svg, legend, notice);
  panel.append(toolbar, stage);

  // Layers, back to front.
  const defs = s("defs", {},
    s("pattern", { id: "tw-hatch", width: 18, height: 18, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      s("rect", { width: 7, height: 18, fill: "rgba(240,87,110,0.35)" })));
  const layers = {
    zones: s("g"), routes: s("g"), bays: s("g"), points: s("g"), gates: s("g"), fans: s("g"),
  };
  svg.append(defs, layers.zones, layers.bays, layers.routes, layers.points, layers.gates, layers.fans);

  let geometry = null;
  let view = null;            // current viewBox {x, y, w, h}
  let home = null;            // fitted viewBox
  let focusedZone = null;
  let selected = null;        // {kind, name}
  const bays = new Map();     // name -> {el, spot, state}
  const gates = [];           // [{el, gate}]  (a list: level 3 has two gates named "gate7")
  const fans = new Map();
  const points = new Map();   // entry/exit spot name -> world position
  const zoneRects = new Map();

  // ------------------------------------------------------------ geometry
  function setGeometry(geo, snapshotForFallback) {
    geometry = geo && geo.level ? geo : schematic(snapshotForFallback);
    notice.hidden = Boolean(geo && geo.level);
    notice.textContent = "Layout not recognised — showing a schematic view";
    for (const layer of Object.values(layers)) clear(layer);
    bays.clear(); gates.length = 0; fans.clear(); points.clear(); zoneRects.clear();

    for (const zone of geometry.zones) drawZone(zone);
    for (const spot of geometry.spots) {
      if (spot.purpose === "Park") drawBay(spot);
      else if (spot.purpose === "EntrySpot" || spot.purpose === "ExitSpot") drawPoint(spot);
    }
    geometry.gates.forEach(drawGate);
    geometry.fans.forEach(drawFan);
    renderZoneChips();
    home = boundsToView(geometry.bounds);
    setView(home);
  }

  function drawZone(zone) {
    const x = zone.x - zone.w / 2;
    const y = zone.y - zone.h / 2;
    const indoor = zone.type === "Closed";
    const rect = s("rect", { class: `tw-zone${indoor ? " closed" : ""}`, x, y, width: zone.w, height: zone.h, rx: 24 });
    layers.zones.append(rect,
      // Inset past the corner fans so the name stays readable.
      s("text", { class: "tw-zone-label", x: x + 110, y: y + 64, text: zone.name }),
      indoor ? s("text", { class: "tw-zone-sub", x: x + 110, y: y + 104, text: "INDOOR · ventilated" }) : null);
    zoneRects.set(zone.name, { rect, x, y, w: zone.w, h: zone.h });
  }

  function drawBay(spot) {
    const deg = (spot.rotation * 180) / Math.PI;
    const rotated = Math.abs(deg) > 5;
    const zone = geometry.zones.find((z) => z.name === spot.zone);
    // Leave the bay's mouth open towards the aisle (the zone's centre line).
    const opensDown = !zone || rotated ? null : spot.y < zone.y;
    const halfW = BAY_W / 2;
    const halfH = BAY_H / 2;
    const paint = opensDown === null
      ? `M${-halfW},${-halfH} H${halfW} V${halfH} H${-halfW} Z`
      : opensDown
        ? `M${-halfW},${halfH} V${-halfH} H${halfW} V${halfH}`
        : `M${-halfW},${-halfH} V${halfH} H${halfW} V${-halfH}`;
    const glyph = TYPE_GLYPH[spot.car_type];
    const title = s("title", { text: spot.name });

    const el = s("g", {
      class: "tw-bay st-free", transform: `translate(${spot.x} ${spot.y}) rotate(${deg})`,
      tabindex: 0, role: "button", "aria-label": `Bay ${spot.name}`,
    },
      title,
      s("rect", { class: "floor", x: -halfW, y: -halfH, width: BAY_W, height: BAY_H, rx: 6 }),
      s("rect", { class: "hatch", x: -halfW, y: -halfH, width: BAY_W, height: BAY_H, fill: "url(#tw-hatch)" }),
      carShape(),
      glyph ? s("text", { class: "glyph", x: 0, y: -10, fill: spot.car_type === "Electric" ? "var(--ev)" : "var(--acc)", text: glyph }) : null,
      s("g", { class: "wrench" },
        s("circle", { r: 24, fill: "var(--fault)" }),
        s("text", { class: "glyph", y: 1, fill: "#fff", style: "font-size:30px", text: "!" })),
      s("path", { class: "paint", d: paint }),
      s("text", { class: "label", x: 0, y: opensDown === false ? -halfH - 14 : halfH + 30, text: spot.name }));

    el.addEventListener("click", () => onSelect?.("spot", spot.name));
    el.addEventListener("keydown", (event) => { if (event.key === "Enter") onSelect?.("spot", spot.name); });
    layers.bays.append(el);
    bays.set(spot.name, { el, spot, title, state: "free", seen: false });
  }

  function carShape() {
    return s("g", { class: "car" },
      s("rect", { class: "tw-car-body", x: -31, y: -68, width: 62, height: 136, rx: 18 }),
      s("rect", { class: "tw-car-glass", x: -24, y: -44, width: 48, height: 24, rx: 6 }),
      s("rect", { class: "tw-car-roof", x: -24, y: -16, width: 48, height: 46, rx: 8 }),
      s("rect", { class: "tw-car-glass", x: -22, y: 34, width: 44, height: 16, rx: 5 }));
  }

  function drawPoint(spot) {
    const kind = spot.purpose === "EntrySpot" ? "entry" : "exit";
    const el = s("g", { class: `tw-point ${kind}`, transform: `translate(${spot.x} ${spot.y})` },
      s("title", { text: `${kind === "entry" ? "Entry" : "Exit"} ${spot.name}` }),
      s("circle", { r: 34 }),
      s("text", { y: 9, text: kind === "entry" ? "IN" : "OUT" }),
      s("text", { y: 72, style: "font-size:20px;fill:var(--muted)", text: spot.name }));
    layers.points.append(el);
    points.set(spot.name, { x: spot.x, y: spot.y, zone: spot.zone });
  }

  function drawGate(gate, index) {
    const deg = (gate.rotation * 180) / Math.PI;
    const title = s("title", { text: gate.name });
    const el = s("g", {
      class: "tw-gate closed", transform: `translate(${gate.x} ${gate.y}) rotate(${deg})`,
      tabindex: 0, role: "button", "aria-label": `Gate ${gate.name}`,
    },
      title,
      s("line", { class: "arm", x1: -8, y1: 0, x2: 96, y2: 0 }),
      s("circle", { class: "post", r: 12 }),
      s("text", { x: 44, y: -22, text: gate.name }));
    el.addEventListener("click", () => onSelect?.("gate", gate.name, index));
    el.addEventListener("keydown", (event) => { if (event.key === "Enter") onSelect?.("gate", gate.name, index); });
    layers.gates.append(el);
    gates.push({ el, gate, title });
  }

  function drawFan(fan) {
    const blade = "M0,-4 C10,-26 22,-18 6,-2 M4,0 C26,10 18,22 2,6 M0,4 C-10,26 -22,18 -6,2 M-4,0 C-26,-10 -18,-22 -2,-6";
    const el = s("g", { class: "tw-fan", transform: `translate(${fan.x} ${fan.y})` },
      s("title", { text: `Fan ${fan.name}` }),
      s("circle", { r: 30 }),
      s("path", { d: blade, stroke: "currentColor", "stroke-width": 7 }));
    el.addEventListener("click", () => onSelect?.("fan", fan.name));
    layers.fans.append(el);
    fans.set(fan.name, { el, fan });
  }

  // Level not recognised: still show every bay, grouped by zone, in a grid.
  function schematic(snapshot) {
    const spots = (snapshot?.spots || []).filter((sp) => sp.purpose === "Park");
    const byZone = new Map();
    for (const spot of spots) {
      const key = spot.zone || "Unzoned";
      if (!byZone.has(key)) byZone.set(key, []);
      byZone.get(key).push(spot);
    }
    const zones = [];
    const out = [];
    const perRow = 15;
    let top = 0;
    for (const [name, list] of [...byZone.entries()].sort((a, b) => naturalCompare(a[0], b[0]))) {
      list.sort((a, b) => naturalCompare(a.name, b.name));
      const rows = Math.ceil(list.length / perRow);
      const w = perRow * 110 + 200;
      const hgt = rows * 300 + 160;
      zones.push({ name, x: w / 2, y: top + hgt / 2, w, h: hgt, type: "Open" });
      list.forEach((spot, i) => out.push({
        name: spot.name, x: 150 + (i % perRow) * 110, y: top + 200 + Math.floor(i / perRow) * 300,
        rotation: 0, purpose: "Park", car_type: spot.car_type, zone: name,
      }));
      top += hgt + 120;
    }
    const maxW = Math.max(400, ...zones.map((z) => z.w));
    return {
      level: null, zones, spots: out, gates: [], fans: [], lights: [],
      bounds: { min_x: 0, max_x: maxW, min_y: 0, max_y: Math.max(400, top) },
    };
  }

  // ------------------------------------------------------------ live state
  function update(snapshot) {
    const spotByName = new Map((snapshot.spots || []).map((sp) => [sp.name, sp]));
    for (const [name, bay] of bays) {
      const live = spotByName.get(name);
      const next = live ? spotState(live) : "free";
      if (next !== bay.state) {
        bay.el.classList.remove(`st-${bay.state}`);
        bay.el.classList.add(`st-${next}`);
        if (bay.seen) {  // flash real changes, not the first paint
          bay.el.classList.remove("is-flash");
          void bay.el.getBoundingClientRect();  // force a reflow so the animation restarts
          bay.el.classList.add("is-flash");
        }
        bay.state = next;
      }
      bay.seen = true;
      const occupant = live?.occupant_plate ? ` · ${live.occupant_plate}` : "";
      bay.title.textContent = `${name} · ${SPOT_STATE_LABEL[next]}${occupant}`;
    }

    const barrierByName = new Map((snapshot.barriers || []).map((b) => [b.name, b]));
    for (const { el, gate, title } of gates) {
      const live = barrierByName.get(gate.name);
      const next = live ? gateState(live) : "closed";
      const waiting = live?.held_plates?.length ? live.held_plates : null;
      el.setAttribute("class", `tw-gate ${next}${waiting ? " needs-attention" : ""}${isSelected("gate", gate.name) ? " is-selected" : ""}`);
      title.textContent = `${gate.name} · ${GATE_STATE_LABEL[next]}${waiting ? ` · waiting: ${waiting.join(", ")} — open to let out` : ""}`;
    }

    const fanByName = new Map((snapshot.fans || []).map((f) => [f.name, f]));
    for (const [name, { el }] of fans) {
      const live = fanByName.get(name);
      const fault = live && (live.broken || live.under_maintenance);
      el.setAttribute("class", `tw-fan${live?.is_on ? " on" : ""}${fault ? " fault" : ""}`);
    }

    drawRoutes(snapshot.sessions || []);
  }

  // Cars that have been dispatched but not yet parked: entry -> aisle -> bay.
  function drawRoutes(sessions) {
    clear(layers.routes);
    const driving = sessions.filter((x) => x.phase === "ASSIGNED" && x.assigned_spot).slice(0, 12);
    for (const session of driving) {
      const bay = bays.get(session.assigned_spot);
      const from = points.get(session.entry_gate);
      if (!bay || !from) continue;
      const zone = geometry.zones.find((z) => z.name === bay.spot.zone);
      const aisleY = zone ? zone.y : (from.y + bay.spot.y) / 2;
      const d = `M${from.x},${from.y} V${aisleY} H${bay.spot.x} V${bay.spot.y}`;
      layers.routes.append(s("path", { class: "tw-route", d }));
    }
  }

  // ------------------------------------------------------------ selection
  function isSelected(kind, name) {
    return selected && selected.kind === kind && selected.name === name;
  }

  function select(kind, name) {
    for (const { el } of bays.values()) el.classList.remove("is-selected");
    selected = kind ? { kind, name } : null;
    if (kind === "spot" && bays.has(name)) {
      const bay = bays.get(name);
      bay.el.classList.add("is-selected");
      ensureVisible(bay.spot.x, bay.spot.y);
    }
    for (const { el, gate } of gates) el.classList.toggle("is-selected", isSelected("gate", gate.name));
  }

  // ------------------------------------------------------------ view / zoom
  function boundsToView(b) {
    if (!b) return { x: 0, y: 0, w: 1000, h: 600 };
    return { x: b.min_x - PADDING, y: b.min_y - PADDING, w: b.max_x - b.min_x + PADDING * 2, h: b.max_y - b.min_y + PADDING * 2 };
  }

  function setView(next) {
    view = next;
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    const pixelsPerUnit = Math.min(svg.clientWidth / view.w, svg.clientHeight / view.h) || 1;
    svg.classList.toggle("is-far", pixelsPerUnit < MIN_PIXELS_PER_UNIT_FOR_LABELS);
  }

  function zoomAt(factor, cx, cy) {
    if (!home) return;
    const minW = home.w / 6;
    const maxW = home.w * 1.6;
    const w = Math.min(maxW, Math.max(minW, view.w * factor));
    const applied = w / view.w;
    setView({ x: cx - (cx - view.x) * applied, y: cy - (cy - view.y) * applied, w, h: view.h * applied });
  }

  function toWorld(clientX, clientY) {
    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const ctm = svg.getScreenCTM();
    return ctm ? point.matrixTransform(ctm.inverse()) : { x: view.x + view.w / 2, y: view.y + view.h / 2 };
  }

  function ensureVisible(x, y) {
    const margin = 0.1;
    const inside = x > view.x + view.w * margin && x < view.x + view.w * (1 - margin)
      && y > view.y + view.h * margin && y < view.y + view.h * (1 - margin);
    if (!inside) setView({ ...view, x: x - view.w / 2, y: y - view.h / 2 });
  }

  function fit() {
    focusZone(null);
  }

  function focusZone(name) {
    focusedZone = name;
    for (const [zoneName, z] of zoneRects) z.rect.classList.toggle("is-focus", zoneName === name);
    const target = name && zoneRects.get(name);
    setView(target ? { x: target.x - 80, y: target.y - 80, w: target.w + 160, h: target.h + 160 } : home);
    renderZoneChips();
  }

  function renderZoneChips() {
    clear(zoneChips);
    if (zoneRects.size < 2) return;
    const chip = (label, value) => h("button", {
      type: "button", class: `chip${focusedZone === value ? " is-on" : ""}`, text: label,
      onclick: () => focusZone(value),
    });
    zoneChips.append(chip("All zones", null));
    for (const name of [...zoneRects.keys()].sort(naturalCompare)) zoneChips.append(chip(name, name));
  }

  zoomIn.addEventListener("click", () => zoomAt(0.75, view.x + view.w / 2, view.y + view.h / 2));
  zoomOut.addEventListener("click", () => zoomAt(1 / 0.75, view.x + view.w / 2, view.y + view.h / 2));
  fitBtn.addEventListener("click", fit);

  stage.addEventListener("wheel", (event) => {
    event.preventDefault();
    const p = toWorld(event.clientX, event.clientY);
    zoomAt(event.deltaY > 0 ? 1.12 : 1 / 1.12, p.x, p.y);
  }, { passive: false });

  // Drag to pan; a press that barely moves is still a click on a bay/gate.
  let drag = null;
  stage.addEventListener("pointerdown", (event) => {
    drag = { x: event.clientX, y: event.clientY, view: { ...view }, moved: false, id: event.pointerId };
  });
  stage.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) {
      drag.moved = true;
      stage.setPointerCapture(drag.id);
      stage.classList.add("is-dragging");
    }
    // Zoom is unchanged while dragging, so the screen->world scale is constant.
    const unitsPerPixel = 1 / (svg.getScreenCTM()?.a || 1);
    setView({ ...drag.view, x: drag.view.x - dx * unitsPerPixel, y: drag.view.y - dy * unitsPerPixel });
  });
  const endDrag = (event) => {
    if (drag?.moved) {
      stage.classList.remove("is-dragging");
      stage.releasePointerCapture(drag.id);
      event.preventDefault();
      // Swallow the click that follows a drag so it doesn't select a bay.
      stage.addEventListener("click", (e) => e.stopPropagation(), { capture: true, once: true });
    }
    drag = null;
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
  new ResizeObserver(() => view && setView(view)).observe(stage);

  return { setGeometry, update, focusZone, fit, select, zones: () => [...zoneRects.keys()] };
}

function legendItem(color, label) {
  return h("span", {}, h("i", { style: `background:${color}` }), label);
}
