// Tokens only enter an explicit import request or a user-triggered clipboard
// action. Never store them in browser storage, list responses, or toast text.
import { el, copyText } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { button } from "../core/ui.js";
import { t } from "../core/i18n.js";

export async function copyAccountForSharing(accountId) {
  const approved = await confirmDialog({
    title: t("acc.jwt.share.title"),
    body: t("acc.jwt.share.body"),
    okLabel: t("acc.jwt.share.ok"), danger: true,
  });
  if (!approved) return;
  try {
    const result = await api.get("/admin/accounts/" + encodeURIComponent(accountId) + "/export");
    const ok = await copyText(JSON.stringify(result));
    toast(ok ? t("acc.jwt.copy_ok") : t("acc.jwt.copy_fail"),
          ok ? "ok" : "err");
  } catch (e) { toast(t("acc.jwt.export_fail", { msg: e.message }), "err"); }
}

export async function copyAllAccountsForSharing() {
  const approved = await confirmDialog({
    title: t("acc.jwt.copy_all.title"),
    body: t("acc.jwt.copy_all.body"),
    okLabel: t("acc.jwt.copy_all.ok"), danger: true,
  });
  if (!approved) return;
  try {
    const bundle = await api.get("/admin/accounts/export");
    const count = bundle.accounts.length;
    if (!count) { toast(t("acc.jwt.no_accounts"), "warn"); return; }
    const ok = await copyText(JSON.stringify(bundle));
    toast(ok ? t("acc.jwt.copied_count", { n: count })
      : t("acc.jwt.copy_fail"), ok ? "ok" : "err");
  } catch (e) { toast(t("acc.jwt.export_all_fail", { msg: e.message }), "err"); }
}

export function importJwt(onImported) {
  const previousFocus = document.activeElement;
  const token = el("textarea", {
    class: "textarea input--mono", rows: 5, id: "jwt-import-token", required: true,
    autocomplete: "off", spellcheck: false, autocapitalize: "none",
     placeholder: t("acc.jwt.ph"), style: { overflowWrap: "anywhere", WebkitTextSecurity: "disc" },
  });
  const email = el("input", {
    class: "input", type: "email", id: "jwt-import-email", autocomplete: "off",
    placeholder: t("acc.jwt.email.ph"), maxLength: 254,
  });
  const error = el("p", { class: "field__error", role: "alert", hidden: true });
  let busy = false;
  const submit = button({ label: t("acc.jwt.btn"), variant: "primary" });
  submit.type = "submit";
  const cancel = button({ label: "Cancel", onClick: close });
  const form = el("form", {
    class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "jwt-import-title",
    style: { maxWidth: "540px" },
    on: { submit: async event => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      submit.disabled = true;
      cancel.disabled = true;
      submit.querySelector("span").textContent = t("acc.jwt.validating");
      error.hidden = true;
      try {
        const value = token.value.trim();
        let bundle;
        if (value.startsWith("{")) {
          try { bundle = JSON.parse(value); } catch (_) { /* The API reports invalid JSON. */ }
        }
        if (bundle?.format === "autozcoder-accounts") {
          if (email.value.trim()) throw new Error("Clear the email label when importing multiple accounts.");
          const result = await api.post("/admin/accounts/import-accounts", bundle);
          const failed = result.failures.length;
          if (!result.imported) throw new Error(t("acc.jwt.import_fail", { n: failed }));
          busy = false;
          close();
          toast(failed ? t("acc.jwt.imported_some", { n: result.imported, m: failed }) : t("acc.imported.count", { n: result.imported }), failed ? "warn" : "ok");
          await onImported?.();
          return;
        }
        const result = await api.post("/admin/accounts/import-account", { token: value, email: email.value.trim() });
        busy = false;
        close();
        toast(result.updated ? t("acc.jwt.updated") : t("acc.jwt.saved"), "ok");
        await onImported?.();
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        busy = false;
        submit.disabled = false;
        cancel.disabled = false;
        submit.querySelector("span").textContent = t("acc.jwt.btn");
      }
    } },
  }, [
    el("div", { class: "modal__head" }, el("h2", { id: "jwt-import-title", text: t("acc.jwt.title") })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: t("acc.jwt.desc") }),
      el("div", { class: "field" }, [el("label", { class: "field__label", for: token.id, text: t("acc.jwt.label") }), token]),
      el("div", { class: "field" }, [el("label", { class: "field__label", for: email.id, text: t("acc.jwt.email_label") }), email,
        el("p", { class: "field__help", text: t("acc.jwt.email_help") })]),
      el("p", { class: "field__help", text: t("acc.jwt.warn") }),
      error,
    ]),
    el("div", { class: "modal__foot" }, [cancel, submit]),
  ]);
  const overlay = el("div", { class: "overlay" }, form);

  function close() {
    if (busy) return;
    token.value = "";
    email.value = "";
    overlay.remove();
    document.removeEventListener("keydown", keyboard, true);
    previousFocus?.focus();
  }
  function keyboard(event) {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Tab") {
      const nodes = [...form.querySelectorAll("input,textarea,button")].filter(n => !n.disabled);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  overlay.addEventListener("mousedown", e => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", keyboard, true);
  document.body.append(overlay);
  token.focus();
}
