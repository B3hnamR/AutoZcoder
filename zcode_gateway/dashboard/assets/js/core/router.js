// Hash router over lazily-imported view modules.
//
// A view module exports:
//   mount(ctx) -> optional cleanup function
// where ctx = { root, signal, refresh }. Its DOM is thrown away on leave, and
// `signal` aborts in-flight requests, so a slow tab cannot paint over the next.

import { errorState } from "./toast.js";

const routes = new Map();
let current = null;          // { name, cleanup, controller, timer }
let viewRoot = null;
let onChange = () => {};

export function register(name, { title, poll = 0, load }) {
  routes.set(name, { name, title, poll, load });
}

export function init({ root, fallback, onNavigate }) {
  viewRoot = root;
  onChange = onNavigate || (() => {});
  window.addEventListener("hashchange", () => go(fromHash() || fallback));
  go(fromHash() || fallback);
}

function fromHash() {
  const name = (location.hash || "").replace(/^#\/?/, "").trim().split("?")[0];
  if (name === "usage" || name === "stats") {
    history.replaceState(null, "", "#/analytics?range=" + (name === "usage" ? "30d" : "all"));
    return "analytics";
  }
  return routes.has(name) ? name : null;
}

export function navigate(name) {
  if (!routes.has(name)) return;
  if (fromHash() === name) { go(name); return; }
  location.hash = "#/" + name;          // triggers hashchange -> go()
}

export function currentRoute() { return current?.name || null; }

/** Re-run the active view's own refresh (polling + manual reload share this). */
export function refresh() {
  if (current?.refresh) current.refresh();
}

async function go(name) {
  const route = routes.get(name);
  if (!route) return;

  teardown();

  const controller = new AbortController();
  const entry = { name, controller, cleanup: null, timer: null, refresh: null };
  current = entry;

  viewRoot.replaceChildren();
  viewRoot.scrollTop = 0;
  onChange(route);

  let mod;
  try {
    mod = await route.load();
  } catch (err) {
    if (current !== entry) return;
    errorState(viewRoot, "This section failed to load: " + err.message);
    return;
  }
  if (current !== entry) return;        // navigated away while importing

  const ctx = {
    root: viewRoot,
    signal: controller.signal,
    refresh: () => entry.refresh && entry.refresh(),
  };

  try {
    const result = await mod.mount(ctx);
    if (current !== entry) { if (typeof result === "function") result(); return; }
    if (typeof result === "function") entry.cleanup = result;
    else if (result && typeof result === "object") {
      entry.cleanup = result.destroy || null;
      entry.refresh = result.refresh || null;
    }
  } catch (err) {
    if (current !== entry || err?.name === "AbortError") return;
    errorState(viewRoot, err.message, () => go(name));
    return;
  }

  if (route.poll && entry.refresh) {
    entry.timer = setInterval(() => {
      if (document.hidden) return;      // a backgrounded tab must not hammer the gateway
      Promise.resolve(entry.refresh()).catch(() => {});
    }, route.poll);
  }
}

function teardown() {
  if (!current) return;
  clearInterval(current.timer);
  current.controller.abort();
  try { current.cleanup?.(); } catch (err) { console.error(err); }
  current = null;
}
