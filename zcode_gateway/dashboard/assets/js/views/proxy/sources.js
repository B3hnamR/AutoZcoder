// User-supplied proxies and scrape-source configuration, not a pool explorer.
import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { card, table, button } from "../../core/ui.js";
import { num } from "../../core/format.js";

export function createSourcesCard(onChanged) {
  const importBox = el("textarea", { class: "textarea input--mono", rows: 4, spellcheck: false,
    placeholder: "one per line: ip:port, http://ip:port, socks5://user:pass@ip:port" });
  const protoSelect = el("select", { class: "select", style: { maxWidth: "140px" } },
    ["http", "https", "socks4", "socks5"].map(p =>
      el("option", { value: p, text: p + " (default)", selected: p === "http" })));
  const importMsg = el("span", { class: "muted text-xs" });
  const fileInput = el("input", { type: "file", accept: ".txt,.csv,text/plain", hidden: true,
    on: { change: event => {
      const file = event.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        importBox.value = String(reader.result || "").slice(0, 200000);
        importMsg.textContent = "File loaded. Press Add to pool.";
      };
      reader.readAsText(file);
    } } });
  const sourcesBox = el("textarea", { class: "textarea input--mono", rows: 6, spellcheck: false,
    placeholder: "One scrape URL per line." });
  const sourcesMsg = el("span", { class: "muted text-xs" });
  const yieldBox = el("div");

  async function doImport() {
    if (!importBox.value.trim()) { importMsg.textContent = "Nothing to import."; return; }
    try {
      const result = await api.post("/admin/dash/generator/proxies/import", {
        text: importBox.value, default_proto: protoSelect.value,
      });
      importMsg.textContent = "Imported " + num(result.imported) + ".";
      importBox.value = "";
      onChanged?.();
    } catch (error) { importMsg.textContent = "Import failed: " + error.message; }
  }
  async function loadSources() {
    try {
      const result = await api.get("/admin/dash/generator/proxies/sources");
      if (document.activeElement !== sourcesBox) sourcesBox.value = result.text || "";
    } catch (error) { sourcesMsg.textContent = "Load failed: " + error.message; }
  }
  async function saveSources() {
    try {
      const result = await api.post("/admin/dash/generator/proxies/sources", { text: sourcesBox.value });
      sourcesMsg.textContent = "Saved (" + num(result.sources) + " sources).";
    } catch (error) { sourcesMsg.textContent = "Save failed: " + error.message; }
  }
  async function loadYield() {
    try {
      const result = await api.get("/admin/dash/generator/proxies/yield");
      fill(yieldBox, table({
        columns: [{ label: "Source" }, { label: "Found", align: "right" },
          { label: "Avg/run", align: "right" }, { label: "Runs", align: "right" }, { label: "Errors", align: "right" }],
        rows: result.sources || [],
        empty: { title: "No scans yet", text: "Run a scan to measure which sources produce candidates." },
        render: source => [
          el("span", { class: "mono text-xs truncate", title: source.url || "",
            style: { maxWidth: "340px", display: "block" },
            text: String(source.url || "").replace(/^https?:\/\//, "") }),
          num(source.found || 0), num(source.avg || 0), num(source.runs || 0), num(source.errors || 0),
        ],
      }));
    } catch (error) { fill(yieldBox, el("p", { class: "field__help", text: "Yield unavailable: " + error.message })); }
  }
  const node = card({ title: "Sources & import", desc: "Add proxies you already have, and control where scans look.",
    body: el("div", { class: "stack" }, [
      el("div", { class: "field" }, [el("label", { class: "field__label", text: "Add proxies to the pool" }),
        importBox, el("div", { class: "row" }, [protoSelect,
          button({ label: "Add to pool", variant: "primary", onClick: doImport }),
          button({ label: "Load from file", onClick: () => fileInput.click() }), fileInput, importMsg]),
        el("p", { class: "field__help", text: "Imported proxies stay pending until a real check proves them." })]),
      el("details", { class: "help-details", on: { toggle: event => { if (event.target.open) loadSources(); } } }, [
        el("summary", { text: "Scrape sources" }), el("div", { class: "stack" }, [sourcesBox,
          el("div", { class: "row" }, [button({ label: "Save sources", onClick: saveSources }),
            button({ label: "Reload", icon: "refresh", onClick: loadSources }), sourcesMsg])])]),
      el("details", { class: "help-details", on: { toggle: event => { if (event.target.open) loadYield(); } } }, [
        el("summary", { text: "Source yield" }), yieldBox]),
    ]),
  });
  return { node };
}
