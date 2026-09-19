/**
 * 60fps HTML5 Canvas digital twin of the kaiten-sushi dispatch ring.
 *
 * The backend has no physical loop inside the organizer's simulator - the
 * "ring" here is the same deterministic sorted-name projection used by
 * app/routing.py's StationRing, recomputed client-side from the spot/barrier
 * name list in every snapshot so the two stay visually and numerically
 * consistent. Positions on screen are placed by that ring index (angle),
 * not by any real coordinate the organizer's API does not provide.
 *
 * State updates arrive over /ws/live at ~1s cadence; this module keeps its
 * own animation loop and eases colors/pulses between snapshots so the
 * twin still feels alive between network ticks.
 */
(function () {
  "use strict";

  const STATUS_COLORS = {
    AVAILABLE: "#3ddc84",
    OCCUPIED: "#4fd1c5",
    RESERVED: "#f5b942",
    BROKEN: "#6b7280",
    MAINTENANCE: "#6b7280",
  };

  class DigitalTwin {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.spots = [];
      this.barriers = [];
      this.pulses = new Map(); // name -> {t0, color}
      this.prevStatus = new Map();
      this._raf = null;
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
      const names = [...spots.map((s) => s.name), ...barriers.map((b) => b.name)].sort();
      const ring = new Map(names.map((n, i) => [n, i]));
      const n = Math.max(1, names.length);

      for (const s of spots) {
        const prev = this.prevStatus.get(s.name);
        if (prev && prev !== s.status) {
          this.pulses.set(s.name, { t0: performance.now(), color: STATUS_COLORS[s.status] || "#fff" });
        }
        this.prevStatus.set(s.name, s.status);
      }

      this.spots = spots.map((s) => ({ ...s, angle: (2 * Math.PI * (ring.get(s.name) || 0)) / n }));
      this.barriers = barriers.map((b) => ({ ...b, angle: (2 * Math.PI * (ring.get(b.name) || 0)) / n }));
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

      const cx = w / 2;
      const cy = h / 2;
      const radius = Math.min(w, h) * 0.38;

      // Ring track
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,255,255,0.08)";
      ctx.lineWidth = 18;
      ctx.stroke();

      // Barriers (gate markers on the ring)
      for (const b of this.barriers) {
        const x = cx + radius * Math.cos(b.angle - Math.PI / 2);
        const y = cy + radius * Math.sin(b.angle - Math.PI / 2);
        ctx.beginPath();
        ctx.moveTo(cx + (radius - 14) * Math.cos(b.angle - Math.PI / 2), cy + (radius - 14) * Math.sin(b.angle - Math.PI / 2));
        ctx.lineTo(cx + (radius + 14) * Math.cos(b.angle - Math.PI / 2), cy + (radius + 14) * Math.sin(b.angle - Math.PI / 2));
        ctx.strokeStyle = b.state === "Open" ? "#3ddc84" : "#ef5b6d";
        ctx.lineWidth = 4;
        ctx.stroke();
        ctx.fillStyle = "#8896a8";
        ctx.font = "10px Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillText(b.name, x, y - 18 * Math.sign(Math.sin(b.angle)) || y - 18);
      }

      // Spots
      for (const s of this.spots) {
        const x = cx + radius * Math.cos(s.angle - Math.PI / 2);
        const y = cy + radius * Math.sin(s.angle - Math.PI / 2);
        let color = STATUS_COLORS[s.status] || "#8896a8";
        let radiusPx = 6;

        const pulse = this.pulses.get(s.name);
        if (pulse) {
          const elapsed = now - pulse.t0;
          const duration = 900;
          if (elapsed < duration) {
            const t = elapsed / duration;
            radiusPx = 6 + 10 * (1 - t);
            color = pulse.color;
            ctx.globalAlpha = 1 - t;
          } else {
            this.pulses.delete(s.name);
          }
        }

        ctx.beginPath();
        ctx.arc(x, y, radiusPx, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = s.status === "OCCUPIED" ? 8 : 0;
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 1;
      }

      // Hub label
      ctx.fillStyle = "rgba(230,237,245,0.5)";
      ctx.font = "11px Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(`${this.spots.length} spots`, cx, cy);
    }

    destroy() {
      if (this._raf) cancelAnimationFrame(this._raf);
    }
  }

  window.DigitalTwin = DigitalTwin;
})();
