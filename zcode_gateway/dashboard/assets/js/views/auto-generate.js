import { el, fill, copyText } from "../core/dom.js";
import { toast } from "../core/toast.js";
import { api } from "../core/api.js";
import { card, button, stateBadge } from "../core/ui.js";
import { t } from "../core/i18n.js";

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

  const note = el("p", { class: "auto-note", role: "status", "aria-live": "polite", text: t("auto.note.loading") });
  const error = el("p", { class: "auto-error", role: "alert", hidden: true });
  const connection = el("p", { class: "auto-error", role: "status", hidden: true });
  const state = el("span");
  const progress = el("p", { class: "auto-progress", text: t("auto.progress.loading") });
  const profile = el("p", { class: "auto-note" });
  const reason = el("p", { class: "auto-note", hidden: true });
  const counts = el("dl", { class: "auto-counts", "aria-label": "Session counts" });
  const slots = el("ul", { class: "auto-slots", "aria-label": "Browser slots" });
  const logPane = el("div", { class: "logpane mono auto-log", role: "region", tabindex: 0,
    "aria-label": "Auto Generate live session log" });
  const follow = el("input", { type: "checkbox", checked: true });
  let lastLog = "";
  const copyLog = button({ label: t("auto.copy_log"), icon: "copy", onClick: async () => {
    const ok = await copyText(lastLog);
    toast(ok ? t("auto.copy_log.ok") : t("auto.copy_log.fail"), ok ? "ok" : "err");
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
  const modeField = field("mode", t("auto.field.mode"), el("select", { class: "select" }, [
    el("option", { value: "goal", text: t("auto.field.mode.goal") }),
    el("option", { value: "attempts", text: t("auto.field.mode.attempts") }),
  ]), t("auto.field.mode.help"));
  const targetField = number("target", 1, t("auto.field.target.help"), t("auto.field.target"));
  const attemptField = number("attempt_limit", 1, t("auto.field.attempts.help"), t("auto.field.attempts"));
  const concurrencyField = number("concurrency", 1, t("auto.field.concurrency.help"), t("auto.field.concurrency"));
  controls.concurrency.max = 3;
  const cooldownField = number("cooldown_seconds", 0, t("auto.field.cooldown.help"), t("auto.field.cooldown"));
  const ipField = field("ip_source", t("auto.field.ip_source"), el("select", { class: "select" }, [
    el("option", { value: "direct", text: t("auto.field.ip_source.direct") }),
    el("option", { value: "proxy", text: t("auto.field.ip_source.proxy") }),
  ]), t("auto.field.ip_source.help"));
  const displayField = field("browser_display", t("auto.field.display"), el("select", { class: "select" }, [
    el("option", { value: "background", text: t("auto.field.display.background") }),
    el("option", { value: "desktop", text: t("auto.field.display.desktop") }),
  ]), t("auto.field.display.help"));
  const captchaField = field("captcha", t("auto.field.captcha"), el("input", {
    class: "input", value: "AI judge only", type: "text", readOnly: true,
  }), t("auto.field.captcha.help"));
  const timeoutField = number("timeout_seconds", 1, t("auto.field.timeout.help"), t("auto.field.timeout"));
  const failureField = number("failure_limit", 1, t("auto.field.failure.help"), t("auto.field.failure"));
  for (const [key, min, max] of [["target", 1, 1000], ["attempt_limit", 1, 10000],
    ["cooldown_seconds", 0, 3600], ["timeout_seconds", 60, 3600], ["failure_limit", 1, 100]]) {
    controls[key].min = min;
    controls[key].max = max;
  }
  const saveButton = button({ label: t("auto.btn.save"), onClick: saveConfig });
  const startButton = button({ label: t("auto.btn.start"), variant: "primary", icon: "bolt", onClick: () => mutate("start") });
  const pauseButton = button({ label: t("auto.btn.pause"), onClick: () => mutate("pause") });
  const resumeButton = button({ label: t("auto.btn.resume"), onClick: () => mutate("resume") });
  const stopButton = button({ label: t("auto.btn.stop"), variant: "danger", onClick: () => mutate("stop") });
  const retryButton = button({ label: t("auto.btn.refresh"), onClick: refresh });
  const form = el("form", { class: "auto-form", on: {
    submit: (event) => { event.preventDefault(); saveConfig(); },
    input: edited, change: edited,
  } }, [
    el("div", { class: "auto-fields" }, [modeField, targetField, attemptField, concurrencyField,
      cooldownField, ipField, displayField, captchaField, timeoutField, failureField]),
    el("div", { class: "auto-actions" }, [saveButton, startButton, note]),
  ]);
  const node = card({ title: t("auto.title"), desc: t("auto.desc"), body: [
    form, error, connection,
    el("section", { class: "auto-session", "aria-label": t("auto.title") }, [
      el("div", { class: "auto-session-head" }, [state, progress]), profile, counts, reason, slots,
      el("div", { class: "auto-actions" }, [pauseButton, resumeButton, stopButton, retryButton]),
      el("div", { class: "auto-actions" }, [
        el("strong", { text: t("auto.live_log") }),
        el("label", { class: "checkline" }, [follow, el("span", { text: t("auto.follow") })]), copyLog,
      ]), logPane,
      el("p", { class: "auto-note", text: t("auto.note.events") }),
      el("p", { class: "auto-note", text: t("auto.note.pause") }),
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
    note.textContent = busy ? ({ save: t("auto.note.saving"), start: t("auto.note.starting"),
      pause: t("auto.note.pausing"), resume: t("auto.note.resuming"), stop: t("auto.note.stopping") }[busy])
      : !hydrated ? t("auto.note.loading")
      : live() ? t("auto.note.locked")
      : dirty ? t("auto.note.dirty")
      : saved ? t("auto.note.saved")
      : t("auto.note.review");
  }
  function paint(data) {
    if (data?.error) throw new Error(String(data.error));
    if (!data?.config || !data?.status?.state) throw new Error(t("auto.error.invalid"));
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
    progress.textContent = status.state === "idle" ? t("auto.progress.no_session")
      : mode === "goal" ? t("auto.progress.goal", { n: count(status.qualified), target: target })
      : t("auto.progress.attempts", { n: count(status.launched), target: count(status.attempt_limit ?? runConfig.attempt_limit) });
    profile.textContent = status.state === "idle" ? t("auto.profile.saved")
      : (status.id ? "Session " + status.id + " | " : "") + (mode === "goal" ? "Goal mode" : "Attempts mode")
        + " | " + count(runConfig.concurrency) + " slots | " + count(runConfig.cooldown_seconds)
        + "s cooldown | " + runConfig.ip_source + " | "
        + (runConfig.browser_display === "desktop" ? t("auto.field.display.desktop") : t("auto.field.display.background")) + " | AI only";
    fill(counts, [["launched", t("auto.counts.launched")], ["completed", t("auto.counts.completed")], ["qualified", t("auto.counts.qualified")],
      ["failed", t("auto.counts.failed")], ["no_plan", t("auto.counts.no_plan")], ["duplicates", t("auto.counts.duplicates")], ["active", t("auto.counts.active")], ["cooling", t("auto.counts.cooling")]]
      .map(([key, label]) => el("div", { class: "auto-count" }, [el("dt", { text: label }), el("dd", { text: String(count(status[key])) })])));
    const details = [];
    if (status.state === "interrupted") details.push(t("auto.interrupted"));
    if (status.reason) details.push(t("auto.reason", { reason: status.reason }));
    if (count(status.remaining_cooldown_seconds)) details.push(t("auto.cooldown.next", { n: Math.ceil(count(status.remaining_cooldown_seconds)) }));
    reason.textContent = details.join(" ");
    reason.hidden = !details.length;
    fill(slots, (status.slots || []).map((slot, index) => el("li", { class: "auto-slot" }, [
      el("strong", { text: t("auto.slot", { n: slot.id ?? index + 1 }) }),
      el("span", { text: slot.state || "idle" }),
      el("span", { class: "auto-note", text: slot.worker_name || slot.name || "" }),
      slot.remaining_cooldown_seconds ? el("span", { class: "auto-note", text: t("auto.slot.remaining", { n: Math.ceil(count(slot.remaining_cooldown_seconds)) }) }) : null,
    ])));
    const lines = (status.events || []).map(event => {
      const stamp = new Date(event.ts * 1000).toLocaleTimeString();
      return "[" + stamp + "] " + (event.worker ? event.worker + " | " : "") + event.message;
    });
    const text = lines.join("\n");
    if (text !== lastLog || !logPane.children.length) {
      const scroll = logPane.scrollTop;
      fill(logPane, lines.length ? lines.map((line, index) => el("div", {
        class: "logpane__line" + ((status.events[index].level === "error") ? " logpane__line--err"
          : status.events[index].level === "warn" ? " logpane__line--warn" : ""), text: line,
      })) : el("div", { class: "faint", text: t("auto.events.empty") }));
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
        connection.textContent = t("auto.connection.unavailable", { msg: exception.message });
        connection.hidden = false;
        renderControls();
        if (!hydrated) {
          note.textContent = t("auto.config.not_loaded");
          progress.textContent = t("auto.session.unavailable");
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
