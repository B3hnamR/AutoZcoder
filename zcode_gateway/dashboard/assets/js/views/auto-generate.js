import { el, fill, copyText } from "../core/dom.js";
import { toast } from "../core/toast.js";
import { api } from "../core/api.js";
import { card, button, stateBadge } from "../core/ui.js";

const PATH = "/admin/dash/generator/auto";
const DEFAULTS = {
  mode: "goal", target: 20, attempt_limit: 100, concurrency: 1,
  cooldown_seconds: 60, ip_source: "direct", captcha: "ai", browser_display: "background",
  timeout_seconds: 900, failure_limit: 5,
};
const TERMINAL = new Set(["idle", "completed", "stopped", "failed"]);
let nextID = 0;

export function createAutoGenerate({ signal } = {}) {
  const controller = new AbortController();
  const prefix = "auto-" + (++nextID) + "-";
  const controls = {};
  let disposed = false;
  let hydrated = false;
  let saved = false;
  let dirty = false;
  let busy = "";
  let healthy = false;
  let status = { state: "idle" };
  let runConfig = null;
  let runID = null;
  let requestVersion = 0;
  let pendingRefresh = null;

  const note = el("p", { class: "auto-note", role: "status", "aria-live": "polite", text: "Loading saved configuration..." });
  const error = el("p", { class: "auto-error", role: "alert", hidden: true });
  const connection = el("p", { class: "auto-error", role: "status", hidden: true });
  const state = el("span");
  const progress = el("p", { class: "auto-progress", text: "Loading session status..." });
  const profile = el("p", { class: "auto-note" });
  const reason = el("p", { class: "auto-note", hidden: true });
  const counts = el("dl", { class: "auto-counts", "aria-label": "Session counts" });
  const slots = el("ul", { class: "auto-slots", "aria-label": "Browser slots" });
  const logPane = el("div", { class: "logpane mono auto-log", role: "region", tabindex: 0,
    "aria-label": "Auto Generate live session log" });
  const follow = el("input", { type: "checkbox", checked: true });
  let lastLog = "";
  const copyLog = button({ label: "Copy log", icon: "copy", onClick: async () => {
    const ok = await copyText(lastLog);
    toast(ok ? "Auto Generate log copied" : "Clipboard unavailable", ok ? "ok" : "err");
  } });

  function field(key, label, input, help) {
    controls[key] = input;
    input.id = prefix + key;
    input.name = key;
    input.setAttribute("aria-describedby", prefix + key + "-help");
    return el("div", { class: "field auto-field" }, [
      el("label", { class: "field__label", htmlFor: input.id, text: label }), input,
      el("p", { class: "field__help auto-help", id: prefix + key + "-help", text: help }),
    ]);
  }
  const number = (key, min, help, label) => field(key, label, el("input", {
    class: "input", type: "number", min, step: 1, required: true, value: DEFAULTS[key],
  }), help);
  const modeField = field("mode", "Run mode", el("select", { class: "select" }, [
    el("option", { value: "goal", text: "Goal: new qualified Start Plan successes" }),
    el("option", { value: "attempts", text: "Attempts: fixed signup budget" }),
  ]), "Only new, qualified Start Plan successes count toward the goal.");
  const targetField = number("target", 1, "Stop once this many new accounts qualify. No-plan and duplicate results do not count.", "Qualified success goal");
  const attemptField = number("attempt_limit", 1, "Mandatory safety budget in goal mode; the run stops at this many launched attempts even if the goal is not reached.", "Maximum attempts");
  const concurrencyField = number("concurrency", 1, "Keep 1 for the first run. Up to 3 browser slots can run concurrently.", "Concurrent browsers");
  controls.concurrency.max = 3;
  const cooldownField = number("cooldown_seconds", 0, "Seconds a slot waits before its next attempt. Active attempts keep their own timeout.", "Cooldown (seconds)");
  const ipField = field("ip_source", "IP source", el("select", { class: "select" }, [
    el("option", { value: "direct", text: "Direct (no selected proxy)" }),
    el("option", { value: "proxy", text: "Proxy pool" }),
  ]), "Direct skips proxy selection. Your system VPN/TUN and OS routing still apply.");
  const displayField = field("browser_display", "Browser display", el("select", { class: "select" }, [
    el("option", { value: "background", text: "Background (invisible)" }),
    el("option", { value: "desktop", text: "Desktop window" }),
  ]), "Windows background uses native headless Chromium, which the provider may reject. Linux background uses Xvfb. Desktop requires a real display. Applies only to this Auto Generate profile.");
  const captchaField = field("captcha", "Captcha solver", el("input", {
    class: "input", value: "AI judge only", type: "text", readOnly: true,
  }), "Uses the AI judge endpoint on this page.");
  const timeoutField = number("timeout_seconds", 1, "Maximum seconds for a single attempt before it is stopped.", "Attempt timeout (seconds)");
  const failureField = number("failure_limit", 1, "Stop after this many consecutive failures. Review the reason before starting again.", "Consecutive failure limit");
  for (const [key, min, max] of [["target", 1, 1000], ["attempt_limit", 1, 10000],
    ["cooldown_seconds", 0, 3600], ["timeout_seconds", 60, 3600], ["failure_limit", 1, 100]]) {
    controls[key].min = min;
    controls[key].max = max;
  }
  const saveButton = button({ label: "Save config", onClick: saveConfig });
  const startButton = button({ label: "Auto Generate", variant: "primary", icon: "bolt", onClick: () => mutate("start") });
  const pauseButton = button({ label: "Pause", onClick: () => mutate("pause") });
  const resumeButton = button({ label: "Resume", onClick: () => mutate("resume") });
  const stopButton = button({ label: "Stop session", variant: "danger", onClick: () => mutate("stop") });
  const retryButton = button({ label: "Refresh status", onClick: refresh });
  const tuning = el("details", { class: "help-details auto-tuning" }, [
    el("summary", { text: "Browser and retry settings" }),
    el("div", { class: "auto-fields" }, [concurrencyField, cooldownField, displayField,
      captchaField, timeoutField, failureField]),
  ]);
  const form = el("form", { class: "auto-form", on: {
    submit: (event) => { event.preventDefault(); saveConfig(); },
    input: edited, change: edited,
  } }, [
    el("div", { class: "auto-fields" }, [modeField, targetField, attemptField, ipField]),
    tuning,
    el("div", { class: "auto-actions" }, [saveButton, startButton, note]),
  ]);
  form.addEventListener("invalid", (event) => {
    if (tuning.contains(event.target)) tuning.open = true;
  }, true);
  const logControls = el("div", { class: "auto-actions auto-log-controls", hidden: true }, [
    el("label", { class: "checkline" }, [follow, el("span", { text: "Follow latest" })]), copyLog,
  ]);
  const logEmpty = el("p", { class: "auto-note", text: "No session events yet." });
  const node = card({ title: "Auto Generate", body: [
    form, error, connection,
    el("section", { class: "auto-session", "aria-label": "Auto Generate session" }, [
      el("div", { class: "auto-session-head" }, [state, progress,
        el("div", { class: "auto-actions" }, [pauseButton, resumeButton, stopButton, retryButton])]),
      profile, counts, reason, slots,
      logEmpty, logControls, logPane,
    ]),
  ] });
  node.classList.add("auto-generate");

  function live() { return !TERMINAL.has(status.state); }
  function edited() {
    dirty = true;
    saved = false;
    error.hidden = true;
    renderControls();
  }
  function readConfig() {
    const config = { mode: controls.mode.value, ip_source: controls.ip_source.value, captcha: "ai",
      browser_display: controls.browser_display.value || DEFAULTS.browser_display };
    for (const key of ["target", "attempt_limit", "concurrency", "cooldown_seconds", "timeout_seconds", "failure_limit"]) {
      config[key] = Number(controls[key].value);
    }
    if (config.mode === "attempts" && (!Number.isInteger(config.target) || config.target < 1 || config.target > 1000)) config.target = DEFAULTS.target;
    return config;
  }
  function writeConfig(config) {
    for (const [key, input] of Object.entries(controls)) {
      if (key !== "captcha") input.value = config[key] ?? DEFAULTS[key];
    }
  }
  function renderControls() {
    const locked = !hydrated || !!busy || live() || disposed;
    for (const input of Object.values(controls)) input.disabled = locked;
    targetField.hidden = controls.mode.value !== "goal";
    controls.target.required = !targetField.hidden;
    controls.target.disabled = locked || targetField.hidden;
    controls.attempt_limit.setCustomValidity(!targetField.hidden && Number(controls.attempt_limit.value) < Number(controls.target.value)
      ? "Maximum attempts must be at least the qualified success goal." : "");
    saveButton.disabled = locked || !healthy;
    startButton.disabled = locked || !healthy || !saved || dirty;
    pauseButton.hidden = !["preparing", "running", "starting", "cooling"].includes(status.state);
    resumeButton.hidden = !["paused", "interrupted"].includes(status.state);
    stopButton.hidden = !live();
    pauseButton.disabled = resumeButton.disabled = !!busy || !healthy || disposed;
    stopButton.disabled = !!busy || !healthy || disposed || status.state === "stopping";
    retryButton.disabled = !!busy || disposed;
    form.setAttribute("aria-busy", String(!!busy || !hydrated));
    note.textContent = busy ? ({ save: "Saving configuration...", start: "Starting session...",
      pause: "Pausing session...", resume: "Resuming session...", stop: "Stopping session..." }[busy])
      : !hydrated ? "Loading saved configuration..."
      : live() ? "Session profile is locked. Stop or finish this session before editing."
      : dirty ? "Unsaved changes. Save config before starting."
      : saved ? "Configuration saved. Ready to Auto Generate."
      : "Review the saved profile, then Save config to enable Auto Generate.";
  }
  function paint(data) {
    if (data?.error) throw new Error(String(data.error));
    if (!data?.config || !data?.status?.state) throw new Error("Invalid Auto Generate response. Refresh status to retry.");
    if (!hydrated) {
      writeConfig(data.config);
      hydrated = true;
    }
    // Another dashboard can save a different profile. Never start that unseen
    // profile under the label of this form; require an explicit save again.
    if (saved && Object.keys(DEFAULTS).some((key) => (data.config[key] ?? DEFAULTS[key]) !== readConfig()[key])) {
      saved = false;
      dirty = true;
    }
    status = data.status;
    if (status.config || runID !== status.id || !runConfig) {
      runID = status.id;
      runConfig = { ...(status.config || data.config) };
    }
    healthy = true;
    connection.hidden = true;
    fill(state, stateBadge(status.state));
    const count = (value) => Math.max(0, Number(value) || 0);
    const mode = status.mode || runConfig.mode;
    const target = count(status.target ?? status.goal ?? runConfig.target);
    progress.textContent = status.state === "idle" ? "No session started."
      : mode === "goal" ? count(status.qualified) + " / " + target + " new qualified Start Plan successes"
      : count(status.launched) + " / " + count(status.attempt_limit ?? runConfig.attempt_limit) + " attempts launched";
    profile.textContent = status.state === "idle" ? "Saved configuration survives gateway restarts; sessions require an explicit start."
      : (status.id ? "Session " + status.id + " | " : "") + (mode === "goal" ? "Goal mode" : "Attempts mode")
        + " | " + count(runConfig.concurrency) + " browser slots | " + count(runConfig.cooldown_seconds)
        + "s cooldown | " + runConfig.ip_source + " | "
        + (runConfig.browser_display === "desktop" ? "Desktop window" : "Background (invisible)") + " | AI only";
    counts.hidden = status.state === "idle";
    fill(counts, [["launched", "Attempts reserved"], ["completed", "Finished attempts"], ["qualified", "Qualified"],
      ["failed", "Unsuccessful"], ["no_plan", "No plan"], ["duplicates", "Duplicates"], ["active", "Active"], ["cooling", "Cooling"]]
      .map(([key, label]) => el("div", { class: "auto-count" }, [el("dt", { text: label }), el("dd", { text: String(count(status[key])) })])));
    const details = [];
    if (status.state === "interrupted") details.push("Session interrupted. Review the status, then choose Resume to continue.");
    if (status.reason) details.push("Reason: " + status.reason);
    if (count(status.remaining_cooldown_seconds)) details.push("Next slot available in " + Math.ceil(count(status.remaining_cooldown_seconds)) + "s.");
    reason.textContent = details.join(" ");
    reason.hidden = !details.length;
    fill(slots, (status.slots || []).map((slot, index) => el("li", { class: "auto-slot" }, [
      el("strong", { text: "Slot " + (slot.id ?? index + 1) }),
      el("span", { text: slot.state || "idle" }),
      el("span", { class: "auto-note", text: slot.worker_name || slot.name || "" }),
      slot.remaining_cooldown_seconds ? el("span", { class: "auto-note", text: Math.ceil(count(slot.remaining_cooldown_seconds)) + "s remaining" }) : null,
    ])));
    const lines = (status.events || []).map(event => {
      const stamp = new Date(event.ts * 1000).toLocaleTimeString();
      return "[" + stamp + "] " + (event.worker ? event.worker + " | " : "") + event.message;
    });
    logEmpty.hidden = !!lines.length;
    logControls.hidden = logPane.hidden = !lines.length;
    const text = lines.join("\n");
    if (text !== lastLog || !logPane.children.length) {
      const scroll = logPane.scrollTop;
      fill(logPane, lines.map((line, index) => el("div", {
        class: "logpane__line" + ((status.events[index].level === "error") ? " logpane__line--err"
          : status.events[index].level === "warn" ? " logpane__line--warn" : ""), text: line,
      })));
      logPane.scrollTop = follow.checked ? logPane.scrollHeight : scroll;
      lastLog = text;
    }
    copyLog.disabled = !lines.length;
    renderControls();
  }
  function showError(action, exception) {
    if (disposed || exception.name === "AbortError") return;
    error.textContent = action + ": " + exception.message + ". Refresh status to check the server before retrying.";
    error.hidden = false;
  }
  async function saveConfig() {
    if (disposed || busy || !hydrated || live() || !healthy || !form.reportValidity()) return;
    busy = "save";
    requestVersion++;
    error.hidden = true;
    const config = readConfig();
    renderControls();
    try {
      const data = await api.put(PATH, config, { signal: controller.signal });
      if (disposed) return;
      paint(data);
      writeConfig(data.config);
      dirty = false;
      saved = true;
    } catch (exception) { saved = false; showError("Configuration was not saved", exception); }
    finally { busy = ""; if (!disposed) renderControls(); }
  }
  async function mutate(action) {
    if (disposed || busy || !healthy || !hydrated) return;
    if (action === "start" && (live() || dirty || !saved || !form.reportValidity())) return;
    if (action === "resume" && !["paused", "interrupted"].includes(status.state)) return;
    if (action === "pause" && !["preparing", "running", "starting", "cooling"].includes(status.state)) return;
    if (action === "stop" && (!live() || status.state === "stopping")) return;
    busy = action;
    requestVersion++;
    error.hidden = true;
    if (action === "start") runConfig = readConfig();
    renderControls();
    try {
      const data = await api.post(PATH + "/" + action, {}, { signal: controller.signal });
      if (!disposed) paint(data);
    } catch (exception) {
      showError("Could not " + action + " session", exception);
      healthy = false;
    } finally { busy = ""; if (!disposed) renderControls(); }
  }
  function refresh() {
    if (disposed || busy) return Promise.resolve();
    if (pendingRefresh) return pendingRefresh;
    const version = requestVersion;
    pendingRefresh = (async () => {
      try {
        const data = await api.get(PATH, { signal: controller.signal });
        if (!disposed && version === requestVersion) paint(data);
      } catch (exception) {
        if (disposed || exception.name === "AbortError" || version !== requestVersion) return;
        healthy = false;
        connection.textContent = "Auto Generate status unavailable: " + exception.message + ". Use Refresh status to retry; no session action was sent.";
        connection.hidden = false;
        renderControls();
        if (!hydrated) {
          note.textContent = "Configuration not loaded. Refresh status to retry.";
          progress.textContent = "Session status unavailable.";
        }
      } finally { pendingRefresh = null; }
    })();
    return pendingRefresh;
  }
  function destroy() {
    disposed = true;
    controller.abort();
    signal?.removeEventListener("abort", destroy);
    renderControls();
  }
  signal?.addEventListener("abort", destroy, { once: true });
  renderControls();
  if (signal?.aborted) destroy();
  else refresh();
  return { node, refresh, destroy };
}

export async function mount({ root, signal }) {
  const view = createAutoGenerate({ signal });
  let judgeLoaded = false;
  const error = el("p", { class: "text-err text-sm", role: "status" });
  const fields = {};
  const field = (key, label, attrs = {}) => {
    const input = el("input", { class: "input", ...attrs });
    fields[key] = input;
    return el("label", { class: "field" }, [el("span", { class: "field__label", text: label }), input]);
  };
  const keyPlaceholder = (saved) => saved ? "Saved. Leave blank to keep it." : "Enter API key";
  const save = button({ label: "Save AI judge", disabled: true });
  const form = el("form", { class: "stack" }, [
    el("div", { class: "auto-fields auto-judge-fields" }, [
      field("custom_ai_url", "Endpoint URL", { type: "url", placeholder: "https://api.openai.com/v1" }),
      field("custom_ai_model", "Model ID", { placeholder: "e.g. gpt-4o-mini" }),
      field("custom_ai_key", "API key", { type: "password", autocomplete: "new-password" }),
    ]), save, error,
  ]);
  save.type = "submit";
  const key = fields.custom_ai_key;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    for (const input of Object.values(fields)) input.readOnly = true;
    error.textContent = "";
    try {
      const body = Object.fromEntries(Object.entries(fields).map(([name, input]) => [name, input.value.trim()]));
      if (!body.custom_ai_key) delete body.custom_ai_key;
      const result = await api.post("/admin/dash/generator/options", body);
      if (result?.ok === false || result?.error) throw new Error(result.error || "Save rejected");
      if (signal.aborted) return;
      if (body.custom_ai_key) key.placeholder = keyPlaceholder(true);
      key.value = "";
      toast("AI judge settings saved", "ok");
    } catch (e) {
      if (!signal.aborted) error.textContent = e.message;
    } finally {
      save.disabled = false;
      for (const input of Object.values(fields)) input.readOnly = false;
    }
  });
  for (const input of Object.values(fields)) input.readOnly = true;
  root.append(el("div", { class: "stack" }, [view.node, card({
    title: "AI judge endpoint",
    desc: "Use an OpenAI-compatible vision model. Leave the endpoint blank to use this gateway.",
    body: form,
  })]));
  async function loadJudge() {
    if (judgeLoaded) return;
    try {
      const { options = {} } = await api.get("/admin/dash/generator/status", { signal });
      if (signal.aborted || judgeLoaded) return;
      judgeLoaded = true;
      fields.custom_ai_url.value = options.custom_ai_url || "";
      fields.custom_ai_model.value = options.custom_ai_model || "";
      key.placeholder = keyPlaceholder(options.custom_ai_key_set);
      save.disabled = false;
      error.textContent = "";
      for (const input of Object.values(fields)) input.readOnly = false;
    } catch (e) {
      if (!signal.aborted) error.textContent = "Could not load AI judge settings. Use Refresh to retry.";
    }
  }
  await loadJudge();
  const refresh = view.refresh;
  view.refresh = async () => { await Promise.all([refresh(), loadJudge()]); };
  return view;
}
