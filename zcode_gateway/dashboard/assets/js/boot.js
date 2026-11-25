// Shell wiring: theme, login gate, nav, router, status chip.

import { $, el, fill, icon } from "./core/dom.js";
import { api, getKey, setKey, probe, on as onApi } from "./core/api.js";
import { toast } from "./core/toast.js";
import * as router from "./core/router.js";
import * as theme from "./core/theme.js";
import * as caps from "./core/caps.js";
import * as updates from "./core/updates.js";
import * as notifications from "./core/notifications.js";
import { ICONS } from "./core/icons.js";

// True once a key has been accepted. Guards the auth handler so the probe at
// startup cannot report a rejection for a key the user never entered.
let signedIn = false;

// Order here is the order in the sidebar.
const SECTIONS = [
  { group: "Operate", items: [
    { name: "accounts", title: "Accounts", icon: "accounts", poll: 6000,
      load: () => import("./views/accounts.js") },
    { name: "models", title: "Models", icon: "models", poll: 8000,
      load: () => import("./views/models.js") },
    { title: "Generator", icon: "bolt", children: [
      { name: "generator", title: "Manual signup", poll: 3000, load: () => import("./views/generator.js") },
      { name: "generator/auto", title: "Auto Generate", poll: 3000, load: () => import("./views/auto-generate.js") },
      { name: "generator/mail", title: "Email setup", load: () => import("./views/generator/mail.js") },
    ] },
    { title: "IP Routes", icon: "swap", children: [
      { name: "proxy", title: "Proxy pool", poll: 3000, load: () => import("./views/proxy.js") },
      ...[["psiphon", "Psiphon"], ["surfshark", "Surfshark"]].map(([provider, title]) => ({
        name: "ip-routes/" + provider, title, poll: 3000,
        load: async () => {
          const { mountProvider } = await import("./views/generator/vpn.js");
          return { mount: (ctx) => mountProvider(ctx, provider) };
        },
      })),
    ] },
    { name: "server", title: "Server", icon: "server", poll: 0,
      load: () => import("./views/server.js") },
  ]},
  { group: "Insight", items: [
    { name: "analytics", title: "Analytics", icon: "chart", poll: 15000,
      load: () => import("./views/analytics.js") },
    { name: "logs", title: "Logs", icon: "logs", poll: 4000,
      load: () => import("./views/logs.js") },
  ]},
  { group: "Configure", items: [
    { name: "settings", title: "Settings", icon: "settings", poll: 0,
      load: () => import("./views/settings.js") },
    { name: "guide", title: "Guide", icon: "help", poll: 0,
      load: () => import("./views/guide.js") },
  ]},
];

/* ---------------------------------------------------------------- nav ---- */

function buildNav() {
  const nav = $("#nav");
  const link = (item) => el("a", {
    class: "nav-item", href: "#/" + item.name, data: { route: item.name },
    on: { click: closeNav },
  }, [item.icon ? icon(ICONS[item.icon] || ICONS.dot) : null, el("span", { text: item.title })]);
  fill(nav, SECTIONS.map((group) => el("div", { class: "nav__group" }, [
    el("div", { class: "nav__label", text: group.group }),
    ...group.items.map((item) => item.children ? el("details", { class: "nav-branch" }, [
      el("summary", { class: "nav-item nav-branch__toggle" }, [
        icon(ICONS[item.icon]), el("span", { text: item.title }),
      ]),
      el("div", { class: "nav-branch__children" }, item.children.map(link)),
    ]) : link(item)),
  ])));
}

function markActive(name) {
  for (const btn of document.querySelectorAll(".nav-item[data-route]")) {
    const active = btn.dataset.route === name;
    btn.classList.toggle("is-active", active);
    if (active) {
      btn.setAttribute("aria-current", "page");
      const parent = btn.closest("details");
      if (parent) parent.open = true;
    }
    else btn.removeAttribute("aria-current");
  }
}

const closeNav = () => {
  $("#app").classList.remove("is-nav-open");
  $("#nav-toggle").setAttribute("aria-expanded", "false");
};

/* ------------------------------------------------------------- status ---- */

function paintStatus(up) {
  $("#dot-gateway").className = "dot " + (up ? "dot--ok dot--live" : "dot--err");
  $("#chip-gateway-text").textContent = up ? "online" : "unreachable";
}

/** Pool readiness and the Zcode app chip, polled independently of the view. */
async function paintChips() {
  try {
    const pool = await api.get("/admin/pool/state");
    const total = Object.values(pool.by_state || {}).reduce((a, b) => a + b, 0);
    const chip = $("#chip-pool");
    chip.textContent = pool.ready + "/" + total + " ready";
    chip.title = "Accounts able to serve a request right now";
    chip.hidden = total === 0;
  } catch (_) { $("#chip-pool").hidden = true; }

  try {
    const cs = await api.get("/admin/dash/zcode-status");
    const tone = { running: "ok", logged_in: "ok", installed: "warn", off: "" }[cs.chip_kind] || "";
    $("#dot-zcode").className = "dot" + (tone ? " dot--" + tone : "");
    $("#chip-zcode-text").textContent = cs.detail || "";
    // Only meaningful where the desktop app could exist.
    $("#chip-zcode").hidden = !cs.detail || cs.chip_kind === "off";
  } catch (_) { $("#chip-zcode").hidden = true; }
}

async function paintVersion() {
  try {
    const s = await api.get("/admin/dash/server");
    const os = caps.platform();
    const label = os === "linux" ? (caps.isHeadless() ? "Linux · headless" : "Linux")
                : os === "windows" ? "Windows" : "";
    $("#foot-version").textContent = "v" + s.version + (label ? " · " + label : "");
    $("#foot-version").title = s.host + ":" + s.port;
  } catch (_) { /* the view will report it */ }
}

/* -------------------------------------------------------------- login ---- */

function showLogin(message) {
  $("#app").hidden = true;
  $("#login-overlay").hidden = false;
  const err = $("#login-error");
  // No message means "we simply need a key" — a stale rejection must not sit
  // above an untouched field.
  err.textContent = message || "";
  err.hidden = !message;
  $("#login-key").focus();
}

function hideLogin() {
  $("#login-overlay").hidden = true;
  $("#app").hidden = false;
}

function wireLogin(onSuccess) {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#login-submit");
    const input = $("#login-key");
    const previous = getKey();
    setKey(input.value);
    btn.disabled = true;
    btn.textContent = "Checking…";
    if (await probe()) {
      input.value = "";
      $("#login-error").hidden = true;
      hideLogin();
      onSuccess();
    } else {
      setKey(previous);
      const err = $("#login-error");
      err.textContent = "That key was not accepted.";
      err.hidden = false;
      input.select();
    }
    btn.disabled = false;
    btn.textContent = "Continue";
  });
}

/* --------------------------------------------------------------- boot ---- */

function wireChrome() {
  const themeBtn = $("#theme-toggle");
  const paintTheme = () => fill(themeBtn, icon(theme.current() === "dark" ? ICONS.sun : ICONS.moon));
  themeBtn.addEventListener("click", () => { theme.toggle(); paintTheme(); });
  paintTheme();

  fill($("#btn-refresh"), icon(ICONS.refresh));
  $("#btn-refresh").addEventListener("click", () => router.refresh());

  fill($("#nav-toggle"), icon(ICONS.menu));
  $("#nav-toggle").addEventListener("click", () => {
    const open = $("#app").classList.toggle("is-nav-open");
    $("#nav-toggle").setAttribute("aria-expanded", String(open));
  });

  document.addEventListener("keydown", (e) => {
    // "r" reloads the section, Escape closes the mobile drawer. Never while
    // typing: a search box must keep its own keys.
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (e.key === "Escape") closeNav();
    if (e.key === "r" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) router.refresh();
  });
}

let appStarted = false;

async function startApp() {
  paintStatus(true);
  if (appStarted) {
    paintChips();
    notifications.poll();
    router.refresh();
    return;
  }
  appStarted = true;
  // Capabilities first: views ask what this host can do while they build.
  await caps.load();
  paintVersion();

  fill($("#btn-notify"), [icon(ICONS.bell), $("#notify-dot")]);
  fill($("#btn-update"), [icon(ICONS.download), $("#btn-update").firstElementChild]);
  notifications.init($("#btn-notify"), $("#notify-dot"));
  updates.init($("#btn-update"));

  paintChips();
  setInterval(paintChips, 10000);

  router.init({
    root: $("#view-inner"),
    fallback: SECTIONS[0].items[0].name,
    onNavigate: (route) => {
      $("#view-title").textContent = route.title;
      document.title = route.title + " · Zcode Gateway";
      markActive(route.name);
    },
  });
}

async function main() {
  theme.init();
  buildNav();
  wireChrome();

  for (const group of SECTIONS) {
    for (const item of group.items.flatMap(item => item.children || [item])) {
      router.register(item.name, { title: item.title, poll: item.poll, load: item.load });
    }
  }

  onApi("reach", paintStatus);
  wireLogin(() => { signedIn = true; startApp(); });

  // No key configured at all is a valid local setup, so probe before asking.
  // This first probe may legitimately 401, which is not a "rejected key" —
  // the auth handler is only armed once we are actually signed in.
  const ok = await probe();
  onApi("auth", () => {
    if (!signedIn) return;
    signedIn = false;
    showLogin("The admin key was rejected. Enter it again.");
  });

  if (ok) {
    signedIn = true;
    hideLogin();
    startApp();
  } else {
    showLogin();
  }
}

main().catch((err) => {
  console.error(err);
  toast("dashboard failed to start: " + err.message, "err", 10000);
});
