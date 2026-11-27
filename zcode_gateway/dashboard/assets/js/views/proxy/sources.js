// User-supplied proxies and scrape-source configuration, not a pool explorer.
import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { card, table, button } from "../../core/ui.js";
import { num } from "../../core/format.js";
import { t } from "../../core/i18n.js";

export function createSourcesCard(onChanged) {
  const importBox = el("textarea", { class: "textarea input--mono", rows: 4, spellcheck: false,
    placeholder: t("src.add.ph") });
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
        importMsg.textContent = t("src.add.file.loaded");
      };
      reader.readAsText(file);
    } } });
  const sourcesBox = el("textarea", { class: "textarea input--mono", rows: 6, spellcheck: false,
    placeholder: t("src.sources.ph") });
  const sourcesMsg = el("span", { class: "muted text-xs" });
  const yieldBox = el("div");

  async function doImport() {
    if (!importBox.value.trim()) { importMsg.textContent = t("src.add.nothing"); return; }
    try {
      const result = await api.post("/admin/dash/generator/proxies/import", {
        text: importBox.value, default_proto: protoSelect.value,
      });
      importMsg.textContent = t("src.add.imported", { n: num(result.imported) });
      importBox.value = "";
      onChanged?.();
    } catch (error) { importMsg.textContent = t("src.add.failed", { msg: error.message }); }
  }
  async function loadSources() {
    try {
      const result = await api.get("/admin/dash/generator/proxies/sources");
      if (document.activeElement !== sourcesBox) sourcesBox.value = result.text || "";
    } catch (error) { sourcesMsg.textContent = t("src.sources.load_failed", { msg: error.message }); }
  }
  async function saveSources() {
    try {
      const result = await api.post("/admin/dash/generator/proxies/sources", { text: sourcesBox.value });
      sourcesMsg.textContent = t("src.sources.saved", { n: num(result.sources) });
    } catch (error) { sourcesMsg.textContent = t("src.sources.save_failed", { msg: error.message }); }
  }
  async function loadYield() {
    try {
      const result = await api.get("/admin/dash/generator/proxies/yield");
      fill(yieldBox, table({
        columns: [{ label: t("src.yield.col.source") }, { label: t("src.yield.col.found"), align: "right" },
          { label: t("src.yield.col.avg"), align: "right" }, { label: t("src.yield.col.runs"), align: "right" }, { label: t("src.yield.col.errors"), align: "right" }],
        rows: result.sources || [],
        empty: { title: t("src.yield.empty.title"), text: t("src.yield.empty.body") },
        render: source => [
          el("span", { class: "mono text-xs truncate", title: source.url || "",
            style: { maxWidth: "340px", display: "block" },
            text: String(source.url || "").replace(/^https?:\/\//, "") }),
          num(source.found || 0), num(source.avg || 0), num(source.runs || 0), num(source.errors || 0),
        ],
      }));
    } catch (error) { fill(yieldBox, el("p", { class: "field__help", text: t("src.yield.unavailable", { msg: error.message }) })); }
  }
  const node = card({ title: t("src.title"), desc: t("src.desc"),
    body: el("div", { class: "stack" }, [
      el("div", { class: "field" }, [el("label", { class: "field__label", text: t("src.add.label") }),
        importBox, el("div", { class: "row" }, [protoSelect,
          button({ label: t("src.add.btn"), variant: "primary", onClick: doImport }),
          button({ label: t("src.add.file"), onClick: () => fileInput.click() }), fileInput, importMsg]),
        el("p", { class: "field__help", text: t("src.add.help") })]),
      el("details", { class: "help-details", on: { toggle: event => { if (event.target.open) loadSources(); } } }, [
        el("summary", { text: t("src.sources") }), el("div", { class: "stack" }, [sourcesBox,
          el("div", { class: "row" }, [button({ label: t("src.sources.save"), onClick: saveSources }),
            button({ label: t("src.sources.reload"), icon: "refresh", onClick: loadSources }), sourcesMsg])])]),
      el("details", { class: "help-details", on: { toggle: event => { if (event.target.open) loadYield(); } } }, [
        el("summary", { text: t("src.yield") }), yieldBox]),
    ]),
  });
  return { node };
}
