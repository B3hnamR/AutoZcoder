// Logs: the process ring buffer, filtered by level and text.
//
// The chrome is built once and only the pane repaints, so a 3s poll can never
// steal focus or reset the caret while you are typing a search.

import { el, fill, debounce, copyText } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast } from "../core/toast.js";
import { card, toolbar, button } from "../core/ui.js";

const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];

function levelClass(l) {
  if (l === "ERROR" || l === "CRITICAL") return "logpane__line--err";
  if (l === "WARNING") return "logpane__line--warn";
  if (l === "DEBUG") return "faint";
  return "";
}

export async function mount({ root, signal }) {
  let level = "";
  let search = "";
  let cleared = false;
  let records = [];

  const pane = el("div", { class: "logpane mono", style: { height: "min(62vh, 560px)" } });

  function paint() {
    if (cleared) {
      fill(pane, el("div", { class: "faint", text: "View cleared. New records appear as they arrive." }));
      return;
    }
    if (!records.length) {
      fill(pane, el("div", { class: "faint", text: "No log records match." }));
      return;
    }
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60;
    fill(pane, records.map((r) => el("div", { class: "logpane__line " + levelClass(r.level) }, [
      el("span", { class: "faint", text: r.time + " " }),
      el("span", { text: r.level + " " }),
      el("span", { class: "muted", text: r.logger + ": " }),
      el("span", { text: r.message }),
    ])));
    if (atBottom) pane.scrollTop = pane.scrollHeight;
  }

  const load = async () => {
    const qs = new URLSearchParams({ limit: "400" });
    if (level) qs.set("level", level);
    if (search) qs.set("q", search);
    const data = await api.get("/admin/dash/logs?" + qs.toString(), { signal });
    records = data.records || [];
    paint();
  };

  const levelSelect = el("select", {
    class: "select", style: { maxWidth: "140px" }, "aria-label": "Filter by level",
    on: { change: (e) => { level = e.target.value; cleared = false; load(); } },
  }, LEVELS.map((l) => el("option", { value: l, text: l || "All levels" })));

  const searchInput = el("input", {
    class: "input", type: "search", placeholder: "Search logs…",
    style: { maxWidth: "240px" }, "aria-label": "Search logs",
    on: { input: debounce((e) => { search = e.target.value; cleared = false; load(); }, 300) },
  });

  const clearBtn = button({
    label: "Clear", onClick: () => {
      cleared = !cleared;
      clearBtn.querySelector("span:last-child").textContent = cleared ? "Show" : "Clear";
      paint();
    },
  });

  const bar = toolbar([levelSelect, searchInput], el("div", { class: "row row--tight" }, [
    button({ label: "Copy", icon: "copy", onClick: async () => {
      const text = records.map((r) => [r.time, r.level, r.logger + ":", r.message].join(" ")).join("\n");
      const ok = await copyText(text);
      toast(ok ? "Logs copied" : "Could not copy", ok ? "ok" : "err", 1600);
    } }),
    clearBtn,
  ]));

  fill(root, el("div", { class: "stack" }, [
    bar,
    card({
      title: "Process log",
      desc: "In-memory ring buffer. Restarting the gateway clears it.",
      body: pane,
    }),
  ]));

  await load();
  return { refresh: load };
}
