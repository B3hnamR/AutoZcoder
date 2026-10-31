// Toasts + the confirm dialog. Both own their DOM and clean it up.

import { el, fill } from "./dom.js";

let host = null;

function toaster() {
  if (!host) {
    host = el("div", { class: "toaster", role: "status", "aria-live": "polite" });
    document.body.append(host);
  }
  return host;
}

/** toast("saved", "ok") — kind: "" | ok | warn | err */
export function toast(message, kind = "", ms = 4200) {
  const node = el("div", { class: "toast" + (kind ? " toast--" + kind : "") }, [
    el("span", { class: "dot toast__dot" + (kind ? " dot--" + kind : "") }),
    el("div", { class: "toast__text", text: String(message) }),
  ]);
  toaster().append(node);

  const close = () => {
    if (!node.isConnected) return;
    node.classList.add("is-leaving");
    node.addEventListener("animationend", () => node.remove(), { once: true });
    setTimeout(() => node.remove(), 400);           // animation may be disabled
  };
  const timer = setTimeout(close, ms);
  node.addEventListener("click", () => { clearTimeout(timer); close(); });
  return close;
}

/**
 * Replacement for window.confirm. Focus-trapped, Escape cancels, and Enter
 * only confirms while focus is inside the dialog so a stray keypress can never
 * trigger a destructive action.
 */
export function confirmDialog({ title, body, okLabel = "OK", cancelLabel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;

    const okBtn = el("button", {
      class: "btn btn--sm " + (danger ? "btn--danger" : "btn--primary"),
      type: "button",
      text: okLabel,
    });
    const cancelBtn = el("button", { class: "btn btn--sm btn--ghost", type: "button", text: cancelLabel });

    const modal = el("div", {
      class: "modal",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "confirm-title",
    }, [
      el("div", { class: "modal__head" }, el("div", { class: "modal__title", id: "confirm-title", text: title })),
      el("div", { class: "modal__body" }, el("p", { text: body, style: { whiteSpace: "pre-line" } })),
      el("div", { class: "modal__foot" }, [cancelBtn, okBtn]),
    ]);
    const overlay = el("div", { class: "overlay" }, modal);

    const done = (value) => {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(value);
    };

    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); done(false); return; }
      if (e.key === "Enter" && modal.contains(document.activeElement)) { e.preventDefault(); done(true); return; }
      if (e.key === "Tab") {
        const focusables = [cancelBtn, okBtn];
        const i = focusables.indexOf(document.activeElement);
        e.preventDefault();
        focusables[e.shiftKey ? (i <= 0 ? focusables.length - 1 : i - 1) : (i + 1) % focusables.length].focus();
      }
    }

    okBtn.addEventListener("click", () => done(true));
    cancelBtn.addEventListener("click", () => done(false));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(false); });
    document.addEventListener("keydown", onKey, true);

    document.body.append(overlay);
    okBtn.focus();
  });
}

/** Render a standard error block into a container. */
export function errorState(container, message, onRetry) {
  fill(container, el("div", { class: "card" }, el("div", { class: "empty" }, [
    el("div", { class: "empty__title", text: "Could not load" }),
    el("div", { class: "empty__text", text: message }),
    onRetry
      ? el("div", { class: "empty__actions" },
          el("button", { class: "btn btn--sm btn--ghost", text: "Try again", on: { click: onRetry } }))
      : null,
  ])));
}
