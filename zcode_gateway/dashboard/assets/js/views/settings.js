// Settings: the whitelisted config surface, with dirty tracking so only what
// you touched is sent on save.

import { el, fill, $$ } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { card, button } from "../core/ui.js";
import { can, why, platform } from "../core/caps.js";
import { checkNow as checkForUpdates } from "../core/updates.js";

// [path, kind, label, help, opts]  kind: bool | enum | model | int | float | text
const SECTIONS = [
  { id: "server", title: "Server", desc: "Where the gateway listens, and who can reach it.", rows: [
    ["server.host", "enum", "Who can connect",
     "“This computer only” is safest. Choose “Everyone on my network” only if other devices should use the gateway; they still need a client key."],
    ["server.port", "int", "Port",
     "The port the gateway and this dashboard listen on.", { min: 1, max: 65535 }],
  ]},
  { id: "accounts", title: "Accounts", desc: "Where the gateway finds the Zcode accounts it uses.", rows: [
    ["accounts.source", "enum", "Where accounts come from",
     "Normally the account folder, which the Accounts section and the generator fill in."],
    ["accounts.dir", "text", "Accounts folder",
     "Folder holding one snapshot .txt file per account."],
    ["accounts.pool_file", "text", "Pool file", "Only used when the source is “Pool file”.", { adv: true }],
  ]},
  { id: "pool", title: "Account rotation", desc: "How requests spread across accounts, and how failures are handled.", rows: [
    ["pool.strategy", "enum", "How to pick an account", "Decides which account serves the next request."],
    ["pool.max_in_flight_per_account", "int", "Simultaneous requests per account",
     "How many requests one account may run at once. Higher is faster but looks busier to the provider.", { min: 1 }],
    ["pool.acquire_wait_seconds", "float", "Wait for a free account",
     "When every account is busy, how long a request waits for a slot. 0 fails immediately.", { unit: "s", min: 0 }],
    ["pool.cooldown_seconds", "int", "Rest after an error",
     "How long an account rests after a connection or server error.", { unit: "s", min: 0 }],
    ["pool.blocked_model_cooldown_seconds", "int", "Rest after “unusual activity”",
     "When the provider blocks one model on an account (405/3012), only that model pauses, for this long.", { unit: "s", min: 0 }],
    ["pool.refresh_lead_seconds", "int", "Renew logins early",
     "Refresh an account's token this long before it expires.", { unit: "s", min: 0 }],
    ["pool.balance_poll_seconds", "int", "Check quotas every",
     "How often each account's remaining quota is read. Minimum 10 seconds.", { unit: "s", min: 10 }],
    ["pool.min_balance_micro", "int", "Legacy balance threshold",
     "Old credit-based setting. Zcode now uses per-model token quotas, so leave it at 0.", { min: 0, adv: true }],
  ]},
  { id: "upstream", title: "Connection to Zcode", desc: "Timeouts and retries for requests sent to the provider.", rows: [
    ["upstream.timeout_connect", "float", "Connection timeout", "How long to wait to reach the provider.", { unit: "s", min: 0 }],
    ["upstream.timeout_read", "float", "Response timeout",
     "How long to wait for data while an answer streams. Keep it high for long answers.", { unit: "s", min: 0 }],
    ["upstream.max_attempts", "int", "Accounts to try per request",
     "If an account fails, the request moves to another one, up to this many tries.", { min: 1 }],
    ["upstream.overload_max_attempts", "int", "Tries when a model is overloaded",
     "Counts the first try. The official app uses 11.", { min: 1 }],
    ["upstream.overload_base_delay_seconds", "float", "First retry delay",
     "Retries back off exponentially (with jitter) from this delay.", { unit: "s", min: 0 }],
    ["upstream.overload_max_delay_seconds", "float", "Longest retry delay", "The official app uses 60.", { unit: "s", min: 0 }],
    ["upstream.anthropic_cache_control", "bool", "Prompt caching",
     "Send Anthropic cache hints, as the official app does. Turn off only to test."],
  ]},
  { id: "models", title: "Models", desc: "Defaults used when a client does not choose.", rows: [
    ["models.default", "model", "Default model (OpenAI-style)", "Used when /v1/chat/completions names no model."],
    ["models.default_anthropic", "model", "Default model (Anthropic-style)", "Used when /v1/messages names no model."],
    ["models.default_effort", "enum", "Reasoning effort",
     "Used when the client does not ask for one. High is nearly as good as Max for coding and much faster."],
    ["models.auto_free_fallback.enabled", "bool", "Fall back to another model",
     "When a model is blocked or out of quota, serve the request with the next model in the fallback chain."],
    ["models.pacing.enabled", "bool", "Space out requests",
     "Adds a small random pause between requests so the provider's CDN does not flag your IP."],
    ["models.pacing.min_gap_ms", "int", "Shortest pause", "With the longest pause this sets the random window.", { unit: "ms", min: 0 }],
    ["models.pacing.max_gap_ms", "int", "Longest pause", "", { unit: "ms", min: 0 }],
  ]},
  { id: "logging", title: "Logging", desc: "What the gateway writes down.", rows: [
    ["logging.level", "enum", "Detail level", "Shown in the Logs section. Info is right for everyday use."],
    ["logging.capture", "bool", "Record every exchange",
     "Writes each upstream request and response to a JSONL file. Handy for debugging, but it stores your prompts on disk."],
  ]},
  { id: "updates", title: "Updates", desc: "Release notifications.", rows: [
    ["update.enabled", "bool", "Check for updates", "Look for new releases on GitHub and show a badge."],
    ["update.interval_hours", "float", "Check every", "Hours between checks.", { unit: "h", min: 0 }],
  ]},
];

/** Settings whose help text depends on the host. */
function sectionsForHost() {
  return SECTIONS.map((section) => {
    if (section.id === "server" && platform() === "linux") {
      // On a remote box "this computer only" means nobody can reach it.
      return {
        ...section,
        rows: section.rows.map((r) => r[0] !== "server.host" ? r : [
          r[0], r[1], r[2],
          "On a server, “this computer only” makes the gateway unreachable from "
          + "outside the box — you would need an SSH tunnel to get back in.",
          r[4],
        ]),
      };
    }
    if (section.id !== "updates") return section;
    // Applying an update in place is Windows-exe only; say so rather than
    // letting someone enable checks and wonder why nothing installs.
    if (can("self_update")) return section;
    const note = why("self_update") || "Updates are applied outside the app on this host.";
    return {
      ...section,
      desc: section.desc + " " + note,
      rows: section.rows.map((r) =>
        r[0] === "update.enabled" ? [r[0], r[1], r[2], r[3] + " " + note, r[4]] : r),
    };
  });
}

let filter = "";

export async function mount({ root, signal }) {
  let data = null;

  const lookup = (path) =>
    data.values?.[path] ?? path.split(".").reduce((o, p) => o?.[p], data.effective);

  function optionsFor(path, kind, cur) {
    let opts = kind === "model"
      ? (data.models || []).map((m) => ({ value: m, label: m, desc: "" }))
      : (data.choices?.[path] || []).map((c) => ({ value: c.value, label: c.label, desc: c.desc }));
    if (cur != null && cur !== ""
        && !opts.some((o) => String(o.value).toLowerCase() === String(cur).toLowerCase())) {
      opts = [{ value: cur, label: cur + " (current)", desc: "Set in config.yaml" }, ...opts];
    }
    return opts;
  }

  function control(path, kind, o) {
    const cur = lookup(path);

    if (kind === "bool") {
      const input = el("input", {
        class: "switch__input", type: "checkbox", checked: !!cur,
        data: { set: path, init: String(!!cur) },
      });
      const state = el("span", { class: "text-sm muted", text: cur ? "On" : "Off" });
      input.addEventListener("change", () => { state.textContent = input.checked ? "On" : "Off"; });
      return el("label", { class: "switch" }, [input, el("span", { class: "switch__track" }), state]);
    }

    if (kind === "enum" || kind === "model") {
      const opts = optionsFor(path, kind, cur);
      const picked = opts.find((x) => String(x.value).toLowerCase() === String(cur ?? "").toLowerCase());
      return el("select", {
        class: "select", "aria-label": o.label,
        data: { set: path, init: String(picked ? picked.value : (cur ?? "")) },
      }, opts.map((x) => el("option", {
        value: x.value, text: x.label, title: x.desc || "", selected: x === picked,
      })));
    }

    const numeric = kind === "int" || kind === "float";
    const input = el("input", {
      class: "input" + (numeric ? "" : " input--mono"),
      type: numeric ? "number" : "text",
      value: cur ?? "",
      spellcheck: false,
      "aria-label": o.label,
      min: o.min, max: o.max,
      step: kind === "float" ? "any" : (numeric ? "1" : null),
      data: { set: path, init: String(cur ?? "") },
    });
    return o.unit
      ? el("span", { class: "row row--tight" }, [input, el("span", { class: "faint text-sm", text: o.unit })])
      : input;
  }

  function settingRow([path, kind, label, help, opts]) {
    const o = Object.assign({ label }, opts || {});
    return el("div", {
      class: "setting",
      data: { text: (label + " " + (help || "") + " " + path).toLowerCase() },
    }, [
      el("div", { class: "setting__meta" }, [
        el("label", { class: "setting__label" }, [
          el("span", { text: label }),
          o.adv ? el("span", { class: "badge", text: "advanced" }) : null,
        ]),
        help ? el("p", { class: "field__help", text: help }) : null,
      ]),
      el("div", { class: "setting__control" }, control(path, kind, o)),
    ]);
  }

  function applyFilter() {
    const q = filter.trim().toLowerCase();
    let shown = 0;
    for (const section of $$(".set-card", root)) {
      let any = 0;
      for (const row of $$(".setting", section)) {
        const hit = !q || (row.dataset.text || "").includes(q);
        row.hidden = !hit;
        if (hit) any++;
      }
      section.hidden = !any;
      shown += any;
    }
    const none = root.querySelector("[data-nomatch]");
    if (none) none.hidden = shown > 0;
  }

  function dirtyState() {
    let n = 0;
    for (const input of $$("[data-set]", root)) {
      const cur = input.type === "checkbox" ? String(input.checked) : input.value;
      const changed = cur !== input.dataset.init;
      input.closest(".setting")?.classList.toggle("is-changed", changed);
      if (changed) n++;
    }
    const label = root.querySelector("[data-dirty]");
    if (label) {
      label.textContent = n ? n + " unsaved change" + (n === 1 ? "" : "s") : "No changes";
      label.classList.toggle("text-warn", n > 0);
    }
    for (const b of $$("[data-needs-dirty]", root)) b.disabled = n === 0;
    return n;
  }

  async function save() {
    const values = {};
    for (const input of $$("[data-set]", root)) {
      const cur = input.type === "checkbox" ? String(input.checked) : input.value;
      if (cur !== input.dataset.init) values[input.dataset.set] = cur;
    }
    if (!Object.keys(values).length) { toast("No changes to save", "warn"); return; }
    try {
      const r = await api.put("/admin/dash/settings", { values });
      toast(r.changed.length
        ? "Saved " + r.changed.length + " setting(s) — restart to apply"
        : "No changes", "ok");
      await load();
    } catch (e) { toast("save: " + e.message, "err"); }
  }

  async function regenerateKeys() {
    const ok = await confirmDialog({
      title: "Regenerate keys?",
      body: "The admin key and the first client key both change.\n"
          + "Anything using the old keys stops working until you update it. A restart is required.",
      okLabel: "Regenerate",
      danger: true,
    });
    if (!ok) return;
    toast("Key rotation stays in the desktop launcher for now — it also restarts the gateway.", "warn", 6000);
  }

  function render() {
    const search = el("input", {
      class: "input", type: "search", placeholder: "Filter settings…", value: filter,
      style: { maxWidth: "260px" }, "aria-label": "Filter settings",
      on: { input: (e) => { filter = e.target.value; applyFilter(); } },
    });

    const sections = sectionsForHost();
    const toc = el("nav", { class: "set-toc", "aria-label": "Settings sections" },
      [...sections, { id: "keys", title: "Keys" }].map((g) => el("button", {
        class: "set-toc__item", type: "button", text: g.title,
        on: { click: () => root.querySelector("#set-" + g.id)?.scrollIntoView({ behavior: "smooth", block: "start" }) },
      })));

    const cards = sections.map((g) => {
      const node = card({ title: g.title, desc: g.desc, body: g.rows.map(settingRow) });
      node.classList.add("set-card");
      node.id = "set-" + g.id;
      return node;
    });

    const keysCard = card({
      title: "Keys",
      desc: "The admin key and client keys protect this dashboard and the /v1 endpoints.",
      body: el("div", { class: "setting" }, [
        el("div", { class: "setting__meta" }, [
          el("label", { class: "setting__label", text: "Regenerate keys" }),
          el("p", { class: "field__help", text:
            "Replaces the admin key and the first client key. Programs using the old keys stop working until you update them." }),
        ]),
        el("div", { class: "setting__control" },
          button({ label: "Regenerate keys…", variant: "danger", onClick: regenerateKeys })),
      ]),
    });
    keysCard.classList.add("set-card");
    keysCard.id = "set-keys";

    const bar = el("div", { class: "set-bar" }, [
      el("span", { class: "text-sm muted", "data-dirty": "", text: "No changes" }),
      el("span", { class: "spacer" }),
      button({ label: "Discard", variant: "ghost", onClick: () => load(), disabled: true }),
      button({ label: "Save changes", variant: "primary", onClick: save, disabled: true }),
    ]);
    bar.querySelectorAll(".btn").forEach((b) => b.setAttribute("data-needs-dirty", ""));

    fill(root, el("div", { class: "stack" }, [
      el("div", { class: "row" }, [
        search,
        button({ label: "Check for updates now", icon: "refresh", onClick: async event => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            const result = await checkForUpdates();
            if (result?.error) toast("Update check failed: " + result.error, "warn");
            else if (!result?.update_available && !result?.release_available) toast(result?.disabled
              ? "Automatic update checks are disabled. Enable them and restart to check releases."
              : "No newer complete release found", "ok");
          } catch (error) { toast("Update check failed: " + error.message, "err"); }
          finally { control.disabled = false; }
        } }),
        el("span", { class: "field__help", text:
          "Saved to " + (data.config_path || "config.yaml") + ". Changes apply after the gateway restarts." }),
      ]),
      el("div", { class: "set-wrap" }, [
        toc,
        el("div", { class: "stack" }, [
          ...cards,
          keysCard,
          el("div", { class: "empty__text", "data-nomatch": "", hidden: true, text: "No settings match that filter." }),
        ]),
      ]),
      bar,
    ]));

    root.querySelector(".set-wrap").addEventListener("input", dirtyState);
    root.querySelector(".set-wrap").addEventListener("change", dirtyState);
    applyFilter();
    dirtyState();
  }

  const load = async () => {
    data = await api.get("/admin/dash/settings", { signal });
    render();
  };

  await load();
  // Deliberately no refresh(): a poll would fight the user's unsaved edits.
  return {};
}
