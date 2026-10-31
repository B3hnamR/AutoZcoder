// Composite building blocks shared by views: cards, tables, toolbars, copy
// affordances. Views should not hand-roll these.

import { el, fill, icon, copyText } from "./dom.js";
import { toast } from "./toast.js";
import { ICONS } from "./icons.js";
import { maskSecret } from "./format.js";

/** card({title, desc, actions, body, flush}) */
export function card({ title, desc, actions, body, flush = false }) {
  const head = (title || desc || actions)
    ? el("div", { class: "card__head" }, [
        el("div", { style: { minWidth: "0" } }, [
          title ? el("div", { class: "card__title", text: title }) : null,
          desc ? el("div", { class: "card__desc", text: desc }) : null,
        ]),
        el("span", { class: "spacer" }),
        ...(actions ? [].concat(actions) : []),
      ])
    : null;
  return el("div", { class: "card" }, [
    head,
    el("div", { class: "card__body" + (flush ? " card__body--flush" : "") }, body),
  ]);
}

/** metric({label, value, hint, tone}) */
export function metric({ label, value, hint, tone }) {
  return el("div", { class: "metric" }, [
    el("div", { class: "metric__label", text: label }),
    el("div", { class: "metric__value" + (tone ? " text-" + tone : ""), text: String(value) }),
    hint ? el("div", { class: "metric__hint", text: hint }) : null,
  ]);
}

/** badge(text, tone) — tone: ok | warn | err | info | accent */
export function badge(text, tone) {
  return el("span", { class: "badge" + (tone ? " badge--" + tone : ""), text: String(text) });
}

/** Account/worker state -> badge tone. One mapping for the whole dashboard. */
export function stateTone(state) {
  const s = String(state || "").toLowerCase();
  if (/^(ready|ok|live|active|online|done|success|running)$/.test(s)) return "ok";
  if (/^(cooling|refreshing|pending|queued|waiting|starting|busy)$/.test(s)) return "warn";
  if (/^(dead|error|failed|revoked|exhausted|blocked|offline)$/.test(s)) return "err";
  return null;
}

export function stateBadge(state) {
  return badge(state || "unknown", stateTone(state));
}

/** Click-to-copy value. */
export function copyable(value, { mono = true, label } = {}) {
  const text = String(value ?? "");
  const slot = el("span", { class: "copyable__icon" }, icon(ICONS.copy, 13));
  const node = el("button", {
    class: "copyable" + (mono ? " mono" : ""),
    type: "button",
    title: "Click to copy",
    "aria-label": "Copy " + (label || text),
  }, [el("span", { class: "copyable__text", text }), slot]);

  let revert = null;
  node.addEventListener("click", async () => {
    if (!(await copyText(text))) { toast("Could not copy — select the text manually", "err"); return; }
    fill(slot, icon(ICONS.check, 13));
    node.classList.add("is-copied");
    toast("Copied to clipboard", "ok", 1600);
    clearTimeout(revert);
    revert = setTimeout(() => { node.classList.remove("is-copied"); fill(slot, icon(ICONS.copy, 13)); }, 1400);
  });
  return node;
}

/** Masked value with reveal + copy. Never blurs: an empty render must not be
    mistakable for a hidden one. */
export function secret(value) {
  const real = String(value ?? "");
  if (!real) return el("span", { class: "faint", text: "Not set" });

  const text = el("span", { class: "secret__value mono", text: maskSecret(real) });
  let masked = true;

  const toggle = el("button", {
    class: "btn btn--quiet btn--sm btn--icon", type: "button",
    title: "Reveal", "aria-label": "Reveal value",
  }, icon(ICONS.eye, 14));
  const copy = el("button", {
    class: "btn btn--quiet btn--sm btn--icon", type: "button",
    title: "Copy", "aria-label": "Copy value",
  }, icon(ICONS.copy, 14));

  toggle.addEventListener("click", () => {
    masked = !masked;
    text.textContent = masked ? maskSecret(real) : real;
    fill(toggle, icon(masked ? ICONS.eye : ICONS.eyeOff, 14));
    toggle.title = masked ? "Reveal" : "Hide";
  });
  copy.addEventListener("click", async () => {
    const ok = await copyText(real);
    toast(ok ? "Copied to clipboard" : "Could not copy", ok ? "ok" : "err", 1600);
  });

  return el("span", { class: "secret row row--tight" }, [text, toggle, copy]);
}

/** button({label, icon, variant, size, title, onClick, disabled}) */
export function button({ label, icon: name, variant = "ghost", size = "sm", title, onClick, disabled }) {
  const cls = ["btn", "btn--" + variant];
  if (size) cls.push("btn--" + size);
  if (!label) cls.push("btn--icon");
  return el("button", {
    class: cls.join(" "),
    type: "button",
    title: title || label,
    disabled: !!disabled,
    "aria-label": label || title,
    on: onClick ? { click: onClick } : null,
  }, [name ? icon(ICONS[name] || ICONS.dot, 14) : null, label ? el("span", { text: label }) : null]);
}

/** toolbar([...nodes]) with a trailing search box. */
export function toolbar(items, search) {
  return el("div", { class: "toolbar" }, [
    ...[].concat(items || []),
    el("span", { class: "spacer" }),
    ...(search ? [search] : []),
  ]);
}

/** searchBox({placeholder, value, onInput}) */
export function searchBox({ placeholder = "Filter…", value = "", onInput }) {
  return el("input", {
    class: "input toolbar__search",
    type: "search",
    placeholder,
    value,
    spellcheck: false,
    "aria-label": placeholder,
    on: onInput ? { input: (e) => onInput(e.target.value) } : null,
  });
}

/**
 * table({columns, rows, render, empty, key})
 * columns: [{label, align, width}]
 * render:  row -> array of cell contents (string | Node | null)
 */
export function table({ columns, rows, render, empty }) {
  if (!rows || !rows.length) {
    return el("div", { class: "empty" }, [
      el("div", { class: "empty__title", text: empty?.title || "Nothing here yet" }),
      empty?.text ? el("div", { class: "empty__text", text: empty.text }) : null,
      empty?.actions ? el("div", { class: "empty__actions" }, empty.actions) : null,
    ]);
  }
  const head = el("tr", {}, columns.map((c) =>
    el("th", { text: c.label, class: c.align === "right" ? "num" : "", style: c.width ? { width: c.width } : null })));

  const body = el("tbody", {}, rows.map((row) => {
    const cells = render(row);
    return el("tr", { data: row.__searchText ? { search: row.__searchText } : null },
      cells.map((cell, i) => el("td", {
        class: columns[i]?.align === "right" ? "num" : "",
      }, cell)));
  }));

  return el("div", { class: "table-wrap" },
    el("table", { class: "table" }, [el("thead", {}, head), body]));
}

/** Scrolling log pane that keeps the viewport pinned to the bottom. */
export function logPane({ lines = [], height = "420px" } = {}) {
  const pane = el("div", { class: "logpane mono", style: { height } });
  const write = (next) => {
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 48;
    fill(pane, next.map((l) => el("div", { class: "logpane__line", text: l })));
    if (atBottom) pane.scrollTop = pane.scrollHeight;
  };
  write(lines);
  return { node: pane, write };
}
