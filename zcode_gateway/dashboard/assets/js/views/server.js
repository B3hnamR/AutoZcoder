// Server: endpoints, keys, process info, and a ready-to-paste client snippet.

import { el, fill, icon, copyText } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast } from "../core/toast.js";
import { ICONS } from "../core/icons.js";
import { maskSecret } from "../core/format.js";
import { t } from "../core/i18n.js";

/** A value that copies itself on click. */
function copyable(value, { mono = true } = {}) {
  const slot = el("span", { class: "copyable__icon" }, icon(ICONS.copy, 13));
  const node = el("button", {
    class: "copyable" + (mono ? " mono" : ""),
    type: "button",
    title: t("server.copy.click"),
  }, [el("span", { class: "copyable__text", text: value }), slot]);

  let revert = null;
  node.addEventListener("click", async () => {
    if (!(await copyText(value))) {
      toast(t("server.copy.failed"), "err");
      return;
    }
    fill(slot, icon(ICONS.check, 13));
    node.classList.add("is-copied");
    toast(t("server.copy.copied"), "ok", 1800);
    clearTimeout(revert);
    revert = setTimeout(() => {
      node.classList.remove("is-copied");
      fill(slot, icon(ICONS.copy, 13));
    }, 1400);
  });
  return node;
}

/** A secret that is blurred until revealed, and copies without revealing. */
function secret(value) {
  if (!value) return el("span", { class: "faint", text: t("server.secret.not_set") });

  // Masked state shows real dots, not blurred text: a blur over an element
  // that failed to render looks identical to a bug.
  const text = el("span", { class: "secret__value mono", text: maskSecret(value) });
  let masked = true;

  const toggle = el("button", {
    class: "btn btn--quiet btn--sm btn--icon",
    type: "button",
    title: t("server.secret.reveal"),
    "aria-label": t("server.secret.reveal"),
  }, icon(ICONS.eye, 14));
  const copy = el("button", {
    class: "btn btn--quiet btn--sm btn--icon",
    type: "button",
    title: t("server.secret.copy"),
    "aria-label": t("server.secret.copy"),
  }, icon(ICONS.copy, 14));

  toggle.addEventListener("click", () => {
    masked = !masked;
    text.textContent = masked ? maskSecret(value) : value;
    fill(toggle, icon(masked ? ICONS.eye : ICONS.eyeOff, 14));
    toggle.title = masked ? t("server.secret.reveal") : t("server.secret.hide");
  });
  copy.addEventListener("click", async () => {
    const ok = await copyText(value);
    toast(ok ? t("server.copy.copied") : t("server.copy.failed"), ok ? "ok" : "err", 1800);
  });

  return el("span", { class: "secret row row--tight" }, [text, toggle, copy]);
}

function kvRow(key, valueNode) {
  return [el("div", { class: "kv__key", text: key }), el("div", { class: "kv__val" }, valueNode)];
}

function card(title, desc, body) {
  return el("div", { class: "card" }, [
    el("div", { class: "card__head" }, [
      el("div", {}, [
        el("div", { class: "card__title", text: title }),
        desc ? el("div", { class: "card__desc", text: desc }) : null,
      ]),
    ]),
    el("div", { class: "card__body" }, body),
  ]);
}

/**
 * A URL a client can actually dial.
 *
 * cfg.server.host is a *bind* address: 0.0.0.0 and :: mean "every interface",
 * which no client can connect to. The address this page was loaded from is
 * always reachable, so prefer it and show the bind address separately.
 */
function clientBase(s) {
  const bindsEverywhere = ["0.0.0.0", "::", "[::]", ""].includes(String(s.host || "").trim());
  const host = bindsEverywhere ? (location.hostname || "127.0.0.1") : s.host;
  const scheme = location.protocol === "https:" ? "https" : "http";
  return scheme + "://" + (host.includes(":") ? "[" + host + "]" : host) + ":" + s.port;
}

function render(root, s) {
  const base = clientBase(s);
  const endpoints = s.endpoints || {};
  const bindsEverywhere = ["0.0.0.0", "::", "[::]"].includes(String(s.host || "").trim());

  const endpointCard = card(t("server.endpoints.title"), t("server.endpoints.desc"),
    el("div", { class: "stack stack--tight" }, [
      el("div", { class: "kv" }, [
        ...kvRow(t("server.endpoints.openai"), copyable(base + (endpoints.openai || "/v1/chat/completions"))),
        ...kvRow(t("server.endpoints.anthropic"), copyable(base + (endpoints.anthropic || "/v1/messages"))),
        ...kvRow(t("server.endpoints.models"), copyable(base + (endpoints.models || "/v1/models"))),
      ]),
      bindsEverywhere
        ? el("p", { class: "field__help", text: t("server.endpoints.listening", { host: s.host + ":" + s.port }) })
        : null,
    ]));

  const clientKeys = s.client_keys || [];
  const keysCard = card(t("server.keys.title"), t("server.keys.desc"),
    el("div", { class: "kv" }, [
      ...kvRow(t("server.keys.admin"), secret(s.admin_key || "")),
      ...clientKeys.flatMap((k) => kvRow(t("server.keys.client", { name: k.name || "default" }), secret(k.key || ""))),
      ...(clientKeys.length ? [] : kvRow(t("server.keys.client", { name: "default" }), el("span", { class: "faint", text: t("server.keys.none") }))),
    ]));

  const processCard = card(t("server.process.title"), null,
    el("div", { class: "kv" }, [
      ...kvRow(t("server.process.version"), el("span", { class: "mono", text: s.version || "—" })),
      ...kvRow(t("server.process.config"), copyable(s.config_path || "—")),
      ...kvRow(t("server.process.accounts_dir"), copyable(s.accounts_dir || "—")),
      ...kvRow(t("server.process.accounts_source"), el("span", { class: "badge", text: s.accounts_source || "—" })),
      ...kvRow(t("server.process.strategy"), el("span", { class: "badge badge--accent", text: s.strategy || "—" })),
      ...kvRow(t("server.process.cwd"), copyable(s.cwd || "—")),
    ]));

  const keyPicker = clientKeys.length > 1 ? el("select", {
    class: "select", "aria-label": t("server.client.key_picker"),
  }, clientKeys.map((key, i) => el("option", { value: String(i), text: key.name || t("server.client.key_default", { n: i + 1 }) }))) : null;
  const clientKey = () => clientKeys[Number(keyPicker?.value || 0)]?.key || "";
  const snippet = el("div", { class: "code" });
  const paintSnippet = () => {
    snippet.textContent = "base_url: " + base + "/v1\napi_key:  "
      + (clientKey() ? maskSecret(clientKey(), 4) : "<client key>");
  };
  keyPicker?.addEventListener("change", paintSnippet);
  paintSnippet();
  const snippetBody = el("div", { class: "stack stack--tight" }, [
    snippet,
    ...(keyPicker ? [keyPicker] : []),
    el("div", { class: "row" }, [
      el("button", {
        class: "btn btn--sm btn--ghost",
        type: "button",
        on: { click: async () => {
          const real = "base_url: " + base + "/v1\napi_key: " + (clientKey() || "<client key>");
          toast(await copyText(real) ? t("server.copy.copied") : t("server.copy.failed"),
                "ok", 2200);
        } },
      }, [icon(ICONS.copy, 14), el("span", { text: t("server.client.copy") })]),
      el("span", { class: "field__help", text: t("server.client.help") }),
    ]),
  ]);
  const clientCard = card(t("server.client.title"), null, snippetBody);

  // Explicit columns, not auto-fit: four cards of uneven height left a hole in
  // the last row. Connection details stack on the left, process facts sit on
  // the right.
  fill(root, el("div", { class: "cols" }, [
    el("div", { class: "stack" }, [endpointCard, clientCard]),
    el("div", { class: "stack" }, [keysCard, processCard]),
  ]));
}

function skeleton(root) {
  const line = (w) => el("div", {
    class: "skeleton",
    text: "loading",
    style: { height: "13px", width: w, margin: "11px 0" },
  });
  const block = () => el("div", { class: "card" },
    el("div", { class: "card__body" }, [line("40%"), line("85%"), line("70%"), line("55%")]));
  fill(root, el("div", { class: "cols" }, [
    el("div", { class: "stack" }, [block(), block()]),
    el("div", { class: "stack" }, [block(), block()]),
  ]));
}

export async function mount({ root, signal }) {
  skeleton(root);

  const load = async () => {
    const data = await api.get("/admin/dash/server", { signal });
    render(root, data);
  };

  await load();
  return { refresh: load };
}
