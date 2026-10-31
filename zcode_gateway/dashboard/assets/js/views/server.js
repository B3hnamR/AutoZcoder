// Server: endpoints, keys, process info, and a ready-to-paste client snippet.

import { el, fill, icon, copyText } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast } from "../core/toast.js";
import { ICONS } from "../core/icons.js";
import { maskSecret } from "../core/format.js";

/** A value that copies itself on click. */
function copyable(value, { mono = true } = {}) {
  const slot = el("span", { class: "copyable__icon" }, icon(ICONS.copy, 13));
  const node = el("button", {
    class: "copyable" + (mono ? " mono" : ""),
    type: "button",
    title: "Click to copy",
  }, [el("span", { class: "copyable__text", text: value }), slot]);

  let revert = null;
  node.addEventListener("click", async () => {
    if (!(await copyText(value))) {
      toast("Could not copy — select the text manually", "err");
      return;
    }
    fill(slot, icon(ICONS.check, 13));
    node.classList.add("is-copied");
    toast("Copied to clipboard", "ok", 1800);
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
  if (!value) return el("span", { class: "faint", text: "Not set" });

  // Masked state shows real dots, not blurred text: a blur over an element
  // that failed to render looks identical to a bug.
  const text = el("span", { class: "secret__value mono", text: maskSecret(value) });
  let masked = true;

  const toggle = el("button", {
    class: "btn btn--quiet btn--sm btn--icon",
    type: "button",
    title: "Reveal",
    "aria-label": "Reveal value",
  }, icon(ICONS.eye, 14));
  const copy = el("button", {
    class: "btn btn--quiet btn--sm btn--icon",
    type: "button",
    title: "Copy",
    "aria-label": "Copy value",
  }, icon(ICONS.copy, 14));

  toggle.addEventListener("click", () => {
    masked = !masked;
    text.textContent = masked ? maskSecret(value) : value;
    fill(toggle, icon(masked ? ICONS.eye : ICONS.eyeOff, 14));
    toggle.title = masked ? "Reveal" : "Hide";
  });
  copy.addEventListener("click", async () => {
    const ok = await copyText(value);
    toast(ok ? "Copied to clipboard" : "Could not copy", ok ? "ok" : "err", 1800);
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

  const endpointCard = card("Endpoints", "Point any OpenAI- or Anthropic-compatible client here.",
    el("div", { class: "stack stack--tight" }, [
      el("div", { class: "kv" }, [
        ...kvRow("OpenAI chat", copyable(base + (endpoints.openai || "/v1/chat/completions"))),
        ...kvRow("Anthropic messages", copyable(base + (endpoints.anthropic || "/v1/messages"))),
        ...kvRow("Models list", copyable(base + (endpoints.models || "/v1/models"))),
      ]),
      bindsEverywhere
        ? el("p", { class: "field__help" }, [
            el("span", { text: "Listening on " }),
            el("span", { class: "mono", text: s.host + ":" + s.port }),
            el("span", { text: " (every interface). Addresses above use this page's host so they are dialable." }),
          ])
        : null,
    ]));

  const clientKeys = s.client_keys || [];
  const keysCard = card("Keys", "Required on /v1/* and /admin/*. Reveal to read, or copy without revealing.",
    el("div", { class: "kv" }, [
      ...kvRow("Admin key", secret(s.admin_key || "")),
      ...clientKeys.flatMap((k) => kvRow("Client key · " + (k.name || "default"), secret(k.key || ""))),
      ...(clientKeys.length ? [] : kvRow("Client keys", el("span", { class: "faint", text: "None configured" }))),
    ]));

  const processCard = card("Process", null,
    el("div", { class: "kv" }, [
      ...kvRow("Version", el("span", { class: "mono", text: s.version || "—" })),
      ...kvRow("Config file", copyable(s.config_path || "—")),
      ...kvRow("Accounts folder", copyable(s.accounts_dir || "—")),
      ...kvRow("Accounts source", el("span", { class: "badge", text: s.accounts_source || "—" })),
      ...kvRow("Rotation strategy", el("span", { class: "badge badge--accent", text: s.strategy || "—" })),
      ...kvRow("Working directory", copyable(s.cwd || "—")),
    ]));

  const snippet = "base_url: " + base + "/v1\napi_key:  " + maskSecret(s.admin_key || "", 4) + "   # admin key, or any client key";
  const snippetBody = el("div", { class: "stack stack--tight" }, [
    el("div", { class: "code", text: snippet }),
    el("div", { class: "row" }, [
      el("button", {
        class: "btn btn--sm btn--ghost",
        type: "button",
        on: { click: async () => {
          const real = "base_url: " + base + "/v1\napi_key: " + (s.admin_key || "");
          toast(await copyText(real) ? "Config copied (with the real key)" : "Could not copy",
                "ok", 2200);
        } },
      }, [icon(ICONS.copy, 14), el("span", { text: "Copy with real key" })]),
      el("span", { class: "field__help", text: "Works with the OpenAI SDK, Anthropic SDK, Kilo, or plain curl." }),
    ]),
  ]);
  const clientCard = card("Point a client at it", null, snippetBody);

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
