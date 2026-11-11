// Generator: run options, the worker/claim board, and email setup.

import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { card, button, badge, metric } from "../core/ui.js";
import { locateCard, openEnvSetup, ensureEnvironment } from "./generator/setup.js";
import { createMailCard } from "./generator/mail.js";
import { can, isHeadless, platform } from "../core/caps.js";
import { createClaimBatch } from "./claim-batch.js";
import { createAutoGenerate } from "./auto-generate.js";

const WORKER_TONE = {
  running: "ok", starting: "warn", exited: "ok", failed: "err", killed: "err",
};

/* ------------------------------------------------------------- board ---- */

function trimProxy(value) {
  const s = String(value || "");
  return s.length > 34 ? s.slice(0, 34) + "…" : s;
}

function planBadges(w) {
  const out = [];
  if (w.giveaway500) out.push(badge("500M claimed", "warn"));
  else if (w.giveaway500_unavailable) out.push(badge("500M unavailable", "warn"));
  if (w.global_build) out.push(badge("100M global build", "warn"));
   else if (w.start_plan) out.push(badge("Start Plan", "ok"));
   else if (w.starter === true) out.push(badge("Starter", "ok"));
   else if (w.starter === false && !w.start_plan && !w.giveaway500 && !w.global_build) out.push(badge("No plan"));
  return out;
}

function workerCard(w, onStop, onRemove) {
  const live = w.state === "running" || w.state === "starting";
  const lines = (w.lines || []).slice(-6);

  return el("div", { class: "job" }, [
    el("div", { class: "job__head" }, [
      el("span", { class: "dot dot--" + (WORKER_TONE[w.state] || "") + (w.state === "running" ? " dot--live" : "") }),
      el("b", { class: "job__name truncate", text: w.name }),
      badge(w.state, WORKER_TONE[w.state]),
      w.email ? el("span", { class: "mono text-xs muted truncate", text: w.email }) : null,
      w.proxy ? el("span", { class: "mono text-xs faint", title: w.proxy, text: trimProxy(w.proxy) }) : null,
      el("span", { class: "faint text-xs", text: "captcha " + (w.captcha || "—") }),
      ...planBadges(w),
      el("span", { class: "spacer" }),
      live
        ? button({ label: "Kill", variant: "danger", onClick: () => onStop(w.name) })
        : button({ icon: "eyeOff", title: "Remove from list", variant: "quiet", onClick: () => onRemove(w.name) }),
    ]),
    lines.length
      ? el("div", { class: "job__log mono" }, lines.map((l) => el("div", { class: "logpane__line", text: l })))
      : null,
  ]);
}

function claimCard(c, onStop, onRemove) {
  const live = c.state === "running" || c.state === "starting";
  const kind = c.kind === "500m" ? "500M" : c.kind === "start" ? "Start Plan" : "100M";
  const issued = c.result?.summary?.issued;
  const unavailable = c.result?.unavailable;
  const lines = (c.lines || []).slice(-10);

  return el("div", { class: "job job--claim" }, [
    el("div", { class: "job__head" }, [
      el("span", { class: "dot dot--" + (WORKER_TONE[c.state] || "") + (c.state === "running" ? " dot--live" : "") }),
      el("b", { class: "job__name truncate", text: kind + " claim · " + (c.email || c.name) }),
      badge(c.state, WORKER_TONE[c.state]),
      el("span", { class: "mono text-xs muted", text: c.mode || "proxy" }),
      c.proxy ? el("span", { class: "mono text-xs faint", title: c.proxy, text: trimProxy(c.proxy) }) : null,
      el("span", { class: "faint text-xs", text: (c.elapsed || 0) + "s" }),
      issued ? badge("100M issued", "warn") : unavailable ? badge("giveaway unavailable", "warn") : null,
      el("span", { class: "spacer" }),
      live
        ? button({ label: "Kill", variant: "danger", onClick: () => onStop(c.name) })
        : button({ icon: "eyeOff", title: "Remove from list", variant: "quiet", onClick: () => onRemove(c.name) }),
    ]),
    unavailable
      ? el("p", { class: "text-warn text-sm", text:
          (c.result.error || "The giveaway is not available right now.")
          + " Nothing was claimed; retrying through other IPs will not help." })
      : null,
    !live && !issued && c.result?.error
      ? el("p", { class: "field__help", text: "Result: " + c.result.error }) : null,
    lines.length
      ? el("div", { class: "job__log mono" }, lines.map((l) => el("div", { class: "logpane__line", text: l })))
      : null,
  ]);
}

/* -------------------------------------------------------------- view ---- */

export async function mount({ root, signal }) {
  let status = null;
  const claimStates = new Map();
  const mail = createMailCard();
  const batch = createClaimBatch({ signal });
  const auto = createAutoGenerate({ signal });

  const optionsBox = el("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" } });
  const boardBox = el("div", { class: "stack--tight" });
  const summaryBox = el("div", { class: "grid grid--metrics" });
  const savedNote = el("span", { class: "text-ok text-xs", hidden: true, text: "Saved" });
  savedNote.setAttribute("role", "status");
  const saveFeedback = el("span", { class: "text-warn text-xs", role: "status", hidden: true });
  const statusError = el("div", { class: "notice notice--warn", role: "status", hidden: true });

  // Controls are created once so a 3s poll never steals focus mid-edit.
  const controls = {};
  // Option-backed switch: kept outside `controls` because the field wrapper is
  // a <label>, not an input.
  let claimStartOn = true;

  function field(key, label, node, help) {
    controls[key] = node;
    return el("div", { class: "field" }, [
      el("label", { class: "field__label", text: label }),
      node,
      help ? el("div", { class: "field__help", text: help }) : null,
    ]);
  }

  let saveTimer = null;
  let saveQueue = Promise.resolve();
  let feedbackTimer = null;
  let revision = 0;
  let savedRevision = 0;
  let destroyed = false;
  function saveOptions() {
    revision++;
    clearTimeout(feedbackTimer);
    savedNote.hidden = true;
    saveFeedback.hidden = false;
    saveFeedback.className = "text-warn text-xs";
    saveFeedback.textContent = "Unsaved changes";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => persistOptions().catch(() => {}), 600);
  }
  function persistOptions() {
    clearTimeout(saveTimer);
    saveTimer = null;
    const snapshotRevision = revision;
    const body = {
      captcha: controls.captcha.value,
      captcha_strategy: controls.strategy.value,
      ip_source: controls.ip.value,
      proxy_types: controls.ptypes.value,
      country: controls.country.value,
      pin_proxy: controls.pin.value.trim(),
      browser_display: controls.display.value,
      retries: parseInt(controls.retries.value, 10) || 0,
      claim_start: claimStartOn,
      custom_ai_url: controls.aiUrl.value.trim(),
      custom_ai_model: controls.aiModel.value.trim(),
    };
    const aiKey = controls.aiKey.value.trim();
    if (aiKey) body.custom_ai_key = aiKey;
    // Capture controls before queueing: navigation can detach them while an
    // earlier save is still in flight. The flush intentionally outlives signal.
    const saved = saveQueue.then(async () => {
      const result = await api.post("/admin/dash/generator/options", body);
      if (result?.ok === false || result?.error) throw new Error(result.error || "Save rejected");
      savedRevision = Math.max(savedRevision, snapshotRevision);
      if (snapshotRevision === revision) {
        savedNote.hidden = false;
        saveFeedback.hidden = true;
        feedbackTimer = setTimeout(() => { savedNote.hidden = true; }, 1600);
      }
    }).catch(e => {
      if (snapshotRevision === revision) {
        clearTimeout(feedbackTimer);
        savedNote.hidden = true;
        saveFeedback.hidden = false;
        saveFeedback.className = "text-err text-xs";
        saveFeedback.textContent = "Options not saved. Edit to retry.";
      }
      toast("Options were not saved: " + e.message, "err");
      throw e;
    });
    saveQueue = saved.catch(() => {});
    return saved;
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearTimeout(feedbackTimer);
    window.removeEventListener("beforeunload", warnUnsaved);
    if (saveTimer !== null) persistOptions().catch(() => {});
    auto.destroy();
  }
  signal.addEventListener("abort", destroy, { once: true });
  function warnUnsaved(event) {
    if (revision <= savedRevision) return;
    event.preventDefault();
    event.returnValue = "";
  }
  window.addEventListener("beforeunload", warnUnsaved);

  function buildOptions(o) {
    const select = (opts, value) => el("select", {
      class: "select", on: { change: () => { syncVisibility(); saveOptions(); } },
    }, opts.map(([v, label]) => el("option", { value: v, text: label, selected: String(value) === String(v) })));

    claimStartOn = o.claim_start !== false;
    const claimToggle = el("input", {
      class: "switch__input", type: "checkbox", checked: claimStartOn,
    });
    const claimState = el("span", { class: "text-sm muted", text: claimStartOn ? "On" : "Off" });
    claimToggle.addEventListener("change", () => {
      claimStartOn = claimToggle.checked;
      claimState.textContent = claimStartOn ? "On" : "Off";
      saveOptions();
    });

    fill(optionsBox, [
      field("count", "Browsers", el("input", {
        class: "input", type: "number", value: 1, min: 1, max: 8,
      })),
      // "manual" needs a browser window you can actually reach; a headless
      // host would just hang on a captcha nobody can see.
      field("captcha", "Captcha", select([
        ["auto", "auto — CV + AI vote"], ["ai", "ai — AI judge only"],
        ["classic", "classic — CV solver"],
        ...(can("manual_captcha") ? [["manual", "manual — I solve"]] : []),
      ], can("manual_captcha") || o.captcha !== "manual" ? o.captcha : "auto"),
        can("manual_captcha") ? null : "Manual solving needs a display; this host has none."),
      field("strategy", "AI judge model", select([
        ["auto", "auto — CV + AI vote"], ["ai", "ai — AI judge only"], ["classic", "classic — CV solver"],
      ], o.captcha_strategy || "auto"), "How the solver picks the drag distance."),
      field("ip", "IP source", select([
        ["pool", "Proxy pool (" + (o.pool_available || 0) + ")"],
        ["direct", "My network (no proxy)"],
        ["pin", "Pinned proxy"],
      ], ["pool", "socks"].includes(o.ip_source) ? "pool" : o.ip_source)),
      field("ptypes", "Proxy types", select([
        ["all", "All types (" + ((o.types_counts || {}).all || 0) + ")"],
        ["socks", "socks4 + socks5 (" + ((o.types_counts || {}).socks || 0) + ")"],
        ["socks5", "socks5 only (" + ((o.types_counts || {}).socks5 || 0) + ")"],
        ["socks4", "socks4 only (" + ((o.types_counts || {}).socks4 || 0) + ")"],
        ["http", "http(s) only (" + ((o.types_counts || {}).http || 0) + ")"],
      ], o.proxy_types || "socks")),
      field("country", "Country", select([
        ["ALL", "Any country"],
        ...Object.entries(o.countries_available || {}).map(([c, n]) => [c, c + " (" + n + ")"]),
      ], o.country || "ALL")),
      field("pin", "Pinned proxy", el("input", {
        class: "input input--mono", type: "text", value: o.pin_proxy || "",
        placeholder: "http://ip:port or socks5://…", on: { input: saveOptions },
      })),
      field("display", "Browser display", select([
        ["auto", platform() === "linux" ? "Automatic (desktop or Xvfb)" : "Desktop window"],
        ...(platform() === "linux" ? [
          ...(can("visible_browser") ? [["desktop", "Desktop window"]] : []),
          ["virtual", "Virtual display (background)"],
        ] : []),
        ...(platform() === "windows" ? [["headless", "Native headless (background)"]] : []),
      ], platform() === "linux" ? (o.browser_display || (o.headless ? "virtual" : "auto"))
        : platform() === "windows" && o.browser_display === "headless" ? "headless" : "auto"),
        platform() === "linux"
          ? "Background uses headed Chromium on Xvfb, not Chromium headless. Applies to new workers."
          : "Native headless hides the Windows browser but may be rejected by the provider. Manual captcha requires a desktop window. Applies to new workers, not Auto Generate."),
      field("retries", "Retries", el("input", {
        class: "input", type: "number", value: o.retries ?? 5, min: 0, max: 10, on: { input: saveOptions },
      })),
      field("claim", "After signup", el("label", { class: "switch" }, [
        claimToggle, el("span", { class: "switch__track" }), claimState,
      ]), "Claims the Start Plan and any Global/Weekend Build card on offer. "
        + "Off means a signup is kept only if the provider granted a plan by itself."),
    ]);
    controls.captcha.addEventListener("change", () => {
      if (controls.captcha.value !== "manual") controls.strategy.value = controls.captcha.value;
      saveOptions();
    });
    controls.strategy.addEventListener("change", () => {
      if (controls.captcha.value !== "manual") controls.captcha.value = controls.strategy.value;
      saveOptions();
    });
  }

  function syncVisibility() {
    const captcha = controls.captcha?.value;
    const strategyField = controls.strategy?.closest(".field");
    if (strategyField) strategyField.hidden = captcha === "manual";

    // Manual solving needs a window you can actually see. On a host with no
    // display the option is not offered at all, so this only fires where a
    // visible window is genuinely possible.
    if (captcha === "manual" && controls.display && (can("visible_browser") || platform() === "windows")) {
      controls.display.value = platform() === "linux" ? "desktop" : "auto";
      controls.display.disabled = true;
      controls.display.title = "Manual captcha needs a desktop window";
    } else if (controls.display) {
      controls.display.disabled = !["linux", "windows"].includes(platform());
      controls.display.title = "";
    }

    if (controls.ptypes) {
      const usesPool = controls.ip?.value === "pool";
      controls.ptypes.disabled = !usesPool;
      controls.ptypes.title = usesPool ? "" : "Applies to the proxy pool source";
    }
    if (controls.pin) {
      controls.pin.closest(".field").hidden = controls.ip?.value !== "pin";
    }
  }

  async function start() {
    if (!(await ensureEnvironment())) return;
    const count = parseInt(controls.count.value, 10) || 1;
    try {
      await persistOptions();
      const r = await api.post("/admin/dash/generator/start", { count });
      if (r?.error) { toast(r.error, "err"); return; }
      toast("Started " + count + " worker" + (count === 1 ? "" : "s"), "ok");
      load();
    } catch (e) { toast("start: " + e.message, "err"); }
  }

  async function stopAll() {
    const ok = await confirmDialog({
      title: "Stop every worker?",
      body: "Stops signup workers. Use Stop batch for the queued claims, or Kill on an individual claim.",
      okLabel: "Stop all", danger: true,
    });
    if (!ok) return;
    try { await api.post("/admin/dash/generator/stopall"); toast("Stopping all workers", "ok"); load(); }
    catch (e) { toast(e.message, "err"); }
  }

  // Claims are a separate job table from signup workers, with their own
  // endpoints — stopping a claim through the worker route is a 404.
  const stopWorker = async (name) => {
    try { await api.post("/admin/dash/generator/stop", { name }); load(); }
    catch (e) { toast(e.message, "err"); }
  };
  const removeWorker = async (name) => {
    try { await api.post("/admin/dash/generator/workers/remove", { name }); load(); }
    catch (e) { toast(e.message, "err"); }
  };
  const stopClaim = async (name) => {
    try { await api.post("/admin/claims/stop", { name }); load(); }
    catch (e) { toast(e.message, "err"); }
  };
  const removeClaim = async (name) => {
    try { await api.post("/admin/claims/remove", { name }); load(); }
    catch (e) { toast(e.message, "err"); }
  };
  const clearFinished = async () => {
    try {
      await api.post("/admin/dash/generator/workers/clear");
      await api.post("/admin/claims/clear");
      load();
    } catch (e) { toast(e.message, "err"); }
  };

  function announceClaims(claims) {
    for (const c of claims) {
      const prev = claimStates.get(c.name);
      const terminal = ["exited", "failed", "killed"].includes(c.state);
      if (prev === "running" && terminal) {
        const kind = c.kind === "500m" ? "500M" : c.kind === "start" ? "Start Plan" : "100M";
        if (c.result?.summary?.issued) toast("100M issued: " + c.email, "ok");
        else if (c.result?.unavailable) toast(kind + " giveaway unavailable — nothing claimed for " + c.email, "warn");
        else if (c.state === "exited") toast(kind + " claim finished: " + c.email, "ok");
        else toast(kind + " claim finished (not issued): " + c.email, "err");
      }
      claimStates.set(c.name, c.state);
    }
  }

  function paintBoard(st) {
    const workers = st.workers || [];
    const claims = st.claims || [];
    announceClaims(claims);

    const running = workers.filter((w) => w.state === "running" || w.state === "starting").length;
    const done = workers.filter((w) => w.state === "exited").length;
    const failed = workers.filter((w) => ["failed", "killed"].includes(w.state)).length;

    fill(summaryBox, [
      metric({ label: "Running", value: running, tone: running ? "ok" : null }),
      metric({ label: "Finished", value: done }),
      metric({ label: "Failed", value: failed, tone: failed ? "err" : null }),
      metric({ label: "Claims", value: claims.length }),
    ]);

    const cards = [
      ...claims.map((c) => claimCard(c, stopClaim, removeClaim)),
      ...workers.map((w) => workerCard(w, stopWorker, removeWorker)),
    ];
    fill(boardBox, cards.length
      ? cards
      : el("div", { class: "empty" }, [
          el("div", { class: "empty__title", text: "No workers running" }),
          el("div", { class: "empty__text", text: "Press Start to launch the signup pipeline." }),
        ]));
  }

  function buildShell(st) {
    const actions = el("div", { class: "row" }, [
      button({ label: "Start", icon: "bolt", variant: "primary", onClick: start }),
      button({ label: "Stop all", onClick: stopAll }),
      button({ label: "Environment setup", icon: "settings", onClick: () => openEnvSetup() }),
      el("span", { class: "spacer" }),
      button({ label: "Clear finished", variant: "quiet", onClick: clearFinished }),
    ]);

    const aiCard = card({
      title: "AI judge endpoint",
      desc: "Optional. Your own OpenAI-compatible vision model; empty uses this gateway.",
      body: el("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" } }, [
        field("aiUrl", "Endpoint URL", el("input", {
          class: "input input--mono", type: "text", value: st.options?.custom_ai_url || "",
          placeholder: "https://api.openai.com/v1", on: { input: saveOptions },
        })),
        field("aiModel", "Model ID", el("input", {
          class: "input input--mono", type: "text", value: st.options?.custom_ai_model || "",
          placeholder: "e.g. gpt-4o-mini", on: { input: saveOptions },
        })),
        field("aiKey", "API key", el("input", {
          class: "input", type: "password", autocomplete: "off",
          placeholder: st.options?.custom_ai_key_set ? "Saved — type to replace" : "sk-…",
          on: { input: saveOptions },
        })),
      ]),
    });

    fill(root, el("div", { class: "stack" }, [
      statusError,
      isHeadless()
        ? el("div", { class: "notice notice--info" }, [
            el("b", { text: "Headless host. " }),
            el("span", { text:
              "No desktop display is available. Signups use headed Chromium on an invisible Xvfb screen "
              + "and automatic captcha solving. Xvfb must be installed; manual solving is unavailable." }),
          ])
        : null,
      summaryBox,
      auto.node,
      actions,
      batch.node,
      card({
        title: "Options",
        actions: el("span", {}, [savedNote, saveFeedback]),
        body: optionsBox,
      }),
      el("div", { class: "cols" }, [aiCard, mail.node]),
      card({ title: "Workers", body: boardBox, flush: true }),
      card({
        title: "Pipeline",
        body: el("p", { class: "field__help", text:
          "signup → device login → app activity → billing preview → Aliyun captcha → billing claim → "
          + "the account joins the pool automatically." }),
      }),
    ]));
  }

  const load = async () => {
    if (signal.aborted || destroyed) return;
    let st;
    try {
      st = await api.get("/admin/dash/generator/status", { signal });
    } catch (e) {
      if (signal.aborted || destroyed || e.name === "AbortError") return;
      // Only an actual missing-scripts response warrants the locator. A brief
      // restart/network failure must not erase the current worker board.
      if (e.status === 404) {
        fill(root, locateCard(e.message, () => load()));
        status = null;
      } else {
        statusError.textContent = "Gateway temporarily unavailable: " + e.message + ". Use Refresh to retry; generator files have not necessarily disappeared.";
        statusError.hidden = false;
        if (!status) fill(root, statusError);
      }
      return;
    }
    if (signal.aborted || destroyed) return;
    statusError.hidden = true;
    const firstPaint = !status;
    status = st;
    if (firstPaint) {
      buildShell(st);
      buildOptions(st.options || {});
      syncVisibility();
      mail.refresh();
    }
    paintBoard(st);
    batch.refresh();
    auto.refresh();
  };

  await load();
  return { refresh: load, destroy };
}
