// Tokens only enter an explicit import request or a user-triggered clipboard
// action. Never store them in browser storage, list responses, or toast text.
import { el, copyText } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { button } from "../core/ui.js";

export async function copyAccountForSharing(accountId) {
  const approved = await confirmDialog({
    title: "Share this account?",
    body: "The export grants access to this account. Send it only to someone you trust. Both installations share its quota and limits. This does not create a separate account or automatic token renewal.",
    okLabel: "Copy account", danger: true,
  });
  if (!approved) return;
  try {
    const result = await api.get("/admin/accounts/" + encodeURIComponent(accountId) + "/export");
    const ok = await copyText(JSON.stringify(result));
    toast(ok ? "Account export copied. Your friend can paste it into Import account." : "Clipboard unavailable; copy was not completed.",
          ok ? "ok" : "err");
  } catch (e) { toast("Could not export account: " + e.message, "err"); }
}

export async function copyAllAccountsForSharing() {
  const approved = await confirmDialog({
    title: "Copy all working accounts?",
    body: "The clipboard export grants access to every working account in the pool, regardless of the table filter. Share it only with someone you trust. Both installations share the same quotas and limits.",
    okLabel: "Copy all accounts", danger: true,
  });
  if (!approved) return;
  try {
    const bundle = await api.get("/admin/accounts/export");
    const count = bundle.accounts.length;
    if (!count) { toast("No working accounts to copy.", "warn"); return; }
    const ok = await copyText(JSON.stringify(bundle));
    toast(ok ? `${count} ${count === 1 ? "account" : "accounts"} copied. Paste the bundle into Import account.`
      : "Clipboard unavailable; copy was not completed.", ok ? "ok" : "err");
  } catch (e) { toast("Could not export accounts: " + e.message, "err"); }
}

export function importJwt(onImported) {
  const previousFocus = document.activeElement;
  const token = el("textarea", {
    class: "textarea input--mono", rows: 5, id: "jwt-import-token", required: true,
    autocomplete: "off", spellcheck: false, autocapitalize: "none",
     placeholder: "Paste an account export, account bundle, or CLI JWT", style: { overflowWrap: "anywhere", WebkitTextSecurity: "disc" },
  });
  const email = el("input", {
    class: "input", type: "email", id: "jwt-import-email", autocomplete: "off",
    placeholder: "Optional table label", maxLength: 254,
  });
  const error = el("p", { class: "field__error", role: "alert", hidden: true });
  let busy = false;
  const submit = button({ label: "Validate and import", variant: "primary" });
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
      submit.querySelector("span").textContent = "Validating with Zcode...";
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
          if (!result.imported) throw new Error(`No accounts imported. ${failed} failed validation or could not be saved.`);
          busy = false;
          close();
          toast(`${result.imported} imported${failed ? `; ${failed} could not be imported` : ""}.`, failed ? "warn" : "ok");
          await onImported?.();
          return;
        }
        const result = await api.post("/admin/accounts/import-account", { token: value, email: email.value.trim() });
        busy = false;
        close();
        toast(result.updated ? "Account token updated and saved" : "Account validated and saved", "ok");
        await onImported?.();
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        busy = false;
        submit.disabled = false;
        cancel.disabled = false;
        submit.querySelector("span").textContent = "Validate and import";
      }
    } },
  }, [
    el("div", { class: "modal__head" }, el("h2", { id: "jwt-import-title", text: "Import account" })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: "Paste one or more accounts copied from AutoZCoder, or a raw CLI JWT. Each token is validated with Zcode before saving. An existing account with the same ID is updated." }),
      el("div", { class: "field" }, [el("label", { class: "field__label", for: token.id, text: "Account export, bundle, or JWT" }), token]),
      el("div", { class: "field" }, [el("label", { class: "field__label", for: email.id, text: "Email label (optional)" }), email,
        el("p", { class: "field__help", text: "Normally included in an account export. Optional override for a raw JWT; this label is not a verified identity." })]),
      el("p", { class: "field__help", text: "You share the original account's quota and limits. Credentials are saved on this gateway, never in browser storage. Imported tokens can be revoked and do not gain automatic renewal." }),
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
