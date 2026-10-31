// Searchable operating manual, with short task chapters and an error index.
import { el, fill } from "../core/dom.js";
import { card, button, copyable, table } from "../core/ui.js";
import { navigate } from "../core/router.js";
import { platform } from "../core/caps.js";
import { GUIDE, ERRORS } from "./guide-content.js";

export function mount({ root }) {
  let selected = "start", query = "";
  const body = el("div", { class: "guide-articles" });
  const nav = el("nav", { class: "guide-toc", "aria-label": "Guide chapters" });
  const resultCount = el("span", { class: "field__help", role: "status", "aria-live": "polite" });
  const heading = el("h2", { class: "guide-heading" });
  const description = el("p", { class: "muted text-sm" });
  const sections = [...GUIDE, { id: "errors", title: "Troubleshooting", desc: "Match the failing stage before changing settings.", items: ERRORS }];
  const chapterPicker = el("select", { class: "select guide-chapter-picker", "aria-label": "Guide chapter",
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
        item.sizing ? table({ columns: [{ label: "Workload" }, { label: "CPU" }, { label: "RAM" }, { label: "Guidance" }],
          rows: item.sizing, render: row => row }) : null,
        el("ol", { class: "guide-instructions" }, item.steps.map(text => el("li", { text }))),
        ...(item.commands || []).map(command => el("div", { class: "guide-command" }, copyable(command))),
        button({ label: "Open " + (item.route || section.route || "accounts"),
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
    heading.textContent = searching ? "Search results" : section.title;
    description.textContent = searching
      ? "Matches include instructions, error codes, platform notes and recovery steps."
      : section.desc;
    const entries = (searching ? sections : [section])
      .flatMap(s => s.items.filter(item => matches(s, item)).map(item => entry(s, item, searching)));
    resultCount.textContent = entries.length + (searching ? " matching topics" : " topics");
    fill(body, entries.length ? entries : el("div", { class: "empty" }, [
      el("h3", { text: "No matching topic" }),
      el("p", { class: "field__help", text: "Try a shorter phrase, an HTTP code, or a stage such as inbox, quota, browser or proxy." }),
      button({ label: "Clear search", onClick: () => { input.value = ""; query = ""; paint(); input.focus(); } }),
    ]));
    if (!searching) body.querySelector("details")?.setAttribute("open", "");
  }

  const input = el("input", { class: "input", type: "search", placeholder: "Search steps, errors, codes...",
    "aria-label": "Search the Guide", on: { input: event => {
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
        el("h2", { text: "Operate with confidence" }),
        el("p", { class: "muted text-sm", text: "Setup, day-to-day tasks, and recovery. Start with the failing stage, not another retry." }),
      ]),
      el("span", { class: "badge", text: platform() === "linux" ? "Linux VPS reference" : platform() === "windows" ? "Windows reference" : "Windows + Linux" }),
    ]),
    el("div", { class: "guide-search" }, [input, resultCount]),
    chapterPicker,
    el("div", { class: "guide-layout" }, [nav,
      el("section", { class: "guide-main", "aria-labelledby": "guide-current-heading" }, [
        el("div", { class: "stack stack--tight" }, [heading, description]), body,
      ]),
    ]),
    card({ title: "Connection reference", desc: "Use a client key for model traffic. These examples contain placeholders, never your saved credentials.",
      body: el("div", { class: "guide-reference" }, [
        el("div", { class: "stack stack--tight" }, [el("span", { class: "field__label", text: "API base URL (current origin)" }), copyable(base),
          el("p", { class: "field__help", text: "OpenAI chat: /v1/chat/completions | Anthropic messages: /v1/messages | Models: /v1/models" }),
          button({ label: "Open Server", onClick: () => navigate("server") })]),
        el("div", { class: "stack stack--tight" }, [el("span", { class: "field__label", text: "Portable request example" }),
          el("pre", { class: "code", text: JSON.stringify({ model: "<model ID from Models>", messages: [{ role: "user", content: "Hello" }] }, null, 2) }),
          el("p", { class: "field__help", text: "POST JSON to /v1/chat/completions with Content-Type: application/json and Authorization: Bearer <client key>." })]),
      ]) }),
    el("p", { class: "field__help", text: "This guide covers known workflows and failure classes, not every possible provider response. For an unknown error, capture the stage, HTTP/status code and minimal redacted logs. Never include account exports, JWTs, passwords, API tokens or inbox secrets." }),
  ]));
  heading.id = "guide-current-heading";
  paint();
  return {};
}
