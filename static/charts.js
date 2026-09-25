// Client-side chart renderer. Charts are specs built server-side (charts.py) and embedded as JSON inside
// <figure class="chart">; rendering at the real pixel width keeps text legible on phones. Every chart also has a
// server-rendered data table, so nothing depends on this script or on hovering.
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const FMT = {
    pct: (v) => Math.round(v * 100) + "%",
    int: (v) => Math.round(v).toLocaleString(),
    num1: (v) => v.toFixed(1),
    signed1: (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1),
  };
  const fmt = (name, v) => (FMT[name] || FMT.num1)(v);

  function el(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    for (const k in attrs || {}) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function text(parent, x, y, str, cls, anchor) {
    const t = el("text", { x, y, class: cls || "ch-tick", "text-anchor": anchor || "start" }, parent);
    t.textContent = str;
    return t;
  }
  let measurer;
  function textWidth(str, size) {
    measurer = measurer || document.createElement("canvas").getContext("2d");
    measurer.font = (size || 12) + "px system-ui, -apple-system, 'Segoe UI', sans-serif";
    return measurer.measureText(str).width;
  }

  // Tooltip: value leads, label follows; built with textContent only (labels are data).
  function tooltip(fig) {
    let tip = fig.querySelector(".ch-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "ch-tip";
      tip.hidden = true;
      fig.querySelector(".chart-plot").appendChild(tip);
    }
    return {
      show(title, rows, x, y, plotW) {
        tip.replaceChildren();
        if (title) {
          const h = document.createElement("div");
          h.className = "ch-tip-title";
          h.textContent = title;
          tip.appendChild(h);
        }
        for (const r of rows) {
          const row = document.createElement("div");
          row.className = "ch-tip-row";
          if (r.slot) {
            const key = document.createElement("span");
            key.className = "ch-key s" + r.slot;
            row.appendChild(key);
          }
          const v = document.createElement("strong");
          v.textContent = r.value;
          row.appendChild(v);
          if (r.name) {
            const n = document.createElement("span");
            n.textContent = r.name;
            row.appendChild(n);
          }
          tip.appendChild(row);
        }
        tip.hidden = false;
        const w = tip.offsetWidth;
        tip.style.left = Math.max(0, Math.min(x + 12, plotW - w)) + "px";
        tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
      },
      hide() { tip.hidden = true; },
    };
  }

  function renderLine(fig, plot, spec) {
    const W = plot.clientWidth, H = spec.height || 240;
    const yLabels = spec.y.ticks.map((t) => fmt(spec.y.fmt, t));
    const left = Math.ceil(Math.max(...yLabels.map((s) => textWidth(s)))) + 10;
    const endLabels = spec.series.filter((s) => s.end_label);
    const right = endLabels.length ? Math.ceil(Math.max(...endLabels.map((s) => textWidth(s.name + " " + fmt(spec.y.fmt, s.points[s.points.length - 1][1]))))) + 14 : 12;
    const top = spec.edge ? 22 : 10, bottom = spec.x.ticks ? 26 : 10;
    const pw = Math.max(40, W - left - right), ph = H - top - bottom;
    const sx = (x) => left + ((x - spec.x.min) / (spec.x.max - spec.x.min || 1)) * pw;
    const sy = (y) => top + (1 - (y - spec.y.min) / (spec.y.max - spec.y.min || 1)) * ph;
    const svg = el("svg", { width: W, height: H, role: "img", "aria-label": fig.getAttribute("aria-label") || "" });

    for (let i = 0; i < spec.y.ticks.length; i++) {
      const y = sy(spec.y.ticks[i]);
      el("line", { x1: left, x2: left + pw, y1: y, y2: y, class: i === 0 ? "ch-base" : "ch-grid" }, svg);
      text(svg, left - 6, y + 4, yLabels[i], "ch-tick", "end");
    }
    // Tick marks at x.tick_marks when given (labels then sit between them, e.g. quarters), else at each label.
    for (const x of spec.x.tick_marks || (spec.x.ticks || []).map((t) => t[0])) {
      el("line", { x1: sx(x), x2: sx(x), y1: top + ph, y2: top + ph + 4, class: "ch-base" }, svg);
    }
    for (const [x, label] of spec.x.ticks || []) text(svg, sx(x), H - 8, label, "ch-tick", "middle");
    for (const r of spec.refs || []) {
      el("line", { x1: left, x2: left + pw, y1: sy(r.y), y2: sy(r.y), class: "ch-ref" }, svg);
      if (r.label) text(svg, left + pw - 4, sy(r.y) + 15, r.label, "ch-note", "end");
    }
    if (spec.diag) {
      el("line", { x1: sx(spec.x.min), y1: sy(spec.y.min), x2: sx(spec.x.max), y2: sy(spec.y.max), class: "ch-ref" }, svg);
      if (spec.diag_label) text(svg, sx(spec.x.max) - 4, sy(spec.y.max) + 14, spec.diag_label, "ch-note", "end");
    }
    if (spec.edge) {
      text(svg, left, 13, spec.edge.top, "ch-note");
      text(svg, left, top + ph - 6, spec.edge.bottom, "ch-note");
    }

    for (const s of spec.series) {
      const d = s.points.map((p, i) => (i ? "L" : "M") + sx(p[0]).toFixed(1) + " " + sy(p[1]).toFixed(1)).join("");
      if (spec.area && s.points.length) {
        const base = sy(spec.area_base != null ? spec.area_base : spec.y.min);
        el("path", { d: d + "L" + sx(s.points[s.points.length - 1][0]) + " " + base + "L" + sx(s.points[0][0]) + " " + base + "Z", class: "ch-area s" + s.slot }, svg);
      }
      el("path", { d, class: "ch-line s" + s.slot }, svg);
      if (spec.markers) for (const p of s.points) el("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 4, class: "ch-dot s" + s.slot }, svg);
      if (s.points.length) {
        const last = s.points[s.points.length - 1];
        if (!spec.markers) el("circle", { cx: sx(last[0]), cy: sy(last[1]), r: 4, class: "ch-dot s" + s.slot }, svg);
        if (s.end_label) text(svg, sx(last[0]) + 8, sy(last[1]) + 4, s.name + " " + fmt(spec.y.fmt, last[1]), "ch-label");
      }
    }

    // Crosshair snaps to the nearest x; the readout lists every series there.
    const xs = [...new Set(spec.series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
    const cross = el("line", { y1: top, y2: top + ph, class: "ch-cross", visibility: "hidden" }, svg);
    const hi = el("g", {}, svg);
    const tip = tooltip(fig);
    let idx = -1;
    function show(i) {
      idx = Math.max(0, Math.min(xs.length - 1, i));
      const x = xs[idx];
      cross.setAttribute("x1", sx(x));
      cross.setAttribute("x2", sx(x));
      cross.setAttribute("visibility", "visible");
      hi.replaceChildren();
      const rows = [];
      let title = "", ymin = Infinity;
      for (const s of spec.series) {
        const p = s.points.find((q) => q[0] === x);
        if (!p) continue;
        el("circle", { cx: sx(x), cy: sy(p[1]), r: 5, class: "ch-dot s" + s.slot }, hi);
        rows.push({ slot: spec.series.length > 1 ? s.slot : null, value: fmt(spec.y.fmt, p[1]) + (p[3] ? " " + p[3] : ""), name: spec.series.length > 1 ? s.name : "" });
        title = title || p[2] || "";
        ymin = Math.min(ymin, sy(p[1]));
      }
      tip.show(title, rows, sx(x), Math.min(ymin, top + ph / 2), W);
    }
    function hide() { cross.setAttribute("visibility", "hidden"); hi.replaceChildren(); tip.hide(); }
    const hit = el("rect", { x: left, y: 0, width: pw, height: H, fill: "transparent" }, svg);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(), px = e.clientX - r.left;
      let best = 0;
      for (let i = 1; i < xs.length; i++) if (Math.abs(sx(xs[i]) - px) < Math.abs(sx(xs[best]) - px)) best = i;
      show(best);
    });
    hit.addEventListener("pointerleave", hide);
    fig.onkeydown = (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        show(idx < 0 ? xs.length - 1 : idx + (e.key === "ArrowRight" ? 1 : -1));
        e.preventDefault();
      } else if (e.key === "Escape") hide();
    };
    fig.onblur = hide;
    plot.replaceChildren(svg);
  }

  // Horizontal bars from a shared baseline; negatives run left in the opposing hue.
  function renderBars(fig, plot, spec) {
    const W = plot.clientWidth, rowH = 30, barH = 16;
    const narrow = W < 480;
    const labelW = narrow ? 0 : Math.ceil(Math.max(...spec.rows.map((r) => textWidth(r.label, 13)))) + 12;
    const values = spec.rows.map((r) => r.value);
    const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
    const valueW = Math.ceil(Math.max(...values.map((v) => textWidth(fmt(spec.fmt, v))))) + 8;
    const x0 = labelW + (lo < 0 ? valueW : 0), x1 = W - (hi > 0 ? valueW : 4);
    const sx = (v) => x0 + ((v - lo) / (hi - lo || 1)) * (x1 - x0);
    const rowTotal = narrow ? rowH + 16 : rowH;
    const H = spec.rows.length * rowTotal + 6;
    const svg = el("svg", { width: W, height: H, role: "img", "aria-label": fig.getAttribute("aria-label") || "" });
    const zero = sx(0);
    const tip = tooltip(fig);
    spec.rows.forEach((r, i) => {
      const yTop = i * rowTotal + (narrow ? 16 : 0), cy = yTop + rowH / 2;
      const g = el("g", { class: "ch-bar-row", tabindex: "0" }, svg);
      if (narrow) text(g, 0, yTop - 3, r.label, "ch-label");
      else text(g, 0, cy + 4, r.label, "ch-label");
      const w = Math.abs(sx(r.value) - zero);
      if (w >= 0.5) {
        const neg = r.value < 0, rad = Math.min(4, w), y = cy - barH / 2;
        const xs = neg ? zero - w : zero;
        // 4px rounded data end, square at the baseline.
        const d = neg
          ? `M${zero} ${y}H${xs + rad}A${rad} ${rad} 0 0 0 ${xs} ${y + rad}V${y + barH - rad}A${rad} ${rad} 0 0 0 ${xs + rad} ${y + barH}H${zero}Z`
          : `M${zero} ${y}H${zero + w - rad}A${rad} ${rad} 0 0 1 ${zero + w} ${y + rad}V${y + barH - rad}A${rad} ${rad} 0 0 1 ${zero + w - rad} ${y + barH}H${zero}Z`;
        el("path", { d, class: "ch-bar " + (neg ? "neg" : "pos") }, g);
      }
      const lab = fmt(spec.fmt, r.value);
      text(g, r.value < 0 ? zero - w - 6 : zero + w + 6, cy + 4, lab, "ch-value", r.value < 0 ? "end" : "start");
      el("rect", { x: 0, y: yTop - (narrow ? 16 : 0), width: W, height: rowTotal, fill: "transparent" }, g);
      const on = () => tip.show(r.label, [{ value: lab + (spec.unit ? " " + spec.unit : ""), name: r.note || "" }], Math.min(Math.max(sx(r.value), zero), W - 40), yTop, W);
      g.addEventListener("pointerenter", on);
      g.addEventListener("focus", on);
      g.addEventListener("pointerleave", () => tip.hide());
      g.addEventListener("blur", () => tip.hide());
    });
    el("line", { x1: zero, x2: zero, y1: 0, y2: H, class: "ch-base" }, svg);
    plot.replaceChildren(svg);
  }

  function render(fig) {
    const src = fig.querySelector("script[type='application/json']");
    const plot = fig.querySelector(".chart-plot");
    if (!src || !plot || !plot.clientWidth) return;
    const spec = JSON.parse(src.textContent);
    (spec.type === "bars" ? renderBars : renderLine)(fig, plot, spec);
    fig.classList.add("chart-ready");
  }

  window.renderCharts = function (root) {
    (root || document).querySelectorAll("figure.chart").forEach(render);
  };
  let width = window.innerWidth;
  window.addEventListener("resize", () => {
    if (window.innerWidth === width) return;
    width = window.innerWidth;
    clearTimeout(window.__chartResize);
    window.__chartResize = setTimeout(() => window.renderCharts(), 150);
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => window.renderCharts());
  else window.renderCharts();
})();
