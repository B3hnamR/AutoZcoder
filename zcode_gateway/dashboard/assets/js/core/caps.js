// Host capabilities, fetched once.
//
// The browser's own platform tells us nothing useful: you can sit at a Windows
// desktop while the gateway runs on a headless Linux VPS. Only the server knows
// whether a visible browser, the desktop-app import or self-update can work.

import { api } from "./api.js";

let caps = {
  os: "unknown",
  frozen: false,
  headless: false,
  features: {},
  notes: {},
};
let loaded = false;

export async function load() {
  try {
    caps = await api.get("/admin/dash/capabilities");
  } catch (_) {
    // An older gateway has no capabilities route. Assume everything works
    // rather than hiding controls the user may actually need.
    caps = { os: "unknown", frozen: false, headless: false, features: {}, notes: {} };
  }
  loaded = true;
  return caps;
}

/** True unless the host explicitly reported the feature as unavailable. */
export function can(feature) {
  if (!loaded) return true;
  const v = caps.features?.[feature];
  return v === undefined ? true : !!v;
}

/** Why a feature is unavailable, for a tooltip or inline note. */
export function why(feature) {
  return caps.notes?.[feature] || "";
}

export function platform() { return caps.os; }
export function isHeadless() { return !!caps.headless; }
export function all() { return caps; }

/**
 * Disable a control and explain why, instead of silently hiding it: a missing
 * button looks like a bug, a disabled one with a reason does not.
 */
export function gate(node, feature) {
  if (can(feature)) return node;
  node.disabled = true;
  node.classList.add("is-unavailable");
  const reason = why(feature);
  if (reason) node.title = reason;
  return node;
}
