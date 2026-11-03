// Proxy tools: the scraper/checker, pool maintenance, health, and edge relays.

import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast } from "../core/toast.js";
import { card, button, metric } from "../core/ui.js";
import { locateCard } from "./generator/setup.js";
import { createRelaysCard } from "./proxy/relays.js";
import { createSourcesCard } from "./proxy/sources.js";
import { num, duration } from "../core/format.js";

const PRESETS = {
  balanced: { latency: 6, timeout: 10, conc: 400, pconc: 300, ptimeout: 3 },
  thorough: { latency: 9, timeout: 20, conc: 250, pconc: 200, ptimeout: 5 },
  fast: { latency: 4, timeout: 6, conc: 700, pconc: 500, ptimeout: 2 },
};

// label, key, tone, explanation
const COUNTERS = [
  ["Raw list", "raw", null, "Scraped candidates waiting to be checked."],
  ["Usable now", "pool_file", "ok", "Verified proxies ready to hand out right now."],
  ["Working", "healthy", "ok", "The latest check passed and the proxy is not used up."],
  ["Failing", "failing", "err", "Checked, and the latest check failed."],
  ["Never checked", "unverified", "warn", "Imported or scraped, not yet proven."],
  ["SOCKS", "socks", null, "SOCKS proxies in the usable list — preferred for signups."],
  ["Used up", "used", "warn", "Already consumed by a signup or claim."],
  ["Bad / MITM", "bad", "err", "Flagged as intercepting HTTPS, or otherwise unusable."],
];

export async function mount({ root, signal }) {
  let built = false;
  const loadWarning = el("div", { class: "notice notice--warn", role: "status", hidden: true });
  const relays = createRelaysCard();
  const sources = createSourcesCard(() => load());

  const autoToggle = el("input", { class: "switch__input", type: "checkbox" });
  const autoInterval = el("select", { class: "select", style: { maxWidth: "150px" } },
    [[30, "every 30 min"], [45, "every 45 min"], [90, "every 90 min"], [180, "every 3 h"]]
      .map(([v, label]) => el("option", { value: v, text: label, selected: v === 45 })));
  const autoState = el("span", { class: "muted text-xs" });

  function paintAutoState(r) {
    autoState.textContent = r.auto_verify
      ? (r.next_run_in_s ? "Next run " + duration(r.next_run_in_s) + " from now." : "Scheduled.")
      : "";
  }

  async function saveAutoVerify() {
    try {
      paintAutoState(await api.post("/admin/dash/proxy/auto_verify", {
        enabled: autoToggle.checked,
        interval_min: parseInt(autoInterval.value, 10) || 45,
      }));
    } catch (e) { toast("auto-verify: " + e.message, "err"); }
  }
  autoToggle.addEventListener("change", saveAutoVerify);
  autoInterval.addEventListener("change", () => { if (autoToggle.checked) saveAutoVerify(); });

  // Current state rides along with the socks badge; there is no GET for the
  // toggle itself.
  async function loadAutoVerify() {
    try {
      const r = await api.get("/admin/dash/proxy/socks_ready");
      autoToggle.checked = !!r.auto_verify;
      if (r.auto_interval_min) autoInterval.value = String(r.auto_interval_min);
      paintAutoState(r);
      return r;
    } catch (_) { return null; }
  }

  const countersBox = el("div", { class: "grid grid--metrics" });
  const progressRow = el("div", { class: "row", hidden: true });
  const progressBar = el("div", { class: "bar", style: { flex: "1", minWidth: "160px", height: "6px" } },
    el("div", { class: "bar__fill", style: { width: "0%" } }));
  const progressText = el("span", { class: "mono text-xs muted" });
  const statusLine = el("div", { class: "text-sm muted" });
  const networkWarning = el("div", { class: "notice notice--warn", role: "status", hidden: true });
  const logPane = el("div", { class: "logpane mono", style: { height: "240px" } });
  const maintMsg = el("span", { class: "muted text-xs" });
  const countryLine = el("div", { class: "field__help" });

  fill(progressRow, [progressBar, progressText]);

  const controls = {};
  function field(key, label, node, width) {
    controls[key] = node;
    if (width) node.style.width = width;
    return el("div", { class: "field" }, [el("label", { class: "field__label", text: label }), node]);
  }

  const runButtons = {};

  function applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    controls.latency.value = p.latency;
    controls.timeout.value = p.timeout;
    controls.conc.value = p.conc;
    controls.pconc.value = p.pconc;
    controls.ptimeout.value = p.ptimeout;
  }

  async function run(action) {
    try {
      await api.post("/admin/dash/generator/proxies/run", {
        action,
        max_latency: parseFloat(controls.latency.value) || 6,
        timeout: parseFloat(controls.timeout.value) || 10,
        concurrency: parseInt(controls.conc.value, 10) || 400,
        probe_concurrency: parseInt(controls.pconc.value, 10) || 300,
        probe_timeout: parseFloat(controls.ptimeout.value) || 3,
        dead_cache_hours: parseInt(controls.dead.value, 10) || 0,
        prefilter: controls.prefilter.checked,
        retry_failed: controls.retry.checked,
      });
      toast("Proxy " + action + " started", "ok");
      load();
    } catch (e) { toast(action + ": " + e.message, "err"); }
  }

  async function stop() {
    try { await api.post("/admin/dash/generator/proxies/stop"); toast("Stopping", "ok"); load(); }
    catch (e) { toast(e.message, "err"); }
  }

  async function maintain(action) {
    maintMsg.textContent = "Working…";
    try {
      const r = await api.post("/admin/dash/generator/proxies/maintain", { action });
      maintMsg.textContent = r.message || "Done";
      load();
    } catch (e) { maintMsg.textContent = e.message; }
  }

  async function lookupCountries() {
    try {
      await api.post("/admin/dash/generator/proxies/countries", {});
      toast("Country lookup started", "ok");
      load();
    } catch (e) { toast(e.message, "err"); }
  }

  function build(px) {
    const ps = px.stats || {};

    const presetSelect = el("select", {
      class: "select", on: { change: (e) => applyPreset(e.target.value) },
    }, Object.keys(PRESETS).map((p) => el("option", { value: p, text: p, selected: p === "balanced" })));

    const scanControls = el("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))" } }, [
      field("preset", "Scan preset", presetSelect),
      field("latency", "Max latency (s)", el("input", { class: "input", type: "number", value: ps.max_latency || 6, step: "0.5", min: 1 })),
      field("timeout", "Timeout (s)", el("input", { class: "input", type: "number", value: 10, min: 2, max: 60 })),
      field("conc", "Checks", el("input", { class: "input", type: "number", value: 400, min: 20, max: 5000 })),
      field("pconc", "TCP probes", el("input", { class: "input", type: "number", value: 300, min: 10, max: 5000 })),
      field("ptimeout", "Probe timeout (s)", el("input", { class: "input", type: "number", value: 3, min: 1, max: 30, step: "0.5" })),
      field("dead", "Dead cache (h)", el("input", { class: "input", type: "number", value: 0, min: 0, max: 720 })),
    ]);

    controls.prefilter = el("input", { type: "checkbox", checked: true });
    controls.retry = el("input", { type: "checkbox", checked: true });

    runButtons.all = button({ label: "Scrape + verify", variant: "primary", onClick: () => run("all") });
    runButtons.verify = button({ label: "Verify raw", onClick: () => run("verify") });
    runButtons.recheck = button({ label: "Recheck pool", icon: "refresh", onClick: () => run("recheck") });
    runButtons.stop = button({ label: "Stop", variant: "danger", onClick: stop });

    const checker = card({
      title: "Proxy checker",
      desc: "A proxy qualifies when the z.ai shell loads, the captcha host is reachable, TLS is valid (anti-MITM) and latency is under the cap.",
      body: el("div", { class: "stack" }, [
        countersBox,
        scanControls,
        el("div", { class: "row" }, [
          el("label", { class: "checkline" }, [controls.prefilter, el("span", { text: "TCP prefilter" })]),
          el("label", { class: "checkline", title: "Retry timeouts and resets once — recovers roughly 30% more proxies" },
            [controls.retry, el("span", { text: "Second chance" })]),
          el("span", { class: "spacer" }),
          runButtons.all, runButtons.verify, runButtons.recheck, runButtons.stop,
        ]),
        progressRow,
        statusLine,
        networkWarning,
        el("div", { class: "row", style: { paddingTop: "8px", borderTop: "1px solid var(--border-subtle)" } }, [
          el("label", { class: "switch" }, [
            autoToggle, el("span", { class: "switch__track" }),
            el("span", { class: "text-sm", text: "Auto re-verify" }),
          ]),
          autoInterval,
          el("span", { class: "faint text-xs", text: "Keeps the pool fresh so claims never hit a stale set." }),
          autoState,
        ]),
        el("div", { class: "row row--between" }, [
          el("span", { class: "field__help", text: "Live check log" }),
          button({ label: "Clear", variant: "quiet", onClick: () => fill(logPane, []) }),
        ]),
        logPane,
      ]),
    });

    const maintenance = card({
      title: "Pool maintenance",
      desc: "A failed check adds one to a proxy's score, a pass removes one. Usable means the latest check passed and the score is at most 3.",
      body: el("div", { class: "stack" }, [
        el("div", { class: "row" }, [
          button({ label: "Prune unhealthy", title: "Drop proxies unhealthy for two weeks", onClick: () => maintain("prune") }),
          button({ label: "Clear bad list", title: "Forget the MITM / TLS blacklist", onClick: () => maintain("clear-bad") }),
          button({ label: "Clear used list", title: "Allow used proxies to be picked again", onClick: () => maintain("clear-used") }),
          button({ label: "Clear dead cache", title: "Re-check hosts that recently failed", onClick: () => maintain("clear-dead") }),
          maintMsg,
        ]),
        el("div", { class: "row" }, [
          countryLine,
          button({ label: "Look up countries", onClick: lookupCountries }),
          el("span", { class: "faint text-xs", text: "Uses free public lookup sites; nothing is downloaded." }),
        ]),
      ]),
    });

    fill(root, el("div", { class: "stack" }, [
      loadWarning, checker, sources.node, maintenance, relays.node,
    ]));
    applyPreset("balanced");
    relays.refresh();
    loadAutoVerify();
    built = true;
  }

  function paint(px) {
    const ps = px.stats || {};
    networkWarning.textContent = ps.network_warning || "";
    networkWarning.hidden = !ps.network_warning;
    const probed = ps.probed ?? 0;
    const total = ps.total ?? 0;
    const pct = total > 0 ? Math.round((probed / total) * 100) : 0;

    fill(countersBox, COUNTERS.map(([label, key, tone, hint]) => {
      const node = metric({ label, value: num(ps[key] ?? 0), tone: ps[key] ? tone : null });
      node.title = hint;
      return node;
    }));

    progressRow.hidden = !(px.running && total);
    progressBar.firstElementChild.style.width = pct + "%";
    progressText.textContent = probed + " / " + total;

    statusLine.textContent = px.running
      ? "Checking… " + (px.action || "") + " (" + (px.elapsed || 0) + "s)"
      : px.state === "exited" ? "Finished"
      : px.state === "failed" ? "Failed" : "Idle";

    for (const [key, btn] of Object.entries(runButtons)) {
      if (key === "stop") btn.hidden = !px.running;
      else btn.disabled = !!px.running;
    }

    const lines = (px.lines || []).slice(-60);
    const atBottom = logPane.scrollHeight - logPane.scrollTop - logPane.clientHeight < 48;
    fill(logPane, lines.length
      ? lines.map((l) => el("div", { class: "logpane__line", text: l }))
      : el("div", { class: "faint", text: "No check output yet." }));
    if (atBottom) logPane.scrollTop = logPane.scrollHeight;

    countryLine.textContent = (ps.countries_known ?? 0) + " of " + (ps.pool_json || 0)
      + " proxies have a known country";
  }

  const load = async () => {
    if (signal.aborted) return;
    let px;
    try {
      px = await api.get("/admin/dash/generator/proxies", { signal });
    } catch (e) {
      if (signal.aborted || e.name === "AbortError") return;
      if (e.status === 404) {
        fill(root, locateCard(e.message, () => { built = false; load(); }));
        built = false;
      } else {
        loadWarning.textContent = "Proxy status unavailable: " + e.message + ". Use Refresh to retry.";
        loadWarning.hidden = false;
        if (!built) fill(root, loadWarning);
      }
      return;
    }
    if (signal.aborted) return;
    loadWarning.hidden = true;
    if (!built) build(px);
    paint(px);
  };

  await load();
  return { refresh: load };
}
