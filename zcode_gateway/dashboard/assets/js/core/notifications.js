// Notification bell: warnings and errors the gateway raised while you were
// looking at another section.

import { el, fill } from "./dom.js";
import { api } from "./api.js";
import { button } from "./ui.js";
import { relative, parseTime, num } from "./format.js";

let since = 0;
let unseen = 0;
let state = {};
let panel = null;
let bell = null;
let dot = null;

function toneFor(level) {
  const l = String(level || "").toUpperCase();
  if (l === "ERROR" || l === "CRITICAL") return "err";
  if (l === "WARNING") return "warn";
  return "";
}

async function openPanel() {
  closePanel();
  let events = [];
  try {
    const r = await api.get("/admin/dash/notifications?since=0");
    events = r.events || [];
    state = r.state || {};
  } catch (e) {
    events = [];
  }

  panel = el("div", { class: "notify-panel", role: "dialog", "aria-label": "Notifications" }, [
    el("div", { class: "row row--tight notify-panel__head" }, [
      el("b", { text: "Notifications" }),
      el("span", { class: "faint text-xs",
                   text: num(state.errors_24h || 0) + " errors · " + num(state.warnings_24h || 0) + " warnings (24h)" }),
      el("span", { class: "spacer" }),
      button({ label: "Clear", variant: "quiet", onClick: clear }),
    ]),
    el("div", { class: "notify-panel__list" }, events.length
      ? events.slice().reverse().map((e) => el("div", { class: "notify-item" }, [
          el("span", { class: "dot dot--" + toneFor(e.level) }),
          el("div", { class: "notify-item__body" }, [
            el("div", { class: "notify-item__text", text: e.message || "" }),
            el("div", { class: "faint text-xs", text: (e.logger || "") + " · " + (e.ts ? relative(parseTime(e.ts)) : "") }),
          ]),
        ]))
      : el("div", { class: "empty__text", text: "Nothing to report." })),
  ]);
  document.body.append(panel);

  unseen = 0;
  paint();

  setTimeout(() => document.addEventListener("mousedown", onOutside), 0);
}

function onOutside(e) {
  if (panel && !panel.contains(e.target) && e.target !== bell) closePanel();
}

function closePanel() {
  document.removeEventListener("mousedown", onOutside);
  panel?.remove();
  panel = null;
}

async function clear() {
  try { await api.post("/admin/dash/notifications/clear"); } catch (_) { /* best effort */ }
  closePanel();
  unseen = 0;
  paint();
}

function paint() {
  if (!bell) return;
  bell.hidden = unseen === 0 && !(state.errors_24h || state.warnings_24h);
  if (dot) dot.hidden = unseen === 0;
  bell.title = unseen ? unseen + " new notification" + (unseen === 1 ? "" : "s") : "Notifications";
}

export function init(bellButton, dotNode) {
  bell = bellButton;
  dot = dotNode;
  bell.addEventListener("click", () => (panel ? closePanel() : openPanel()));
  poll();
  setInterval(poll, 15000);
}

export async function poll() {
  try {
    const r = await api.get("/admin/dash/notifications?since=" + since);
    const events = r.events || [];
    state = r.state || {};
    if (events.length) {
      unseen += events.length;
      since = Math.max(since, ...events.map((e) => Number(e.id) || 0));
    }
    paint();
  } catch (_) { /* bell is decorative */ }
}
