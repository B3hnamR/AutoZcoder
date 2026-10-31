// Theme: dark by default, light on request, persisted locally.
// localStorage (not sessionStorage) — a look preference should survive a restart.

const STORAGE = "gw-theme";
const root = document.documentElement;

export function current() {
  return root.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export function apply(theme) {
  const next = theme === "light" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  try { localStorage.setItem(STORAGE, next); } catch (_) { /* private mode */ }
  return next;
}

export function toggle() {
  return apply(current() === "dark" ? "light" : "dark");
}

export function init() {
  let saved = null;
  try { saved = localStorage.getItem(STORAGE); } catch (_) { /* private mode */ }
  if (saved === "light" || saved === "dark") return apply(saved);
  const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)").matches;
  return apply(prefersLight ? "light" : "dark");
}
