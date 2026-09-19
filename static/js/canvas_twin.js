/**
 * 60fps HTML5 Canvas digital twin of the parking lot, drawn top-down as two
 * rows of stalls (matching the real simulator's own top-down layout) rather
 * than the earlier circular projection.
 *
 * The organizer's API gives no real x/y coordinates - only names and a
 * zoneParent - so stalls are laid out deterministically: sorted by the
 * numeric suffix in their name, split evenly into a top and bottom row, and
 * chunked into small clusters with a divider between them (purely visual,
 * echoing the simulator's own tree-divided bays). Entry/exit spots and
 * barrier gates are pinned to the left/right edges.
 *
 * State updates arrive over /ws/live at ~1s cadence; this module keeps its
 * own animation loop and eases a highlight pulse between snapshots so the
 * twin still feels alive between network ticks.
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

  function naturalKey(name) {
    const m = name.match(/(\d+)/);
    return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
  }

  function chunk(list, size) {
    const out = [];
    for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
    return out;
  }

  class DigitalTwin {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.parkSpots = [];
      this.entrySpots = [];
      this.exitSpots = [];
      this.barriers = [];
      this.zoneName = "";
      this.pulses = new Map();
      this.prevStatus = new Map();
      this._resize();
      window.addEventListener("resize", () => this._resize());
      this._loop = this._loop.bind(this);
      this._raf = requestAnimationFrame(this._loop);
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
    }

    update(spots, barriers) {
      for (const s of spots) {
        const prev = this.prevStatus.get(s.name);
        if (prev && prev !== s.status) {
          this.pulses.set(s.name, { t0: performance.now(), color: STATUS_FILL[s.status] || "#fff" });
        }
        this.prevStatus.set(s.name, s.status);
      }

      this.parkSpots = spots.filter((s) => s.purpose === "Park").sort((a, b) => naturalKey(a.name) - naturalKey(b.name));
      this.entrySpots = spots.filter((s) => s.purpose === "EntrySpot");
      this.exitSpots = spots.filter((s) => s.purpose === "ExitSpot" || s.purpose === "LeaveParking");
      this.barriers = barriers;
      this.zoneName = (spots.find((s) => s.zone) || {}).zone || "";
    }

    _loop(now) {
      this._draw(now);
      this._raf = requestAnimationFrame(this._loop);
    }

    _draw(now) {
      const ctx = this.ctx;
      const w = this.w;
      const h = this.h;
      ctx.clearRect(0, 0, w, h);

      const marginX = 46;
      const marginY = 18;
      const laneHeight = Math.max(40, h * 0.22);
      const rowHeight = (h - marginY * 2 - laneHeight) / 2;
      const lotLeft = marginX;
      const lotWidth = w - marginX * 2;
      const topRowY = marginY;
      const bottomRowY = h - marginY - rowHeight;

      const half = Math.ceil(this.parkSpots.length / 2);
      this._drawRow(this.parkSpots.slice(0, half), lotLeft, topRowY, lotWidth, rowHeight, now);
      this._drawRow(this.parkSpots.slice(half), lotLeft, bottomRowY, lotWidth, rowHeight, now);

      // Drive lane + zone label
      ctx.fillStyle = "rgba(230,237,245,0.4)";
      ctx.font = "11px Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(this.zoneName || "lot", w / 2, marginY + rowHeight + laneHeight / 2 + 4);

      this._drawEndMarkers(this.entrySpots, "ENTRY", lotLeft - 6, marginY, h - marginY * 2, true);
      this._drawEndMarkers(this.exitSpots, "EXIT", lotLeft + lotWidth + 6, marginY, h - marginY * 2, false);

      this._drawBarriers(lotLeft, lotLeft + lotWidth, marginY + rowHeight + laneHeight / 2);
    }

    _drawRow(rowSpots, x0, y0, rowWidth, rowHeight, now) {
      const ctx = this.ctx;
      const groups = chunk(rowSpots, 4);
      const dividerW = 10;
      const dividerCount = Math.max(0, groups.length - 1);
      const stallAreaW = rowWidth - dividerCount * dividerW;
      const total = rowSpots.length || 1;
      const stallW = stallAreaW / total;
      const stallH = rowHeight - 14;
      const stallY = y0 + (rowHeight - stallH) / 2;

      let x = x0;
      for (let gi = 0; gi < groups.length; gi++) {
        for (const spot of groups[gi]) {
          this._drawStall(x + 1, stallY, stallW - 2, stallH, spot, now);
          x += stallW;
        }
        if (gi < groups.length - 1) {
          ctx.fillStyle = "rgba(61,220,132,0.12)";
          ctx.fillRect(x, y0, dividerW, rowHeight);
          x += dividerW;
        }
      }
    }

    _drawStall(x, y, width, height, spot, now) {
      const ctx = this.ctx;
      let fill = STATUS_FILL[spot.status] || STATUS_FILL.AVAILABLE;
      let stroke = STATUS_STROKE[spot.status] || STATUS_STROKE.AVAILABLE;
      let flashAlpha = 0;

      const pulse = this.pulses.get(spot.name);
      if (pulse) {
        const elapsed = now - pulse.t0;
        const duration = 900;
        if (elapsed < duration) {
          flashAlpha = 1 - elapsed / duration;
        } else {
          this.pulses.delete(spot.name);
        }
      }

      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(x, y, width, height, 3) : ctx.rect(x, y, width, height);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.lineWidth = 1;
      ctx.strokeStyle = stroke;
      ctx.stroke();

      if (spot.broken || spot.under_maintenance) {
        ctx.strokeStyle = "rgba(239,91,109,0.6)";
        ctx.beginPath();
        ctx.moveTo(x + 3, y + 3);
        ctx.lineTo(x + width - 3, y + height - 3);
        ctx.moveTo(x + width - 3, y + 3);
        ctx.lineTo(x + 3, y + height - 3);
        ctx.stroke();
      }

      if (flashAlpha > 0) {
        ctx.globalAlpha = flashAlpha * 0.6;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(x, y, width, height);
        ctx.globalAlpha = 1;
      }

      if (width > 16) {
        ctx.fillStyle = "rgba(230,237,245,0.55)";
        ctx.font = "8px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillText(spot.name, x + width / 2, y + height + 9);
      }
    }

    _drawEndMarkers(spots, fallbackLabel, x, y0, height, alignRight) {
      const ctx = this.ctx;
      const count = Math.max(1, spots.length);
      const slot = height / count;
      spots.forEach((spot, i) => {
        const cy = y0 + slot * i + slot / 2;
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(alignRight ? x : x - 16, cy - 9, 16, 18, 3) : ctx.rect(alignRight ? x : x - 16, cy - 9, 16, 18);
        ctx.fillStyle = STATUS_FILL[spot.status] === STATUS_FILL.AVAILABLE ? "rgba(79,209,197,0.25)" : (STATUS_FILL[spot.status] || "#4fd1c5");
        ctx.fill();
        ctx.strokeStyle = "#4fd1c5";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = "rgba(230,237,245,0.6)";
        ctx.font = "8px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.save();
        ctx.translate(alignRight ? x + 8 : x - 8, cy);
        ctx.rotate(alignRight ? Math.PI / 2 : -Math.PI / 2);
        ctx.fillText(spot.name || fallbackLabel, 0, 3);
        ctx.restore();
      });
    }

    _drawBarriers(leftX, rightX, laneY) {
      const ctx = this.ctx;
      if (!this.barriers.length) return;
      const spacing = (rightX - leftX) / (this.barriers.length + 1);
      this.barriers.forEach((b, i) => {
        const bx = leftX + spacing * (i + 1);
        ctx.beginPath();
        ctx.moveTo(bx - 10, laneY);
        ctx.lineTo(bx + 10, laneY);
        ctx.strokeStyle = b.state === "Open" ? "#3ddc84" : "#ef5b6d";
        ctx.lineWidth = 4;
        ctx.stroke();
        ctx.fillStyle = "rgba(230,237,245,0.45)";
        ctx.font = "8px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillText(b.name, bx, laneY - 8);
      });
    }

    destroy() {
      if (this._raf) cancelAnimationFrame(this._raf);
    }
  }

  window.DigitalTwin = DigitalTwin;
})();
