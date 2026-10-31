// Minimal SVG charts. SVG rather than canvas so they inherit theme colours,
// stay crisp on any DPI, and need no redraw on resize.

import { el } from "./dom.js";

const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, String(v));
  }
  return node;
}

export function compactNumber(n) {
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
}

/**
 * barChart({data, height, format})
 * data: [{label, value, title?, tone?}]
 * Bars scale to the widest container via viewBox + preserveAspectRatio=none,
 * while labels stay upright in HTML underneath.
 */
export function barChart({ data, height = 150, format = compactNumber, emptyText = "No data yet" }) {
  if (!data || !data.length) {
    return el("div", { class: "chart chart--empty faint", style: { height: height + "px" }, text: emptyText });
  }

  const max = Math.max(1, ...data.map((d) => Number(d.value) || 0));
  const cols = el("div", { class: "chart__cols", style: { height: height + "px" } },
    data.map((d) => {
      const value = Number(d.value) || 0;
      const pct = (value / max) * 100;
      const bar = el("div", {
        class: "chart__bar" + (d.tone ? " chart__bar--" + d.tone : ""),
        style: { height: (value > 0 ? Math.max(2, pct) : 0) + "%" },
      });
      return el("div", {
        class: "chart__col",
        title: d.title || (d.label + ": " + format(value)),
        tabindex: 0,
        "aria-label": d.title || (d.label + ": " + format(value)),
      }, [el("div", { class: "chart__track" }, bar), el("span", { class: "chart__label", text: d.label })]);
    }));

  return el("div", { class: "chart" }, cols);
}

/** Horizontal ranked bars — better than vertical when labels are long. */
export function rankedBars({ data, format = compactNumber, emptyText = "No data yet", max: maxItems = 8 }) {
  const rows = (data || []).slice(0, maxItems);
  if (!rows.length) return el("div", { class: "faint text-sm", text: emptyText });

  const max = Math.max(1, ...rows.map((d) => Number(d.value) || 0));
  return el("div", { class: "ranked" }, rows.map((d) => el("div", { class: "ranked__row" }, [
    el("span", { class: "ranked__label truncate", text: d.label, title: d.label }),
    el("span", { class: "ranked__track" },
      el("span", { class: "ranked__fill", style: { width: ((Number(d.value) || 0) / max) * 100 + "%" } })),
    el("span", { class: "ranked__value mono text-xs", text: format(d.value) }),
  ])));
}

/**
 * sparkline({points, width, height, tone}) — an inline trend line.
 */
export function sparkline({ points, width = 160, height = 36, tone = "accent" }) {
  const values = (points || []).map((p) => Number(p) || 0);
  if (values.length < 2) return el("span", { class: "faint text-xs", text: "—" });

  const max = Math.max(...values), min = Math.min(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const d = values.map((v, i) =>
    (i ? "L" : "M") + (i * step).toFixed(1) + " " + (height - ((v - min) / span) * height).toFixed(1)).join(" ");

  const svg = svgEl("svg", {
    viewBox: "0 0 " + width + " " + height,
    width, height,
    preserveAspectRatio: "none",
    class: "spark spark--" + tone,
    "aria-hidden": "true",
  });
  svg.append(svgEl("path", { d, fill: "none", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round" }));
  return svg;
}
