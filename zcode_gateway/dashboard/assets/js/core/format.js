// Value formatting shared by every view. Pure functions, no DOM.

const DASH = "—";

export function num(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : DASH;
}

export function usd(micro) {
  return micro === null || micro === undefined ? DASH : "$" + (Number(micro) / 1e6).toFixed(2);
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return DASH;
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 60) return s + "s";
  const m = Math.round(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}

export function bytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + " " + units[i];
}

/** Absolute local time, e.g. "Oct 30, 14:05:11". */
export function dateTime(ms) {
  if (!Number.isFinite(ms)) return DASH;
  return new Date(ms).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hourCycle: "h23",
  });
}

/** "3m ago" / "in 2h 5m". */
export function relative(ms, now = Date.now()) {
  if (!Number.isFinite(ms)) return DASH;
  const delta = ms - now;
  const future = delta > 0;
  const s = Math.abs(Math.round(delta / 1000));
  if (s < 10) return future ? "in a moment" : "just now";
  const text = duration(s);
  return future ? "in " + text : text + " ago";
}

/**
 * Parse a timestamp the API may send as ISO, epoch seconds or epoch ms.
 * A naive ISO string (no Z, no offset) is ambiguous, so it is rejected rather
 * than silently read as local time.
 */
export function parseTime(value) {
  if (value === null || value === undefined || value === "") return NaN;
  if (typeof value === "number") return value > 1e12 ? value : value * 1000;
  const text = String(value);
  if (/^\d+$/.test(text)) return parseTime(Number(text));
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) return NaN;
  return Date.parse(text);
}

export function titleCase(name) {
  return String(name || "").replace(/(^|[\s-])\w/g, (c) => c.toUpperCase());
}

/** Mask all but the last `tail` characters of a secret. */
export function maskSecret(secret, tail = 4) {
  const text = String(secret || "");
  if (text.length <= tail) return "•".repeat(text.length);
  return "•".repeat(Math.min(24, text.length - tail)) + text.slice(-tail);
}
