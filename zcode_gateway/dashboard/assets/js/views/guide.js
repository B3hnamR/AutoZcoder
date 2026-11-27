// Searchable operating manual, with short task chapters and an error index.
import { el, fill } from "../core/dom.js";
import { t, lang } from "../core/i18n.js";
import { card, button, copyable, table } from "../core/ui.js";
import { navigate } from "../core/router.js";
import { platform } from "../core/caps.js";
import { GUIDE as GUIDE_EN, ERRORS as ERRORS_EN } from "./guide-content.js";
import { GUIDE as GUIDE_FA, ERRORS as ERRORS_FA } from "./guide-content-fa.js";

export function mount({ root }) {
  let selected = "start", query = "";
  const fa = lang() === "fa";
  const GUIDE = fa ? GUIDE_FA : GUIDE_EN;
  const ERRORS = fa ? ERRORS_FA : ERRORS_EN;
  const body = el("div", { class: "guide-articles" });
  const nav = el("nav", { class: "guide-toc", "aria-label": t("guide.toc.aria") });
  const resultCount = el("span", { class: "field__help", role: "status", "aria-live": "polite" });
  const heading = el("h2", { class: "guide-heading" });
  const description = el("p", { class: "muted text-sm" });
  const sections = [...GUIDE, { id: "errors", title: t("guide.troubleshooting.title"), desc: t("guide.troubleshooting.desc"), items: ERRORS }];
  const chapterPicker = el("select", { class: "select guide-chapter-picker", "aria-label": t("guide.chapter.aria"),
    on: { change: event => { selected = event.target.value; query = ""; input.value = ""; paint(); } } },
    sections.map(s => el("option", { value: s.id, text: s.title })));
  const matches = (section, item) => !query ||
    query.split(/\s+/).every(word => [section.title, item.title, item.tags, ...item.steps]
      .join(" ").toLowerCase().includes(word));

  function entry(section, item, searching) {
    const details = el("details", { class: "guide-entry", open: searching }, [
      el("summary", {}, [el("span", { text: item.title }),
        searching ? el("span", { class: "guide-entry__chapter", text: section.title }) : null]),
      el("div", { class: "guide-entry__body" }, [
        item.sizing ? table({ columns: [{ label: t("guide.col.workload") }, { label: t("guide.col.cpu") }, { label: t("guide.col.ram") }, { label: t("guide.col.guidance") }],
          rows: item.sizing, render: row => row }) : null,
        el("ol", { class: "guide-instructions" }, item.steps.map(text => el("li", { text }))),
        ...(item.commands || []).map(command => el("div", { class: "guide-command" }, copyable(command))),
        button({ label: t("guide.open", { route: item.route || section.route || "accounts" }),
          variant: "ghost", onClick: () => navigate(item.route || section.route || "accounts") }),
      ]),
    ]);
    return details;
  }

  function paint() {
    chapterPicker.value = selected;
    const searching = !!query;
    for (const node of nav.children) {
      const active = !searching && node.dataset.chapter === selected;
      node.classList.toggle("is-active", active);
      if (active) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    }
    const section = sections.find(s => s.id === selected);
    heading.textContent = searching ? t("guide.heading.results") : section.title;
    description.textContent = searching
      ? t("guide.desc.results")
      : section.desc;
    const entries = (searching ? sections : [section])
      .flatMap(s => s.items.filter(item => matches(s, item)).map(item => entry(s, item, searching)));
    resultCount.textContent = t(searching ? "guide.count.matching" : "guide.count.topics", { n: entries.length });
    fill(body, entries.length ? entries : el("div", { class: "empty" }, [
      el("h3", { text: t("guide.no_match.title") }),
      el("p", { class: "field__help", text: t("guide.no_match.body") }),
      button({ label: t("guide.no_match.clear"), onClick: () => { input.value = ""; query = ""; paint(); input.focus(); } }),
    ]));
    if (!searching) body.querySelector("details")?.setAttribute("open", "");
  }

  const input = el("input", { class: "input", type: "search", placeholder: t("guide.search.ph"),
    "aria-label": t("guide.search.aria"), on: { input: event => {
      query = event.target.value.trim().toLowerCase(); paint();
    } } });
  fill(nav, sections.map(s => el("button", {
    class: "guide-toc__item", type: "button", text: s.title, data: { chapter: s.id },
    on: { click: () => {
      selected = s.id; input.value = ""; query = ""; paint();
      heading.focus({ preventScroll: true });
    } },
  })));
  heading.tabIndex = -1;
  const base = location.origin + "/v1";
  fill(root, el("div", { class: "stack guide-workspace" }, [
    el("header", { class: "guide-intro" }, [
      el("div", {}, [
        el("h2", { text: t("guide.intro.title") }),
        el("p", { class: "muted text-sm", text: t("guide.intro.desc") }),
      ]),
      el("span", { class: "badge", text: platform() === "linux" ? t("guide.intro.badge.linux") : platform() === "windows" ? t("guide.intro.badge.windows") : t("guide.intro.badge.both") }),
    ]),
    el("div", { class: "guide-search" }, [input, resultCount]),
    chapterPicker,
    el("div", { class: "guide-layout" }, [nav,
      el("section", { class: "guide-main", "aria-labelledby": "guide-current-heading" }, [
        el("div", { class: "stack stack--tight" }, [heading, description]), body,
      ]),
    ]),
    card({ title: t("guide.reference.title"), desc: t("guide.reference.desc"),
      body: el("div", { class: "guide-reference" }, [
        el("div", { class: "stack stack--tight" }, [el("span", { class: "field__label", text: t("guide.reference.api_label") }), copyable(base),
          el("p", { class: "field__help", text: t("guide.reference.api_help") }),
          button({ label: t("guide.reference.open_server"), onClick: () => navigate("server") })]),
        el("div", { class: "stack stack--tight" }, [el("span", { class: "field__label", text: t("guide.reference.portable") }),
          el("pre", { class: "code", text: JSON.stringify({ model: "<model ID from Models>", messages: [{ role: "user", content: "Hello" }] }, null, 2) }),
          el("p", { class: "field__help", text: t("guide.reference.portable_help") })]),
      ]) }),
    el("p", { class: "field__help", text: t("guide.disclaimer") }),
  ]));
  heading.id = "guide-current-heading";
  paint();
  return {};
}
