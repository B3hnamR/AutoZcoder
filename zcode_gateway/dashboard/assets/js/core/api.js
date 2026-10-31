// The single HTTP entry point. Holds the admin key, surfaces reachability, and
// turns auth failures into one event the shell listens to.

const KEY_STORAGE = "gw-admin-key";

// next.html holds the injection point (api_dash.py rewrites that shell, never
// these static modules) and hands the key over on window. Fall back to session
// storage for a key the user typed on an earlier page load.
let adminKey = String(window.__GW_ADMIN_KEY__ ?? sessionStorage.getItem(KEY_STORAGE) ?? "");
delete window.__GW_ADMIN_KEY__;

let reachable = true;
const listeners = { auth: new Set(), reach: new Set() };

export function getKey() { return adminKey; }

export function setKey(value) {
  adminKey = String(value || "").trim();
  if (adminKey) sessionStorage.setItem(KEY_STORAGE, adminKey);
  else sessionStorage.removeItem(KEY_STORAGE);
}

export function isReachable() { return reachable; }

/** on("auth"|"reach", fn) -> unsubscribe */
export function on(event, fn) {
  listeners[event]?.add(fn);
  return () => listeners[event]?.delete(fn);
}

function emit(event, payload) {
  for (const fn of listeners[event] || []) {
    try { fn(payload); } catch (err) { console.error(err); }
  }
}

function setReachable(next) {
  if (reachable === next) return;
  reachable = next;
  emit("reach", next);
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, { method = "GET", body, signal, headers } = {}) {
  const opts = {
    method,
    signal,
    headers: { Authorization: "Bearer " + adminKey, ...headers },
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = typeof body === "string" ? body : JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    setReachable(false);
    throw new ApiError("gateway unreachable", 0);
  }
  setReachable(true);

  if (res.status === 401 || res.status === 403) {
    emit("auth", res.status);
    throw new ApiError("admin key rejected", res.status);
  }
  if (!res.ok) {
    let detail = res.statusText || ("HTTP " + res.status);
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* keep statusText */ }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return null;
  try { return await res.json(); } catch (_) { return null; }
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: "GET" }),
  post: (path, body, opts) => request(path, { ...opts, method: "POST", body: body ?? {} }),
  put: (path, body, opts) => request(path, { ...opts, method: "PUT", body: body ?? {} }),
  del: (path, opts) => request(path, { ...opts, method: "DELETE" }),
};

/** True when the current key (or none at all) is accepted. */
export async function probe() {
  try {
    await api.get("/admin/dash/server");
    return true;
  } catch (_) {
    return false;
  }
}
