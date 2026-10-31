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

export function importJwt(onImported) {
  const previousFocus = document.activeElement;
  const token = el("textarea", {
    class: "textarea input--mono", rows: 5, id: "jwt-import-token", required: true,
    autocomplete: "off", spellcheck: false, autocapitalize: "none",
    placeholder: "Paste the copied AutoZCoder account export", style: { overflowWrap: "anywhere", WebkitTextSecurity: "disc" },
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
        const result = await api.post("/admin/accounts/import-account", {
          token: token.value.trim(), email: email.value.trim(),
        });
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
      el("p", { text: "Paste an account copied from another AutoZCoder. Its current token is validated with Zcode and saved locally. The same account ID updates an existing entry. A raw CLI JWT is also accepted." }),
      el("div", { class: "field" }, [el("label", { class: "field__label", for: token.id, text: "Account export or JWT" }), token]),
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
