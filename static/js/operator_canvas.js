/**
 * Operator canvas: draws the parking lot at its real simulator geometry
 * (pixel coordinates pulled from /api/layout, which reads the level's own
 * ParkingSpots/Gates/Zones X/Y - not a synthetic projection), then overlays
 * live status and animated dispatch paths from the /ws/telemetry feed.
 *
 * A "path" is an orthogonal aisle-style line (out of the origin, across,
 * into the destination) rather than a straight diagonal, since that is what
 * a car in a lot actually does. It is a visual estimate, not the result of
 * pathfinding over the level's real road graph.
 */
(function () {
  "use strict";

  const STATUS_FILL = {
    AVAILABLE: "rgba(230,237,245,0.06)",
    OCCUPIED: "#4fd1c5",
    RESERVED: "#f5b942",
    BROKEN: "#6b7280",
    MAINTENANCE: "#6b7280",
  };
  const STATUS_STROKE = {
    AVAILABLE: "rgba(230,237,245,0.25)",
    OCCUPIED: "#2b7d75",
    RESERVED: "#a97a1f",
    BROKEN: "#4b5563",
    MAINTENANCE: "#4b5563",
  };
  const ENTRY_COLOR = "#4fd1c5";
  const EXIT_COLOR = "#f5b942";

  class OperatorCanvas {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.layout = { spots: {}, gates: {}, zones: {}, bounds: null };
      this.spotStatus = new Map();
      this.barrierStatus = new Map();
      this.paths = new Map(); // plate -> {waypoints, kind, t0}
      this.dashOffset = 0;
      this.trackedPlate = null; // null = show nothing; a route only ever renders for this exact plate
      this._resize();
      window.addEventListener("resize", () => this._resize());
      this._loop = this._loop.bind(this);
      this._raf = requestAnimationFrame(this._loop);
    }

    /** Only render a route for this exact plate (trimmed, case-insensitive).
     * Pass null/empty to show nothing - the canvas never tracks every car
     * at once, only the one the operator asked about. */
    setTrackedPlate(plate) {
      this.trackedPlate = plate ? plate.trim().toUpperCase() : null;
      if (!this.trackedPlate) this.paths.clear();
    }

    async loadLayout() {
      try {
        const res = await fetch("/api/layout");
        this.layout = await res.json();
        this._computeTransform();
        this._computeAisle();
      } catch (err) {
        console.error("layout load failed", err);
      }
    }

    /** Find the empty drive lane between the two rows of bays, so dispatch
     * paths can travel through it instead of cutting straight across a row
     * of parked cars. Works by clustering Park-spot Y centers into a "top"
     * and "bottom" group (median split) and taking the midpoint between
     * them - this level has exactly two rows, so that midpoint is the aisle. */
    _computeAisle() {
      const ys = Object.values(this.layout.spots)
        .filter((s) => s.purpose === "Park")
        .map((s) => s.y)
        .sort((a, b) => a - b);
      if (ys.length < 2) {
        this.aisleY = null;
        return;
      }
      const mid = ys[Math.floor(ys.length / 2)];
      const top = ys.filter((y) => y <= mid);
      const bottom = ys.filter((y) => y > mid);
      if (!top.length || !bottom.length) {
        this.aisleY = mid;
        this.laneOffset = 0;
        return;
      }
      const topEdge = Math.max(...top);
      const bottomEdge = Math.min(...bottom);
      this.aisleY = (topEdge + bottomEdge) / 2;
      // Keep each direction of travel to its own side of the aisle - a car
      // heading to its bay hugs the near (top-row) lane, a car heading to
      // the exit hugs the far (bottom-row) lane - rather than both sharing
      // one line down the dead centre of a very wide open area.
      this.laneOffset = Math.min(70, (bottomEdge - topEdge) / 4);
    }

    /** Waypoints for a car travelling between two named points, routed
     * through the drive aisle rather than straight through a row of bays.
     * ``kind`` ("entry" | "exit") picks which side of the aisle to hug. */
    _routeWaypoints(from, to, kind) {
      if (this.aisleY == null) return [from, to];
      const laneY = kind === "exit" ? this.aisleY + this.laneOffset : this.aisleY - this.laneOffset;
      return [
        from,
        { x: from.x, y: laneY },
        { x: to.x, y: laneY },
        to,
      ];
    }

    _resize() {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.max(200, rect.width) * dpr;
      this.canvas.height = Math.max(140, rect.height) * dpr;
      this.canvas.style.width = rect.width + "px";
      this.canvas.style.height = rect.height + "px";
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.w = rect.width;
      this.h = rect.height;
      this._computeTransform();
    }

    _computeTransform() {
      const b = this.layout.bounds;
      if (!b) {
        this.scale = 1;
        this.offX = 0;
        this.offY = 0;
        return;
      }
      const pad = 40;
      const spanX = Math.max(1, b.max_x - b.min_x);
      const spanY = Math.max(1, b.max_y - b.min_y);
      this.scale = Math.min((this.w - pad * 2) / spanX, (this.h - pad * 2) / spanY);
      this.offX = pad - b.min_x * this.scale + (this.w - pad * 2 - spanX * this.scale) / 2;
      this.offY = pad - b.min_y * this.scale + (this.h - pad * 2 - spanY * this.scale) / 2;
    }

    _toScreen(x, y) {
      return [x * this.scale + this.offX, y * this.scale + this.offY];
    }

    /** Feed one /ws/telemetry snapshot in. */
    update(snapshot) {
      this.spotStatus.clear();
      for (const s of snapshot.spots || []) this.spotStatus.set(s.name, s);
      this.barrierStatus.clear();
      for (const b of snapshot.barriers || []) this.barrierStatus.set(b.name, b);

      if (!this.trackedPlate) {
        this.paths.clear();
        return;
      }

      const session = (snapshot.sessions || [])
        .find((s) => s.plate.trim().toUpperCase() === this.trackedPlate);

      if (!session) {
        this.paths.clear();
        return;
      }

      const originName = session.phase === "AT_EXIT" || session.phase === "CHARGED"
        ? session.assigned_spot : session.entry_gate;
      const destName = session.phase === "AT_EXIT" || session.phase === "CHARGED"
        ? session.exit_gate : session.assigned_spot;
      const kind = session.phase === "AT_EXIT" || session.phase === "CHARGED" ? "exit" : "entry";
      const active = ["ASSIGNED", "AT_EXIT", "CHARGED"].includes(session.phase);

      if (!active || !originName || !destName) {
        this.paths.clear();
        return;
      }
      const from = this.layout.spots[originName] || this.layout.gates[originName];
      const to = this.layout.spots[destName] || this.layout.gates[destName];
      if (!from || !to) {
        this.paths.clear();
        return;
      }
      const existing = this.paths.get(session.plate);
      if (!existing || existing.kind !== kind) {
        const waypoints = this._routeWaypoints(from, to, kind);
        this.paths.set(session.plate, { waypoints, kind, plate: session.plate, t0: performance.now() });
      }
    }

    _loop(now) {
      this.dashOffset = (this.dashOffset + 0.6) % 16;
      this._draw(now);
      this._raf = requestAnimationFrame(this._loop);
    }

    _draw(now) {
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.h);
      if (!this.layout.bounds) {
        ctx.fillStyle = "rgba(230,237,245,0.4)";
        ctx.font = "13px Segoe UI, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("waiting for layout...", this.w / 2, this.h / 2);
        return;
      }

      for (const zone of Object.values(this.layout.zones)) {
        const [x, y] = this._toScreen(zone.x - zone.w / 2, zone.y - zone.h / 2);
        ctx.strokeStyle = "rgba(79,209,197,0.15)";
        ctx.lineWidth = 1;
        ctx.strokeRect(x, y, zone.w * this.scale, zone.h * this.scale);
      }

      for (const [name, spot] of Object.entries(this.layout.spots)) {
        const live = this.spotStatus.get(name) || {};
        const status = live.status || "AVAILABLE";
        const purpose = live.purpose || spot.purpose;
        const w = Math.max(6, spot.w * this.scale * 0.8);
        const h = Math.max(6, spot.h * this.scale * 0.8);
        const [cx, cy] = this._toScreen(spot.x, spot.y);
        const x = cx - w / 2;
        const y = cy - h / 2;

        if (purpose === "Park") {
          ctx.beginPath();
          ctx.roundRect ? ctx.roundRect(x, y, w, h, 3) : ctx.rect(x, y, w, h);
          ctx.fillStyle = STATUS_FILL[status] || STATUS_FILL.AVAILABLE;
          ctx.fill();
          ctx.lineWidth = 1;
          ctx.strokeStyle = STATUS_STROKE[status] || STATUS_STROKE.AVAILABLE;
          ctx.stroke();
          if (live.broken || live.under_maintenance) {
            ctx.strokeStyle = "rgba(239,91,109,0.6)";
            ctx.beginPath();
            ctx.moveTo(x + 2, y + 2);
            ctx.lineTo(x + w - 2, y + h - 2);
            ctx.moveTo(x + w - 2, y + 2);
            ctx.lineTo(x + 2, y + h - 2);
            ctx.stroke();
          }
        } else {
          const isEntry = purpose === "EntrySpot";
          ctx.beginPath();
          ctx.arc(cx, cy, 8, 0, Math.PI * 2);
          ctx.fillStyle = isEntry ? ENTRY_COLOR : EXIT_COLOR;
          ctx.fill();
        }

        if (this.scale > 0.18) {
          ctx.fillStyle = "rgba(230,237,245,0.5)";
          ctx.font = "10px Consolas, monospace";
          ctx.textAlign = "center";
          ctx.fillText(name, cx, cy + h / 2 + 9);
        }
      }

      for (const [name, gate] of Object.entries(this.layout.gates)) {
        const live = this.barrierStatus.get(name);
        const [cx, cy] = this._toScreen(gate.x, gate.y);
        ctx.beginPath();
        ctx.moveTo(cx - 14, cy);
        ctx.lineTo(cx + 14, cy);
        ctx.strokeStyle = live && live.state === "Open" ? "#3ddc84" : "#ef5b6d";
        ctx.lineWidth = 4;
        ctx.stroke();
        ctx.fillStyle = "rgba(230,237,245,0.5)";
        ctx.font = "10px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillText(name, cx, cy - 10);
      }

      for (const path of this.paths.values()) this._drawPath(path, now);
    }

    _drawPath(path, now) {
      const ctx = this.ctx;
      const color = path.kind === "exit" ? EXIT_COLOR : ENTRY_COLOR;
      const screenPts = path.waypoints.map((p) => this._toScreen(p.x, p.y));

      ctx.beginPath();
      ctx.moveTo(screenPts[0][0], screenPts[0][1]);
      for (let i = 1; i < screenPts.length; i++) ctx.lineTo(screenPts[i][0], screenPts[i][1]);
      ctx.setLineDash([7, 6]);
      ctx.lineDashOffset = -this.dashOffset;
      ctx.strokeStyle = color;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.setLineDash([]);

      // Walk the polyline at a constant speed to find where the car marker sits.
      const legs = [];
      let total = 0;
      for (let i = 1; i < screenPts.length; i++) {
        const len = Math.hypot(screenPts[i][0] - screenPts[i - 1][0], screenPts[i][1] - screenPts[i - 1][1]);
        legs.push(len);
        total += len;
      }
      const elapsed = (now - path.t0) / 1000;
      let dist = Math.min(total, (elapsed / 5) * total);
      let px = screenPts[0][0];
      let py = screenPts[0][1];
      for (let i = 0; i < legs.length; i++) {
        if (dist <= legs[i] || i === legs.length - 1) {
          const t = legs[i] > 0 ? Math.min(1, dist / legs[i]) : 1;
          const [x0, y0] = screenPts[i];
          const [x1, y1] = screenPts[i + 1];
          px = x0 + (x1 - x0) * t;
          py = y0 + (y1 - y0) * t;
          break;
        }
        dist -= legs[i];
      }

      // Car marker
      ctx.beginPath();
      ctx.arc(px, py, 7, 0, Math.PI * 2);
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 3;
      ctx.stroke();

      // Plate label on a solid pill so it stays legible over the lot.
      ctx.font = "bold 13px Consolas, monospace";
      ctx.textAlign = "center";
      const labelY = py - 16;
      const textWidth = ctx.measureText(path.plate).width;
      const padX = 7;
      ctx.fillStyle = "rgba(6, 10, 16, 0.85)";
      ctx.beginPath();
      const rx = px - textWidth / 2 - padX;
      const ry = labelY - 13;
      const rw = textWidth + padX * 2;
      const rh = 18;
      ctx.roundRect ? ctx.roundRect(rx, ry, rw, rh, 5) : ctx.rect(rx, ry, rw, rh);
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.fillText(path.plate, px, labelY);
    }
  }

  window.OperatorCanvas = OperatorCanvas;
})();
