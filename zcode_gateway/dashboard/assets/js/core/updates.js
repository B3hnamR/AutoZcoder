// Update notifications. Download + apply only exist on hosts that can swap
// their own binary; elsewhere the card explains how to update instead.

import { el } from "./dom.js";
import { api } from "./api.js";
import { button } from "./ui.js";
import { can, why, platform } from "./caps.js";

const SKIP_KEY = "gw-skip-version";

let status = null;
let card = null;
let controls = null;
let phase = "idle";
let operation = null;
let message = "";
let revision = 0;
let pollRequest = null;
let checkRequest = null;
let initialized = false;

function skipped() {
  try { return localStorage.getItem(SKIP_KEY) || ""; } catch (_) { return ""; }
}

export function available() {
  return phase !== "idle" || !!(status?.update_available && status.latest !== skipped());
}

export function open() {
  if (card) return;
  controls = {
    title: el("b"),
    version: el("span", { class: "mono text-xs", style: { color: "var(--accent-fg)", marginInlineStart: "var(--s-3)" } }),
    notes: el("div", { class: "update-card__notes mono" }),
    help: el("p", { class: "field__help" }),
    download: button({ label: "Download update", variant: "primary", onClick: download }),
    apply: button({ label: "Restart to apply", variant: "primary", onClick: apply }),
    check: button({ label: "Check now", variant: "quiet", onClick: () => { void checkNow(); } }),
    release: el("a", { class: "btn btn--sm btn--ghost", target: "_blank", rel: "noopener noreferrer", text: "Release notes" }),
    skip: button({ label: "Skip this version", variant: "quiet", onClick: skip }),
    progress: el("div", { class: "faint text-xs", role: "status", "aria-label": "Update status", "aria-live": "polite", "aria-atomic": "true" }),
  };
  card = el("div", { class: "update-card", role: "dialog", "aria-label": "Update available" }, [
    el("div", { class: "row row--tight" }, [
      controls.title,
      controls.version,
      el("span", { class: "spacer" }),
      button({ icon: "eyeOff", title: "Close", variant: "quiet", onClick: close }),
    ]),
    controls.notes,
    controls.help,
    el("div", { class: "row" }, [
      controls.download, controls.apply, controls.check, controls.release, controls.skip,
    ]),
    controls.progress,
  ]);
  document.body.append(card);
  paint();
}

export function close() {
  card?.remove();
  card = null;
  controls = null;
}

function skip() {
  try { if (status?.latest) localStorage.setItem(SKIP_KEY, status.latest); } catch (_) { /* private mode */ }
  close();
  paint();
}

async function download() {
  if (operation || phase !== "idle" || !can("self_update")) return;
  operation = "download";
  phase = "downloading";
  revision++;
  message = "Downloading update. Your current work will continue.";
  paint();
  try {
    await api.post("/admin/dash/update/download");
    phase = "ready";
    message = "Download ready. Restart to apply when your work has finished.";
  } catch (e) {
    phase = "idle";
    message = "Download failed: " + e.message;
  } finally {
    operation = null;
    paint();
  }
}

async function apply() {
  if (operation || phase !== "ready" || !can("self_update")) return;
  operation = "apply";
  phase = "applying";
  revision++;
  message = "Requesting restart. New work will be held while the update applies.";
  paint();
  try {
    await api.post("/admin/dash/update/apply");
    message = "Restarting to apply the update. The app will relaunch.";
  } catch (e) {
    phase = "ready";
    message = "Restart not started: " + e.message + ". The download is ready; retry when your work has finished.";
  } finally {
    operation = null;
    paint();
  }
}

let indicator = null;

function releaseUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password && !url.port
      ? url.href : null;
  } catch (_) { return null; }
}

function paint() {
  const active = !!operation || phase === "downloading" || phase === "applying";
  if (indicator) {
    // Keep manual checks reachable even before the first cached result exists.
    indicator.hidden = false;
    indicator.setAttribute("aria-label", available() ? "Open update available" : "Check for updates");
    indicator.setAttribute("aria-expanded", String(!!card));
  }
  if (!controls) return;
  card.setAttribute("aria-busy", String(active));
  const title = available() ? "Update available" : "Updates";
  card.setAttribute("aria-label", title);
  controls.title.textContent = title;
  const hasNewRelease = !!status?.update_available || !!status?.release_available;
  const current = status?.current || "?";
  controls.version.textContent = status?.latest
    ? hasNewRelease ? current + " -> " + status.latest
      : "Installed " + current + " | Latest published " + status.latest : "";
  controls.notes.textContent = status?.notes || "";
  controls.notes.hidden = !status?.notes || !hasNewRelease;
  controls.help.textContent = can("self_update")
    ? "Downloading does not stop work. Active work blocks restart. New starts will be held while the update applies."
    : platform() === "linux"
      ? "Update outside the app: run `autozcoder update` on the gateway host."
      : why("self_update") || "Update outside the app on this host.";
  controls.download.hidden = !can("self_update") || !available() || phase === "ready" || phase === "applying";
  controls.download.disabled = active;
  controls.apply.hidden = !can("self_update") || (phase !== "ready" && phase !== "applying");
  controls.apply.disabled = active;
  controls.check.disabled = active;
  controls.skip.hidden = !status?.update_available || phase !== "idle";
  controls.skip.disabled = active;
  const url = releaseUrl(status?.release_url);
  controls.release.hidden = !url;
  if (url) controls.release.href = url;
  else controls.release.removeAttribute("href");
  controls.progress.textContent = message || (phase === "downloading" ? "Downloading update. Your current work will continue."
    : phase === "ready" ? "Download ready. Restart to apply when your work has finished."
    : phase === "applying" ? "Restarting to apply the update. New work is held."
    : status?.disabled ? "Automatic update checks are disabled."
    : status?.checked_at ? (available() ? "A new release is available."
      : status.current && status.latest && status.current !== status.latest
        ? "Your installed version is newer than the latest published release. No downgrade will be offered."
        : "You are up to date. No download or restart is needed.")
    : "Waiting for the first update check. You can also check now.");
}

/** Wire the topbar button and start polling. */
export function init(button_) {
  if (indicator !== button_) {
    indicator?.removeEventListener("click", toggle);
    button_.addEventListener("click", toggle);
  }
  indicator = button_;
  paint();
  if (initialized) return;
  initialized = true;
  void poll();
  setInterval(() => { void poll(); }, 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
}

function toggle() { if (card) { close(); paint(); } else open(); }

function receive(next) {
  if (!next) return;
  // A failed check must not erase a previously discovered release.
  status = next.error && status ? { ...status, error: next.error } : next;
  if (next.updater_phase !== undefined || next.downloaded !== undefined) {
    phase = ["downloading", "ready", "applying"].includes(next.updater_phase)
      ? next.updater_phase : next.downloaded ? "ready" : "idle";
  }
  message = next.error ? "Update check failed: " + next.error + ". Check again to retry." : "";
  paint();
}

export function poll() {
  if (pollRequest) return pollRequest;
  if (operation || (document.hidden && phase !== "downloading" && phase !== "applying")) return Promise.resolve();
  const started = revision;
  pollRequest = (async () => {
    try {
      const next = await api.get("/admin/dash/update/status");
      if (started === revision) receive(next);
    } catch (e) {
      if (started === revision) {
        message = "Update status unavailable: " + e.message + ". Check again to retry.";
        paint();
      }
    } finally { pollRequest = null; }
  })();
  return pollRequest;
}

/** Also usable by Settings when no newer release has been discovered yet. */
export function checkNow() {
  open();
  if (checkRequest) return checkRequest;
  if (operation || phase === "downloading" || phase === "applying") return Promise.resolve();
  operation = "check";
  revision++;
  message = "Checking for updates...";
  paint();
  checkRequest = (async () => {
    try {
      const next = await api.post("/admin/dash/update/check");
      receive(next);
      return next;
    }
    catch (e) { message = "Update check failed: " + e.message + ". Check again to retry."; }
    finally {
      operation = null;
      checkRequest = null;
      paint();
    }
  })();
  return checkRequest;
}
