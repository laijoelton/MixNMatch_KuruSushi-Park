/**
 * Cinema-style interactive parking spot picker for the mobile gate portal.
 *
 * Renders one button per "Park" spot (organizer's list-parking-spots
 * purpose), colored by live status, and lets the driver pick a specific
 * bay before check-in. Live updates arrive over the same /ws/live feed the
 * operator HUD uses; this module only reads the `spots` field of each frame.
 */
(function () {
  "use strict";

  function statusClass(spot) {
    if (spot.broken || spot.under_maintenance) return "broken";
    if (spot.status === "OCCUPIED") return "occupied";
    if (spot.status === "RESERVED") return "reserved";
    return "available";
  }

  class LotPicker {
    constructor(gridEl, { onSelect } = {}) {
      this.gridEl = gridEl;
      this.onSelect = onSelect || function () {};
      this.selected = null;
      this.spots = [];
      this.buttons = new Map();
    }

    render(spots) {
      this.spots = spots.filter((s) => s.purpose === "Park");
      this.gridEl.innerHTML = "";
      this.buttons.clear();

      for (const spot of this.spots) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "seat " + statusClass(spot);
        btn.textContent = spot.name;
        btn.disabled = statusClass(spot) !== "available";
        btn.setAttribute("aria-label", `Spot ${spot.name}, ${spot.status}`);
        btn.addEventListener("click", () => this._select(spot.name, btn));
        this.gridEl.appendChild(btn);
        this.buttons.set(spot.name, btn);
      }

      if (this.selected && !this.buttons.has(this.selected)) {
        this.selected = null;
        this.onSelect(null);
      } else if (this.selected) {
        const btn = this.buttons.get(this.selected);
        if (btn && !btn.disabled) btn.classList.add("selected");
        else {
          this.selected = null;
          this.onSelect(null);
        }
      }
    }

    _select(name, btn) {
      if (btn.disabled) return;
      for (const b of this.buttons.values()) b.classList.remove("selected");
      btn.classList.add("selected");
      this.selected = name;
      this.onSelect(name);
    }

    clearSelection() {
      this.selected = null;
      for (const b of this.buttons.values()) b.classList.remove("selected");
    }
  }

  window.LotPicker = LotPicker;
})();
