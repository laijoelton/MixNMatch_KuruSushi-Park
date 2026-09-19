/**
 * Phone-frame driver portal: plate entry + cinema-style bay picker, then a
 * Waze-style turn-by-turn HUD once a bay is chosen. Reads the same
 * /api/layout geometry and /ws/telemetry feed the operator canvas uses, so
 * both panes settle on the same state without any extra plumbing between
 * them - there is exactly one source of truth (ParkingState on the server).
 */
(function () {
  "use strict";

  function statusClass(spot) {
    if (spot.broken || spot.under_maintenance) return "broken";
    if (spot.status === "OCCUPIED") return "occupied";
    if (spot.status === "RESERVED") return "reserved";
    return "available";
  }

  function bearingArrow(dx, dy) {
    const angle = Math.atan2(dy, dx) * (180 / Math.PI);
    if (angle > -45 && angle <= 45) return { glyph: "→", label: "Head right", deg: 90 };
    if (angle > 45 && angle <= 135) return { glyph: "↓", label: "Head down", deg: 180 };
    if (angle > -135 && angle <= -45) return { glyph: "↑", label: "Head up", deg: 0 };
    return { glyph: "←", label: "Head left", deg: 270 };
  }

  class DriverPortal {
    constructor(screenEl, defaultGate) {
      this.screenEl = screenEl;
      this.gate = defaultGate;
      this.layout = { spots: {}, gates: {}, bounds: null };
      this.spots = [];
      this.selectedSpot = null;
      this.mode = "picker"; // picker | navigating | arrived
      this.activePlate = null;
      this.navStart = 0;
      this._renderPicker();
    }

    async loadLayout() {
      const res = await fetch("/api/layout");
      this.layout = await res.json();
    }

    /** Feed one /ws/telemetry snapshot in. */
    update(snapshot) {
      this.spots = snapshot.spots || [];
      if (this.mode === "picker") this._refreshSeats();

      if (this.mode === "navigating" && this.activePlate) {
        const session = (snapshot.sessions || []).find((s) => s.plate === this.activePlate);
        if (session && session.phase === "PARKED") {
          this.mode = "arrived";
          this._renderArrived();
        } else if (!session) {
          // Session vanished without ever parking (dispatch failed / lot full).
          this.mode = "picker";
          this.activePlate = null;
          this._renderPicker();
        } else {
          this._updateNavStats(session);
        }
      }
    }

    // ------------------------------------------------------------------ //
    // Picker screen
    // ------------------------------------------------------------------ //
    /** Full rebuild - only ever called when entering picker mode fresh. */
    _renderPicker() {
      this.selectedSpot = null;
      this.screenEl.innerHTML = `
        <h2>Find your bay</h2>
        <div class="subtitle">Enter your plate, pick a spot, go.</div>
        <div class="field-row">
          <label for="dp-plate">Plate number</label>
          <input id="dp-plate" type="text" placeholder="e.g. ABC 1234" maxlength="16" />
        </div>
        <div class="lot-grid" id="dp-grid"></div>
        <button id="dp-go" disabled>Select a bay first</button>
        <div class="status-message" id="dp-status"></div>
      `;
      this._seatButtons = new Map();
      this.screenEl.querySelector("#dp-go").addEventListener("click", () => this._dispatch());
      this._refreshSeats();
    }

    /** In-place update on every telemetry tick - never touches the plate
     * input or the current selection, only each seat's color/enabled state
     * (and adds/removes seats if the spot list itself changes). */
    _refreshSeats() {
      const grid = this.screenEl.querySelector("#dp-grid");
      if (!grid) return this._renderPicker();
      const parkSpots = this.spots.filter((s) => s.purpose === "Park");
      const seen = new Set();

      for (const spot of parkSpots) {
        seen.add(spot.name);
        const cls = statusClass(spot);
        let btn = this._seatButtons.get(spot.name);
        if (!btn) {
          btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = spot.name;
          btn.addEventListener("click", () => this._selectSeat(spot.name, btn));
          grid.appendChild(btn);
          this._seatButtons.set(spot.name, btn);
        }
        const wasSelected = btn.classList.contains("selected");
        btn.className = "seat " + cls + (wasSelected ? " selected" : "");
        btn.disabled = cls !== "available";
        if (wasSelected && cls !== "available") {
          // The bay we picked just got taken by someone else - clear it.
          btn.classList.remove("selected");
          if (this.selectedSpot === spot.name) {
            this.selectedSpot = null;
            const go = this.screenEl.querySelector("#dp-go");
            if (go) { go.disabled = true; go.textContent = "Select a bay first"; }
          }
        }
      }
      for (const [name, btn] of this._seatButtons) {
        if (!seen.has(name)) {
          btn.remove();
          this._seatButtons.delete(name);
        }
      }
    }

    _selectSeat(name, btn) {
      if (btn.disabled) return;
      for (const b of this._seatButtons.values()) b.classList.remove("selected");
      btn.classList.add("selected");
      this.selectedSpot = name;
      const go = this.screenEl.querySelector("#dp-go");
      go.disabled = false;
      go.textContent = `Go to ${name}`;
    }

    async _dispatch() {
      const plateInput = this.screenEl.querySelector("#dp-plate");
      const statusEl = this.screenEl.querySelector("#dp-status");
      const plate = plateInput.value.trim();
      if (!plate) {
        statusEl.textContent = "Enter a plate number first";
        statusEl.className = "status-message error";
        return;
      }
      if (!this.selectedSpot) return;

      const goBtn = this.screenEl.querySelector("#dp-go");
      goBtn.disabled = true;
      statusEl.textContent = "Dispatching...";
      statusEl.className = "status-message";

      try {
        const res = await fetch("/api/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ car_plate: plate, target_spot_id: this.selectedSpot, gate: this.gate }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "dispatch failed");

        this.activePlate = plate;
        this.navStart = performance.now();
        this.mode = "navigating";
        this._renderNav(this.selectedSpot);
      } catch (err) {
        statusEl.textContent = err.message;
        statusEl.className = "status-message error";
        goBtn.disabled = false;
      }
    }

    // ------------------------------------------------------------------ //
    // GPS navigation screen
    // ------------------------------------------------------------------ //
    _renderNav(spotName) {
      this.screenEl.innerHTML = `
        <div class="gps-view">
          <h2>Navigating to ${spotName}</h2>
          <div class="subtitle">Follow the aisle - the operator can see this exact path too.</div>
          <div class="gps-lane">
            <span class="arrow" id="gps-arrow">→</span>
            <div>
              <div id="gps-instruction" style="font-weight:700;">Head toward the bay</div>
              <div class="subtitle" id="gps-sub">Calculating route...</div>
            </div>
          </div>
          <div class="gps-stats">
            <div class="stat"><div class="value" id="gps-distance">--</div><div class="label">meters</div></div>
            <div class="stat"><div class="value" id="gps-eta">--</div><div class="label">seconds</div></div>
          </div>
          <div class="gps-map"></div>
        </div>
      `;
      this._updateNavGeometry(spotName);
    }

    _updateNavGeometry(spotName) {
      const gateGeo = this.layout.spots[this.gate] || this.layout.gates[this.gate];
      const spotGeo = this.layout.spots[spotName];
      if (!gateGeo || !spotGeo) return;
      const dx = spotGeo.x - gateGeo.x;
      const dy = spotGeo.y - gateGeo.y;
      const arrow = bearingArrow(dx, dy);
      const distPx = Math.hypot(dx, dy);
      // Cosmetic conversion from simulator pixel units to a friendly distance figure.
      const meters = Math.round(distPx / 10);

      const arrowEl = this.screenEl.querySelector("#gps-arrow");
      if (arrowEl) {
        arrowEl.textContent = arrow.glyph;
        arrowEl.style.transform = `rotate(${arrow.deg}deg)`;
      }
      const instr = this.screenEl.querySelector("#gps-instruction");
      if (instr) instr.textContent = arrow.label + ` toward ${spotName}`;
      const sub = this.screenEl.querySelector("#gps-sub");
      if (sub) sub.textContent = `${meters} m via the main aisle`;
      const distEl = this.screenEl.querySelector("#gps-distance");
      if (distEl) distEl.textContent = meters;
    }

    _updateNavStats(session) {
      const etaEl = this.screenEl.querySelector("#gps-eta");
      if (!etaEl) return;
      const elapsed = (performance.now() - this.navStart) / 1000;
      const remaining = Math.max(0, Math.round(8 - elapsed));
      etaEl.textContent = remaining;
    }

    // ------------------------------------------------------------------ //
    // Arrived screen
    // ------------------------------------------------------------------ //
    _renderArrived() {
      this.screenEl.innerHTML = `
        <div class="gps-view">
          <div class="arrived-banner">You have arrived - bay confirmed</div>
          <div class="subtitle">Plate ${this.activePlate}</div>
          <button id="dp-new" class="secondary">Dispatch another car</button>
        </div>
      `;
      this.screenEl.querySelector("#dp-new").addEventListener("click", () => {
        this.mode = "picker";
        this.activePlate = null;
        this.selectedSpot = null;
        this._renderPicker();
      });
    }
  }

  window.DriverPortal = DriverPortal;
})();
