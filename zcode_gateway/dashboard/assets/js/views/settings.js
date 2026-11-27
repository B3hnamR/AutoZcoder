// Settings: the whitelisted config surface, with dirty tracking so only what
// you touched is sent on save.

import { el, fill, $$ } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { card, button } from "../core/ui.js";
import { can, why, platform } from "../core/caps.js";
import { checkNow as checkForUpdates } from "../core/updates.js";
import { t } from "../core/i18n.js";

// [path, kind, labelKey, helpKey, opts]  kind: bool | enum | model | int | float | text
// Labels and help are i18n keys resolved at render time (see settingRow), so a
// live language switch repaints them without rebuilding the structure.
const SECTIONS = [
  { id: "server", titleKey: "set.server.title", descKey: "set.server.desc", rows: [
    ["server.host", "enum", "set.server.host.label", "set.server.host.help"],
    ["server.port", "int", "set.server.port.label", "set.server.port.help", { min: 1, max: 65535 }],
  ]},
  { id: "accounts", titleKey: "set.accounts.title", descKey: "set.accounts.desc", rows: [
    ["accounts.source", "enum", "set.accounts.source.label", "set.accounts.source.help"],
    ["accounts.dir", "text", "set.accounts.dir.label", "set.accounts.dir.help"],
    ["accounts.pool_file", "text", "set.accounts.pool_file.label", "set.accounts.pool_file.help", { adv: true }],
  ]},
  { id: "pool", titleKey: "set.pool.title", descKey: "set.pool.desc", rows: [
    ["pool.strategy", "enum", "set.pool.strategy.label", "set.pool.strategy.help"],
    ["pool.max_in_flight_per_account", "int", "set.pool.inflight.label", "set.pool.inflight.help", { min: 1 }],
    ["pool.acquire_wait_seconds", "float", "set.pool.acquire_wait.label", "set.pool.acquire_wait.help", { unit: "s", min: 0 }],
    ["pool.cooldown_seconds", "int", "set.pool.cooldown.label", "set.pool.cooldown.help", { unit: "s", min: 0 }],
    ["pool.blocked_model_cooldown_seconds", "int", "set.pool.blocked.label", "set.pool.blocked.help", { unit: "s", min: 0 }],
    ["pool.refresh_lead_seconds", "int", "set.pool.refresh.label", "set.pool.refresh.help", { unit: "s", min: 0 }],
    ["pool.balance_poll_seconds", "int", "set.pool.balance_poll.label", "set.pool.balance_poll.help", { unit: "s", min: 10 }],
    ["pool.min_balance_micro", "int", "set.pool.min_balance.label", "set.pool.min_balance.help", { min: 0, adv: true }],
  ]},
  { id: "upstream", titleKey: "set.upstream.title", descKey: "set.upstream.desc", rows: [
    ["upstream.timeout_connect", "float", "set.upstream.connect.label", "set.upstream.connect.help", { unit: "s", min: 0 }],
    ["upstream.timeout_read", "float", "set.upstream.read.label", "set.upstream.read.help", { unit: "s", min: 0 }],
    ["upstream.max_attempts", "int", "set.upstream.max_attempts.label", "set.upstream.max_attempts.help", { min: 1 }],
    ["upstream.overload_max_attempts", "int", "set.upstream.overload_attempts.label", "set.upstream.overload_attempts.help", { min: 1 }],
    ["upstream.overload_base_delay_seconds", "float", "set.upstream.overload_base.label", "set.upstream.overload_base.help", { unit: "s", min: 0 }],
    ["upstream.overload_max_delay_seconds", "float", "set.upstream.overload_max.label", "set.upstream.overload_max.help", { unit: "s", min: 0 }],
    ["upstream.anthropic_cache_control", "bool", "set.upstream.cache.label", "set.upstream.cache.help"],
  ]},
  { id: "models", titleKey: "set.models.title", descKey: "set.models.desc", rows: [
    ["models.default", "model", "set.models.default.label", "set.models.default.help"],
    ["models.default_anthropic", "model", "set.models.default_anthropic.label", "set.models.default_anthropic.help"],
    ["models.default_effort", "enum", "set.models.effort.label", "set.models.effort.help"],
    ["models.auto_free_fallback.enabled", "bool", "set.models.fallback.label", "set.models.fallback.help"],
    ["models.pacing.enabled", "bool", "set.models.pacing.label", "set.models.pacing.help"],
    ["models.pacing.min_gap_ms", "int", "set.models.pacing_min.label", "set.models.pacing_min.help", { unit: "ms", min: 0 }],
    ["models.pacing.max_gap_ms", "int", "set.models.pacing_max.label", "set.models.pacing_max.help", { unit: "ms", min: 0 }],
  ]},
  { id: "logging", titleKey: "set.logging.title", descKey: "set.logging.desc", rows: [
    ["logging.level", "enum", "set.logging.level.label", "set.logging.level.help"],
    ["logging.capture", "bool", "set.logging.capture.label", "set.logging.capture.help"],
  ]},
  { id: "updates", titleKey: "set.updates.title", descKey: "set.updates.desc", rows: [
    ["update.enabled", "bool", "set.updates.enabled.label", "set.updates.enabled.help"],
    ["update.interval_hours", "float", "set.updates.interval.label", "set.updates.interval.help", { unit: "h", min: 0 }],
  ]},
];

/** Settings whose help text depends on the host. */
function sectionsForHost() {
  return SECTIONS.map((section) => {
    if (section.id === "server" && platform() === "linux") {
      // On a remote box "this computer only" means nobody can reach it.
      return {
        ...section,
        // hostNote is appended to the translated desc at render time.
        hostNote: t("set.server.host.help_linux"),
        rows: section.rows.map((r) => r[0] !== "server.host" ? r : [
          r[0], r[1], r[2], "set.server.host.help_linux",
          r[4],
        ]),
      };
    }
    if (section.id !== "updates") return section;
    // Applying an update in place is Windows-exe only; say so rather than
    // letting someone enable checks and wonder why nothing installs.
    if (can("self_update")) return section;
    const note = why("self_update") || t("set.updates.note");
    return {
      ...section,
      hostNote: note,
      rows: section.rows.map((r) =>
        r[0] === "update.enabled"
          ? [r[0], r[1], r[2], r[3], { ...r[4], note }]
          : r),
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

  function settingRow([path, kind, labelKey, helpKey, opts]) {
    const label = t(labelKey);
    let help = helpKey ? t(helpKey) : "";
    if (opts && opts.note) help = help ? help + " " + opts.note : opts.note;
    const o = Object.assign({ label }, opts || {});
    return el("div", {
      class: "setting",
      data: { text: (label + " " + (help || "") + " " + path).toLowerCase() },
    }, [
      el("div", { class: "setting__meta" }, [
        el("label", { class: "setting__label" }, [
          el("span", { text: label }),
          o.adv ? el("span", { class: "badge", text: t("settings.adv") }) : null,
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
      label.textContent = n ? t("settings.unsaved.count", { n }) : t("settings.unsaved.none");
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
    if (!Object.keys(values).length) { toast(t("settings.save.none"), "warn"); return; }
    try {
      const r = await api.put("/admin/dash/settings", { values });
      toast(r.changed.length
        ? t("settings.save.done", { n: r.changed.length }) + (r.restart_required ? t("settings.save.restart") : t("settings.save.immediate"))
        : t("settings.save.none"), "ok");
      await load();
    } catch (e) { toast(t("settings.save.err") + ": " + e.message, "err"); }
  }

  async function regenerateKeys() {
    const ok = await confirmDialog({
      title: t("settings.keys.confirm.title"),
      body: t("settings.keys.confirm.body"),
      okLabel: t("settings.keys.confirm.ok"),
      danger: true,
    });
    if (!ok) return;
    toast(t("settings.keys.toast"), "warn", 6000);
  }

  function render() {
    const search = el("input", {
      class: "input", type: "search", placeholder: t("settings.filter.placeholder"), value: filter,
      style: { maxWidth: "260px" }, "aria-label": t("settings.filter.aria"),
      on: { input: (e) => { filter = e.target.value; applyFilter(); } },
    });

    const sections = sectionsForHost();
    const toc = el("nav", { class: "set-toc", "aria-label": t("settings.toc.aria") },
      [...sections, { id: "keys", titleKey: "settings.keys.title" }].map((g) => el("button", {
        class: "set-toc__item", type: "button", text: t(g.titleKey),
        on: { click: () => root.querySelector("#set-" + g.id)?.scrollIntoView({ behavior: "smooth", block: "start" }) },
      })));

    const cards = sections.map((g) => {
      const desc = g.hostNote ? t(g.descKey) + " " + g.hostNote : t(g.descKey);
      const node = card({ title: t(g.titleKey), desc, body: g.rows.map(settingRow) });
      node.classList.add("set-card");
      node.id = "set-" + g.id;
      return node;
    });

    const keysCard = card({
      title: t("settings.keys.title"),
      desc: t("settings.keys.desc"),
      body: el("div", { class: "setting" }, [
        el("div", { class: "setting__meta" }, [
          el("label", { class: "setting__label", text: t("settings.keys.label") }),
          el("p", { class: "field__help", text: t("settings.keys.help") }),
        ]),
        el("div", { class: "setting__control" },
          button({ label: t("settings.keys.action"), variant: "danger", onClick: regenerateKeys })),
      ]),
    });
    keysCard.classList.add("set-card");
    keysCard.id = "set-keys";

    const bar = el("div", { class: "set-bar" }, [
      el("span", { class: "text-sm muted", "data-dirty": "", text: t("settings.unsaved.none") }),
      el("span", { class: "spacer" }),
      button({ label: t("settings.save.discard"), variant: "ghost", onClick: () => load(), disabled: true }),
      button({ label: t("settings.save.action"), variant: "primary", onClick: save, disabled: true }),
    ]);
    bar.querySelectorAll(".btn").forEach((b) => b.setAttribute("data-needs-dirty", ""));

    fill(root, el("div", { class: "stack" }, [
      el("div", { class: "row" }, [
        search,
        button({ label: t("settings.check.now"), icon: "refresh", onClick: async event => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            const result = await checkForUpdates();
            if (result?.error) toast(t("settings.check.failed") + ": " + result.error, "warn");
            else if (!result?.update_available && !result?.release_available) toast(result?.disabled
              ? t("settings.check.disabled")
              : t("settings.check.none"), "ok");
          } catch (error) { toast(t("settings.check.failed") + ": " + error.message, "err"); }
          finally { control.disabled = false; }
        } }),
        el("span", { class: "field__help", text:
          t("settings.saved.to").replace("{path}", data.config_path || "config.yaml") }),
      ]),
      el("div", { class: "set-wrap" }, [
        toc,
        el("div", { class: "stack" }, [
          ...cards,
          keysCard,
          el("div", { class: "empty__text", "data-nomatch": "", hidden: true, text: t("settings.nomatch") }),
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
