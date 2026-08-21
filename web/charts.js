// Dependency-free canvas time-series charts (HiDPI-aware, crosshair + legend).
// Colours come from the theme CSS variables (--chart-*, --chart-grid,
// --chart-text) so a light/dark toggle recolours charts on the next draw.

export const PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#06b6d4", "#8b5cf6", "#ef4444", "#84cc16", "#f97316", "#ec4899"];

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function hexRGB(color) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color.trim());
  if (!m) return [59, 130, 246];
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
}

export class Chart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts
   *   axes: draw axes/grid (default true)
   *   crosshair: hover crosshair + value readout (default axes)
   *   fmt: y-value formatter (default 1 decimal)
   *   ymin: force y-axis minimum (default 0)
   *   ymax: force y-axis maximum when the data fits inside it
   *   legend: draw legend row with latest/hover values (default axes)
   *   fill: area fill under lines at 8% alpha, "gradient" for a vertical fade (default axes)
   *   frame: stroke a btop-style border box around the plot area (default false)
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = Object.assign(
      { axes: true, fill: opts.axes !== false, ymin: 0, fmt: (v) => v.toFixed(1), legend: opts.axes !== false, crosshair: opts.axes !== false },
      opts
    );
    this.series = [];
    this._hoverTs = null;
    if (this.opts.crosshair) {
      canvas.addEventListener("mousemove", (e) => {
        const ts = this._tsAt(e);
        if (ts !== this._hoverTs) { this._hoverTs = ts; this.draw(); }
      });
      canvas.addEventListener("mouseleave", () => { this._hoverTs = null; this.draw(); });
    }
    if (typeof ResizeObserver !== "undefined") {
      this._resizeObserver = new ResizeObserver(() => this.draw());
      this._resizeObserver.observe(canvas);
    }
    this._onWindowResize = () => this.draw();
    window.addEventListener("resize", this._onWindowResize);
  }

  dispose() {
    this._resizeObserver?.disconnect();
    window.removeEventListener("resize", this._onWindowResize);
  }

  setSeries(series) {
    // series: [{label, data:[[ts,v],...], color?}] — series without an
    // explicit colour resolve from the theme palette at draw time, so a
    // theme toggle recolours them without re-setting data.
    this.series = series.map((s, i) => ({ _ci: i, ...s }));
    this.draw();
  }

  _color(s) {
    if (s.color) return s.color;
    if (s.colorVar) return cssVar(s.colorVar, PALETTE[s._ci % PALETTE.length]);
    return cssVar(`--chart-${(s._ci % 9) + 1}`, PALETTE[s._ci % PALETTE.length]);
  }

  _bounds() {
    let tMin = Infinity, tMax = -Infinity, yMax = 0;
    for (const s of this.series) {
      for (const [t, v] of s.data) {
        if (t < tMin) tMin = t;
        if (t > tMax) tMax = t;
        if (v > yMax) yMax = v;
      }
    }
    if (!isFinite(tMin)) return null;
    if (tMax - tMin < 1) tMax = tMin + 1;
    return { tMin, tMax, yMax };
  }

  _niceMax(y) {
    if (y <= 0) return 1;
    const exp = Math.floor(Math.log10(y));
    const f = y / 10 ** exp;
    const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nice * 10 ** exp;
  }

  _timeLabel(ts, span) {
    const d = new Date(ts * 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (span <= 26 * 3600) return `${hh}:${mm}`;
    return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${hh}:${mm}`;
  }

  _layout() {
    const { canvas } = this;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!this.opts.axes) return { x0: 0, y0: 0, x1: w, y1: h, w, h };
    const top = this.opts.legend ? 18 : 6;
    return { x0: 52, y0: top, x1: w - 6, y1: h - 18, w, h };
  }

  _tsAt(e) {
    const b = this._bounds();
    if (!b) return null;
    const L = this._lastLayout || this._layout();
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (x < L.x0 || x > L.x1) return null;
    return b.tMin + ((x - L.x0) / (L.x1 - L.x0)) * (b.tMax - b.tMin);
  }

  draw() {
    const { ctx } = this;
    const L = (this._lastLayout = this._layout());
    ctx.clearRect(0, 0, L.w, L.h);
    const b = this._bounds();
    if (!b) {
      ctx.fillStyle = cssVar("--chart-text", "#737373");
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("no data", L.w / 2, L.h / 2);
      return;
    }
    const yTop =
      this.opts.ymax != null && this.opts.ymax >= b.yMax ? this.opts.ymax : this._niceMax(b.yMax);
    const span = b.tMax - b.tMin;
    const x = (t) => L.x0 + ((t - b.tMin) / span) * (L.x1 - L.x0);
    const y = (v) => L.y1 - ((v - this.opts.ymin) / (yTop - this.opts.ymin)) * (L.y1 - L.y0);

    if (this.opts.axes) this._drawAxes(x, y, b, yTop, L, span);

    for (const s of this.series) {
      if (s.data.length === 0) continue;
      const color = this._color(s);
      if (this.opts.fill) {
        ctx.beginPath();
        ctx.moveTo(x(s.data[0][0]), y(this.opts.ymin));
        for (const [t, v] of s.data) ctx.lineTo(x(t), y(v));
        ctx.lineTo(x(s.data[s.data.length - 1][0]), y(this.opts.ymin));
        ctx.closePath();
        if (this.opts.fill === "gradient") {
          const [r, g, bl] = hexRGB(color);
          const grad = ctx.createLinearGradient(0, L.y0, 0, L.y1);
          grad.addColorStop(0, `rgba(${r},${g},${bl},0.3)`);
          grad.addColorStop(1, `rgba(${r},${g},${bl},0)`);
          ctx.fillStyle = grad;
        } else {
          ctx.globalAlpha = 0.08;
          ctx.fillStyle = color;
        }
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.lineJoin = "round";
      for (let i = 0; i < s.data.length; i++) {
        const [t, v] = s.data[i];
        if (i === 0) ctx.moveTo(x(t), y(v));
        else ctx.lineTo(x(t), y(v));
      }
      ctx.stroke();
    }

    if (this.opts.frame) {
      ctx.strokeStyle = cssVar("--line", "#e5e5e5");
      ctx.lineWidth = 1;
      ctx.strokeRect(L.x0 + 0.5, L.y0 + 0.5, L.x1 - L.x0 - 1, L.y1 - L.y0 - 1);
    }

    if (this.opts.legend) this._drawLegend(x, y, b, L, yTop);
    if (this.opts.crosshair && this._hoverTs != null) {
      const hx = x(this._hoverTs);
      ctx.strokeStyle = cssVar("--chart-text", "#a3a3a3");
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, L.y0);
      ctx.lineTo(hx, L.y1);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  _drawAxes(x, y, b, yTop, L, span) {
    const { ctx } = this;
    ctx.font = "10px sans-serif";
    ctx.fillStyle = cssVar("--chart-text", "#a3a3a3");
    ctx.strokeStyle = cssVar("--chart-grid", "#eeeeee");
    ctx.lineWidth = 1;
    // y gridlines: 4 rows
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const v = this.opts.ymin + (i / 4) * (yTop - this.opts.ymin);
      const gy = Math.round(y(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(L.x0, gy);
      ctx.lineTo(L.x1, gy);
      ctx.stroke();
      ctx.fillText(this.opts.fmt(v), L.x0 - 5, gy + 3);
    }
    // x gridlines: ~5 columns
    ctx.textAlign = "center";
    for (let i = 0; i <= 5; i++) {
      const t = b.tMin + (i / 5) * span;
      const gx = Math.round(x(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(gx, L.y0);
      ctx.lineTo(gx, L.y1);
      ctx.stroke();
      ctx.fillText(this._timeLabel(t, span), gx, L.y1 + 12);
    }
  }

  _drawLegend(x, y, b, L, yMax) {
    const { ctx } = this;
    let lx = L.x0;
    ctx.font = "10px sans-serif";
    ctx.textAlign = "left";
    for (const s of this.series) {
      const v = this._valueAt(s, this._hoverTs);
      const label = `${s.label} ${v == null ? "—" : this.opts.fmt(v)}`;
      ctx.fillStyle = this._color(s);
      ctx.fillText(label, lx, 10);
      lx += ctx.measureText(label).width + 16;
    }
    if (this._hoverTs != null) {
      ctx.fillStyle = cssVar("--chart-text", "#a3a3a3");
      ctx.textAlign = "right";
      const span = b.tMax - b.tMin;
      ctx.fillText(this._timeLabel(this._hoverTs, span), L.x1, 10);
      ctx.textAlign = "left";
    }
    // crosshair dots
    if (this._hoverTs != null) {
      for (const s of this.series) {
        const pt = this._pointAt(s, this._hoverTs);
        if (!pt) continue;
        const gx = ((pt[0] - b.tMin) / (b.tMax - b.tMin)) * (L.x1 - L.x0) + L.x0;
        const gy = L.y1 - ((pt[1] - this.opts.ymin) / (yMax - this.opts.ymin)) * (L.y1 - L.y0);
        ctx.beginPath();
        ctx.arc(gx, gy, 3, 0, Math.PI * 2);
        ctx.fillStyle = this._color(s);
        ctx.fill();
      }
    }
  }

  _pointAt(s, ts) {
    // nearest point by timestamp (data is time-ordered)
    const d = s.data;
    if (d.length === 0) return null;
    let lo = 0, hi = d.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (d[mid][0] < ts) lo = mid + 1;
      else hi = mid;
    }
    const c1 = d[lo];
    const c0 = d[Math.max(0, lo - 1)];
    return Math.abs(c1[0] - ts) <= Math.abs(ts - c0[0]) ? c1 : c0;
  }

  _valueAt(s, ts) {
    const p = this._pointAt(s, ts ?? s.data[s.data.length - 1]?.[0]);
    if (!p) return null;
    if (ts != null && Math.abs(p[0] - ts) > (this._bounds()?.tMax - this._bounds()?.tMin) / 4) return null;
    return p[1];
  }
}
