// Environment setup: python + packages + Chromium, and locating ZCODEAUTO.
// Shared by the Generator and Proxy sections.

import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { toast } from "../../core/toast.js";
import { card, button } from "../../core/ui.js";

/** The card shown when the generator folder has not been located yet. */
export function locateCard(message, onConnected) {
  const status = el("p", { class: "field__help" });
  const input = el("input", {
    class: "input input--mono",
    type: "text",
    placeholder: "Path to the ZCODEAUTO folder",
    "aria-label": "ZCODEAUTO folder path",
  });

  async function locate(mode) {
    const body = mode === "path" ? { path: input.value.trim() } : {};
    if (mode === "path" && !body.path) { status.textContent = "Enter a folder path first."; return; }
    status.textContent = "Working…";
    try {
      const r = await api.post("/admin/dash/generator/locate", body);
      status.textContent = "Connected: " + r.path;
      toast("ZCODEAUTO connected", "ok");
      onConnected?.();
    } catch (e) {
      status.textContent = e.message;
      status.className = "field__error";
    }
  }

  input.addEventListener("keydown", (e) => { if (e.key === "Enter") locate("path"); });

  return card({
    title: "Connect ZCODEAUTO",
    desc: "The generator and proxy tools run the ZCODEAUTO scripts. Point the gateway at that folder once — no config editing, no restart.",
    body: el("div", { class: "stack" }, [
      message ? el("p", { class: "field__error", text: message }) : null,
      el("div", { class: "row" }, [
        button({ label: "Find it automatically", icon: "refresh", variant: "primary", onClick: () => locate("auto") }),
        el("span", { class: "field__help", text: "Searches beside the app, your home folder, Documents and common clone paths." }),
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field__label", text: "Or enter the folder path (the one containing gen/)" }),
        el("div", { class: "row" }, [
          input,
          button({ label: "Use this folder", onClick: () => locate("path") }),
        ]),
      ]),
      status,
    ]),
  });
}

/** Modal that installs python, packages and Chromium. */
export function openEnvSetup({ auto = false } = {}) {
  let timer = null;
  let checking = false;
  let closed = false;
  const controller = new AbortController();

  const steps = el("div", { class: "stack--tight", style: { display: "flex", flexDirection: "column", gap: "6px" } });
  const logPane = el("div", { class: "logpane mono", style: { height: "190px", display: "none" } });
  const errorBox = el("p", { class: "field__error", hidden: true });
  const progress = el("p", { class: "field__help", role: "status", "aria-live": "polite",
    text: "Checking Python, packages and Chromium. This may take a few seconds..." });

  const installBtn = button({ label: "Install missing", variant: "primary" });
  const recheckBtn = button({ label: "Check again", icon: "refresh" });
  const closeBtn = button({ label: "Close", variant: "ghost" });
  const labelButton = (node, text) => {
    node.querySelector("span:last-child").textContent = text;
    node.setAttribute("aria-label", text);
  };

  const modal = el("div", {
    class: "modal", role: "dialog", "aria-modal": "true", style: { maxWidth: "560px" },
  }, [
    el("div", { class: "modal__head" }, el("div", { class: "modal__title", text: "Environment setup" })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: "The account generator needs a real Python, its packages and a Chromium browser. "
                    + "Checks are read-only. Missing packages/browser downloads can be installed here; Linux OS dependencies may require the server installer." }),
      progress, steps, logPane, errorBox,
    ]),
    el("div", { class: "modal__foot" }, [closeBtn, recheckBtn, installBtn]),
  ]);
  const overlay = el("div", { class: "overlay" }, modal);

  function close() {
    closed = true;
    controller.abort();
    clearInterval(timer);
    overlay.remove();
  }
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });

  async function refresh(forceCheck = false) {
    if (checking || closed) return;
    checking = true;
    recheckBtn.disabled = true;
    labelButton(recheckBtn, "Checking...");
    progress.textContent = "Checking environment... On a busy server this can take 10-30 seconds.";
    errorBox.hidden = true;
    try {
    let st;
    try {
      st = await api.get("/admin/dash/setup/status", { signal: controller.signal });
    } catch (e) {
      throw e;
    }

    if (forceCheck || !["installing"].includes(st.state)) {
      const check = await api.get("/admin/dash/setup/check", { signal: controller.signal });
      if (check?.steps) {
        fill(steps, check.steps.map((s) => el("div", { class: "setup-step" }, [
          el("span", { class: s.ok ? "text-ok" : "text-warn", text: s.ok ? "✓" : "○" }),
          el("b", { class: "setup-step__name", text: s.label }),
          el("span", { class: "faint text-xs truncate", text: s.detail || "" }),
        ])));
        installBtn.disabled = !!check.all_ok;
        labelButton(installBtn, check.all_ok ? "Everything installed" : "Install missing");
        progress.textContent = (check.all_ok ? "Environment ready. All checks passed." : "Missing requirements found. Review the checks below.")
          + " Checked at " + new Date().toLocaleTimeString();
      } else {
        throw new Error("The server returned no environment checks. Try again or inspect Logs.");
      }
      if (st.state === "done") {
        errorBox.hidden = true;
        clearInterval(timer);
        timer = null;
        if (auto && check.all_ok) { toast("Environment ready", "ok"); close(); }
      } else if (st.state === "failed") {
        errorBox.textContent = st.error || "Installation failed — press Install to retry.";
        errorBox.hidden = false;
      }
    }

    if (st.lines?.length) {
      logPane.style.display = "";
      const atBottom = logPane.scrollHeight - logPane.scrollTop - logPane.clientHeight < 48;
      fill(logPane, st.lines.map((l) => el("div", { class: "logpane__line", text: l })));
      if (atBottom) logPane.scrollTop = logPane.scrollHeight;
    }

    if (st.state === "installing" && !timer) {
      timer = setInterval(() => refresh(), 1500);
      installBtn.disabled = true;
      labelButton(installBtn, "Installing...");
    }
    if (st.state === "installing") progress.textContent = "Installation is running in the background. Live output appears below.";
    } catch (e) {
      if (closed || e.name === "AbortError") return;
      errorBox.textContent = "Environment check failed: " + e.message;
      errorBox.hidden = false;
      progress.textContent = "Could not complete the check. Use Check again to retry.";
    } finally {
      checking = false;
      if (!closed) {
        recheckBtn.disabled = false;
        labelButton(recheckBtn, "Check again");
      }
    }
  }

  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    labelButton(installBtn, "Installing...");
    errorBox.hidden = true;
    try {
      await api.post("/admin/dash/setup/install");
      if (!timer) timer = setInterval(() => refresh(), 1500);
      refresh();
    } catch (e) {
      errorBox.textContent = e.message;
      errorBox.hidden = false;
      installBtn.disabled = false;
      labelButton(installBtn, "Install missing");
    }
  });
  recheckBtn.addEventListener("click", () => refresh(true));

  document.body.append(overlay);
  refresh();
  return close;
}

/** True when the environment is ready; otherwise opens the modal. */
export async function ensureEnvironment() {
  try {
    const check = await api.get("/admin/dash/setup/check");
    if (check.all_ok) return true;
  } catch (_) {
    toast("Could not verify the environment. Open Environment setup and try Check again.", "warn");
    openEnvSetup();
    return false;
  }
  openEnvSetup({ auto: true });
  return false;
}
