import { h, keyedList, setText } from "../core/dom.js";
import { TYPE_GLYPH, gateState, naturalCompare, spotState } from "../core/format.js";

// One card per zone: free bays, occupancy bar, free-by-type, air quality,
// fans (only when the level has them) and that zone's gates.
// Level-agnostic: zones, types and gates all come from the snapshot.

const PERIMETER = "Perimeter";

export function createZones(container, { onFocus, onGate } = {}) {
  function update(snapshot) {
    const park = (snapshot.spots || []).filter((sp) => sp.purpose === "Park");
    const zoneNames = new Set(park.map((sp) => sp.zone || PERIMETER));
    const barriers = snapshot.barriers || [];
    if (barriers.some((b) => !b.zone)) zoneNames.add(PERIMETER);
    const envByZone = new Map((snapshot.zones || []).map((z) => [z.name, z]));

    const cards = [...zoneNames].sort(naturalCompare).map((name) => ({
      name,
      spots: park.filter((sp) => (sp.zone || PERIMETER) === name),
      gates: barriers.filter((b) => (b.zone || PERIMETER) === name),
      fans: (snapshot.fans || []).filter((f) => f.zone === name),
      env: envByZone.get(name),
    }));
    keyedList(container, cards, (c) => c.name, render, fill);
  }

  function render(card) {
    const refs = {
      title: h("h3"), tag: h("span", { class: "tag" }), free: h("span"), total: h("small"),
      occ: h("span", { class: "b-occ" }), res: h("span", { class: "b-res" }), fault: h("span", { class: "b-fault" }),
      types: h("div", { class: "zone-types" }), env: h("div", { class: "zone-env" }), gates: h("div", { class: "zone-gates" }),
    };
    const el = h("article", {
      class: "zone-card", tabindex: 0, role: "button",
      onclick: (event) => { if (!event.target.closest(".gate-chip")) onFocus?.(card.name); },
      onkeydown: (event) => { if (event.key === "Enter") onFocus?.(card.name); },
    },
      h("div", { class: "zone-top" }, refs.title, h("div", { class: "spacer" }), refs.tag),
      h("div", { class: "zone-free" }, refs.free, refs.total),
      h("div", { class: "bar", "aria-hidden": "true" }, refs.occ, refs.res, refs.fault),
      refs.types, refs.env, refs.gates);
    el._refs = refs;
    fill(el, card);
    return el;
  }

  function fill(el, card) {
    const r = el._refs;
    el._card = card;
    const states = card.spots.map(spotState);
    const count = (st) => states.filter((x) => x === st).length;
    const total = card.spots.length;
    const free = count("free");
    const pct = (n) => (total ? `${(n / total) * 100}%` : "0");

    setText(r.title, card.name);
    const full = total > 0 && free === 0;
    el.classList.toggle("is-full", full);
    const nearlyFull = total > 0 && free <= Math.ceil(total * 0.1);
    r.tag.className = `tag ${total === 0 ? "" : full ? "fault" : nearlyFull ? "reserved" : "free"}`;
    setText(r.tag, total === 0 ? "Gates only" : full ? "Full" : nearlyFull ? "Almost full" : "Open");
    setText(r.free, total ? free : "—");
    setText(r.total, total ? ` free of ${total}` : "");
    r.occ.style.width = pct(count("occupied"));
    r.res.style.width = pct(count("reserved"));
    r.fault.style.width = pct(count("fault"));
    r.free.parentElement.hidden = total === 0;
    r.occ.parentElement.hidden = total === 0;

    // Free bays per car type - only the types this zone actually has.
    const types = [...new Set(card.spots.map((sp) => sp.car_type || "Any"))].sort();
    r.types.replaceChildren(...types.map((type) => {
      const freeOfType = card.spots.filter((sp) => (sp.car_type || "Any") === type && spotState(sp) === "free").length;
      return h("span", {}, `${TYPE_GLYPH[type] ? `${TYPE_GLYPH[type]} ` : ""}${type} `, h("b", { text: freeOfType }));
    }));

    const envParts = [];
    if (card.env && (card.env.co_level > 0 || (card.env.danger_level && card.env.danger_level !== "Safe"))) {
      envParts.push(h("span", {}, "CO ", h("b", { class: "num", text: Number(card.env.co_level).toFixed(1) }),
        ` · ${card.env.danger_level || card.env.risk || "Safe"}`));
    }
    if (card.fans.length) {
      const on = card.fans.filter((f) => f.is_on).length;
      envParts.push(h("span", {}, "Fans ", h("b", { class: "num", text: `${on}/${card.fans.length}` }), " on"));
    }
    r.env.replaceChildren(...envParts);
    r.env.hidden = envParts.length === 0;

    r.gates.replaceChildren(...card.gates.map((gate) => {
      const st = gateState(gate);
      return h("button", {
        type: "button", class: `gate-chip ${st}`, title: `${gate.name}: ${gate.state}${st === "fault" ? " (out of service)" : ""}`,
        onclick: () => onGate?.(gate.name),
      }, h("i"), gate.name);
    }));
  }

  return { update };
}
