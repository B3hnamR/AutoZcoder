// Environment setup: python + packages + Chromium, and locating ZCODEAUTO.
// Shared by the Generator and Proxy sections.

import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { toast } from "../../core/toast.js";
import { card, button } from "../../core/ui.js";
import { t } from "../../core/i18n.js";

/** The card shown when the generator folder has not been located yet. */
export function locateCard(message, onConnected) {
  const status = el("p", { class: "field__help" });
  const input = el("input", {
    class: "input input--mono",
    type: "text",
    placeholder: t("setup.locate.ph"),
    "aria-label": t("setup.locate.aria"),
  });

  async function locate(mode) {
    const body = mode === "path" ? { path: input.value.trim() } : {};
    if (mode === "path" && !body.path) { status.textContent = t("setup.locate.enter_first"); return; }
    status.textContent = t("setup.locate.working");
    try {
      const r = await api.post("/admin/dash/generator/locate", body);
      status.textContent = t("setup.locate.connected", { path: r.path });
      toast(t("setup.locate.toast_ok"), "ok");
      onConnected?.();
    } catch (e) {
      status.textContent = e.message;
      status.className = "field__error";
    }
  }

  input.addEventListener("keydown", (e) => { if (e.key === "Enter") locate("path"); });

  return card({
    title: t("setup.locate.title"),
    desc: t("setup.locate.desc"),
    body: el("div", { class: "stack" }, [
      message ? el("p", { class: "field__error", text: message }) : null,
      el("div", { class: "row" }, [
        button({ label: t("setup.locate.auto"), icon: "refresh", variant: "primary", onClick: () => locate("auto") }),
        el("span", { class: "field__help", text: t("setup.locate.auto_hint") }),
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field__label", text: t("setup.locate.path_label") }),
        el("div", { class: "row" }, [
          input,
          button({ label: t("setup.locate.use_path"), onClick: () => locate("path") }),
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
    text: t("setup.checking_progress") });

  const installBtn = button({ label: t("setup.btn.install"), variant: "primary" });
  const recheckBtn = button({ label: t("setup.btn.recheck"), icon: "refresh" });
  const closeBtn = button({ label: t("setup.btn.close"), variant: "ghost" });
  const labelButton = (node, text) => {
    node.querySelector("span:last-child").textContent = text;
    node.setAttribute("aria-label", text);
  };

  const modal = el("div", {
    class: "modal", role: "dialog", "aria-modal": "true", style: { maxWidth: "560px" },
  }, [
    el("div", { class: "modal__head" }, el("div", { class: "modal__title", text: t("setup.title") })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: t("setup.desc") }),
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
    labelButton(recheckBtn, t("setup.checking"));
    progress.textContent = t("setup.checking_slow");
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
        labelButton(installBtn, check.all_ok ? t("setup.all_ok") : t("setup.btn.install"));
        progress.textContent = (check.all_ok ? t("setup.ready") : t("setup.missing"))
          + " " + t("setup.checked_at", { time: new Date().toLocaleTimeString() });
      } else {
        throw new Error(t("setup.no_checks"));
      }
      if (st.state === "done") {
        errorBox.hidden = true;
        clearInterval(timer);
        timer = null;
        if (auto && check.all_ok) { toast(t("setup.ready"), "ok"); close(); }
      } else if (st.state === "failed") {
        errorBox.textContent = st.error || t("setup.install_failed");
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
      labelButton(installBtn, t("setup.installing"));
    }
    if (st.state === "installing") progress.textContent = t("setup.installing_hint");
    } catch (e) {
      if (closed || e.name === "AbortError") return;
      errorBox.textContent = t("setup.check_failed", { msg: e.message });
      errorBox.hidden = false;
      progress.textContent = t("setup.check_retry");
    } finally {
      checking = false;
      if (!closed) {
        recheckBtn.disabled = false;
        labelButton(recheckBtn, t("setup.btn.recheck"));
      }
    }
  }

  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    labelButton(installBtn, t("setup.installing"));
    errorBox.hidden = true;
    try {
      await api.post("/admin/dash/setup/install");
      if (!timer) timer = setInterval(() => refresh(), 1500);
      refresh();
    } catch (e) {
      errorBox.textContent = e.message;
      errorBox.hidden = false;
      installBtn.disabled = false;
      labelButton(installBtn, t("setup.btn.install"));
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
    toast(t("setup.verify_failed"), "warn");
    openEnvSetup();
    return false;
  }
  openEnvSetup({ auto: true });
  return false;
}
