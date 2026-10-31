// Tiny DOM helpers. No framework, no virtual DOM, no template strings holding
// user data — el() sets text through textContent, so account names, domains and
// error messages can never become markup.

/** Create an element. props: {class, text, html, on:{click}, data:{}, ...attrs} */
export function el(tag, props = null, children = null) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;            // callers must pass trusted markup only
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else if (k === "data") for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
      else if (k in node && k !== "list") node[k] = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
  }
  append(node, children);
  return node;
}

/** Append a child, an array, a string, or nothing. */
export function append(parent, children) {
  if (children === null || children === undefined || children === false) return parent;
  if (Array.isArray(children)) { for (const c of children) append(parent, c); return parent; }
  parent.append(children instanceof Node ? children : document.createTextNode(String(children)));
  return parent;
}

/** Replace all children in one shot. */
export function fill(parent, children) {
  parent.replaceChildren();
  return append(parent, children);
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Inline SVG icon from a path string, sized to the current font. */
export function icon(path, size = 16) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of [].concat(path)) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

/** Copy text, with a clipboard-API fallback for non-secure contexts (plain
    http://<lan-ip>:8788 is exactly that case). */
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
  }
  try {
    const ta = el("textarea", { value: text, style: { position: "fixed", opacity: "0", top: "0" } });
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (_) { return false; }
}

/** Debounce trailing-edge. */
export function debounce(fn, ms = 250) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
