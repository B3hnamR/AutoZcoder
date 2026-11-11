// Accounts: the pool table, per-account actions, and the Zcode login flow.

import { el, fill, $$ } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { card, table, toolbar, searchBox, button, stateBadge, badge, metric } from "../core/ui.js";
import { num, duration, dateTime, relative, parseTime } from "../core/format.js";
import { can, gate } from "../core/caps.js";
import { importJwt, copyAccountForSharing } from "./accounts-jwt.js";
import { createClaimBatch } from "./claim-batch.js";
import { aggregateModelQuotas } from "./model-quota.js";

let query = "";

// Claims and signups need SOCKS proxies; an empty pool is the usual reason a
// claim fails, so it is worth a glance from here.
const socksBadge = el("span", { class: "badge", hidden: true });

async function paintSocks() {
  try {
    const r = await api.get("/admin/dash/proxy/socks_ready");
    const n = r.socks_ready;
    socksBadge.hidden = false;
    socksBadge.textContent = n < 0 ? "proxy pool unknown" : n + " socks ready";
    socksBadge.className = "badge" + (n < 0 ? "" : n === 0 ? " badge--err" : " badge--ok");
    socksBadge.title = r.last_verified_at
      ? "Last verified " + relative(parseTime(r.last_verified_at))
      : "Never verified";
  } catch (_) {
    socksBadge.hidden = true;        // no generator configured: not an error
  }
}

/* ------------------------------------------------------------- cells ---- */

function quotaCell(account) {
  const entries = Object.entries(account.quotas || {});
  const status = account.quota_check_error
    ? el("div", { class: "text-warn text-xs", text: account.quota_check_error,
                  title: "Use Refresh quotas to retry. Existing values are from the last successful check." })
    : account.balance_checked_at
      ? el("div", { class: "faint text-xs", text: "Checked " + relative(parseTime(account.balance_checked_at)) })
      : null;
  if (!entries.length) return el("div", {}, [status || el("span", {
    class: "faint", text: "Awaiting quota check" }),
    el("div", { class: "field__help", text: account.balance_checked_at ? "No quota buckets returned" : "Use Refresh quotas to retry" })]);
  return el("div", { class: "stack--tight", style: { display: "flex", flexDirection: "column", gap: "4px" } }, [
    ...entries.map(([model, q]) => {
      const total = Number(q.total) || 0;
      const left = Number(q.remaining) || 0;
      const pct = total > 0 ? Math.max(0, Math.min(100, (left / total) * 100)) : 0;
      const tone = pct <= 0 ? "err" : pct < 15 ? "warn" : "ok";
      return el("div", { class: "quota" }, [
        el("div", { class: "quota__head" }, [
          el("span", { class: "quota__model", text: model.split("/").pop() }),
          el("span", { class: "quota__nums mono text-xs", text: num(left) + " / " + num(total) }),
        ]),
        el("div", { class: "bar" }, el("div", { class: "bar__fill bar__fill--" + tone, style: { width: pct + "%" } })),
        q.resets_at
          ? el("div", { class: "faint text-xs", text: "resets " + relative(parseTime(q.resets_at)) })
          : null,
      ]);
    }), status]);
}

function capCell(account) {
  const caps = account.model_caps || {};
  const names = [...new Set([...Object.keys(caps), ...(account.capped_models || [])])].sort();
  if (!names.length) return el("span", { class: "faint", text: "—" });
  return el("div", { style: { display: "flex", flexDirection: "column", gap: "4px" } },
    names.map((model) => {
      const info = caps[model] || {};
      const ms = parseTime(info.release_at);
      const left = Number.isFinite(ms)
        ? relative(ms)
        : Number.isFinite(Number(info.release_in_s))
          ? "in " + duration(info.release_in_s)
          : "unknown";
      return el("div", { title: info.message || model }, [
        badge(model.split("/").pop(), "warn"),
        el("span", { class: "faint text-xs", text: " " + left }),
      ]);
    }));
}

function planCell(a) {
  const plans = a.plans || [];
  if (!plans.length) {
    return a.has_plan === false
      ? badge("no entitlement", "err")
      : el("span", { class: a.quota_check_error ? "text-warn text-xs" : "faint",
          text: a.quota_check_error ? "Check failed" : "Awaiting quota check",
          title: a.quota_check_error || "Updated together with quota checks" });
  }
  return el("div", { style: { display: "flex", flexDirection: "column", gap: "3px" } },
    plans.map((p) => el("div", {}, [
      el("div", { text: p.name || p.plan_id }),
      el("div", { class: "faint text-xs", text: (p.status || "") + (p.ends_at ? " · " + dateTime(parseTime(p.ends_at)) : " · no expiry") }),
    ])));
}

function tokenCell(a) {
  const ms = Number(a.expires_at) || 0;
  if (!ms) return el("span", { class: "faint", text: "No declared expiry",
    title: "This token has no expiry timestamp. It can still be revoked or invalidated by the provider." });
  const left = ms - Date.now();
  if (left <= 0) return badge("expired", "err");
  return el("span", {
    class: left < 36e5 ? "text-warn" : "",
    text: relative(ms),
    title: dateTime(ms),
  });
}

function errorCell(a) {
  const errs = (a.error_history || []).slice(-12);
  if (!errs.length) return el("span", { class: "faint", text: "—" });
  const detail = errs.map((e) => (e.ago_s ?? "?") + "s ago · " + (e.status || "") + " " + (e.code || "")).join("\n");
  return el("span", {
    class: "mono text-xs " + (a.state === "dead" ? "text-err" : "text-warn"),
    text: errs.length + " err",
    title: detail,
    style: { cursor: "help" },
  });
}

/* ----------------------------------------------------------- actions ---- */

async function accountAction(action, id, reload) {
  try {
    if (action === "balance") {
      const result = await api.post("/admin/accounts/" + encodeURIComponent(id) + "/balance");
      toast(result.quota_check_error || "Quotas checked", result.quota_check_error ? "warn" : "ok");
    } else {
      await api.post("/admin/accounts/" + encodeURIComponent(id) + "/" + action);
      toast(action + " · " + id.split("-")[0] + "…", "ok");
    }
    reload();
  } catch (e) { toast(e.message, "err"); }
}

async function removeAccount(a, reload) {
  const ok = await confirmDialog({
    title: "Remove this account?",
    body: (a.email || a.id) + " leaves the pool and stops serving requests.\n"
        + "Its snapshot is kept; reloading the pool can restore it.",
    okLabel: "Remove",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del("/admin/accounts/" + encodeURIComponent(a.id));
    toast("Account removed", "ok");
    reload();
  } catch (e) { toast(e.message, "err"); }
}

async function claimStart(id, mode) {
  try {
    const r = await api.post("/admin/accounts/" + encodeURIComponent(id) + "/claim_start", { mode });
    if (r?.error) toast(r.error, "err");
    else toast("Claim queued (" + mode + ") — watch it in Generator", "ok");
  } catch (e) { toast("claim_start: " + e.message, "err"); }
}

async function purgeDrained(reload) {
  const ok = await confirmDialog({
    title: "Purge drained accounts?",
    body: "Removes dead accounts and accounts with 0 tokens left on ALL models, and DELETES their "
        + "snapshot file from the accounts folder.\n\n"
        + "Accounts whose quota resets later today are kept.",
    okLabel: "Purge",
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api.post("/admin/accounts/purge_drained");
    toast("Purged " + r.count + " account" + (r.count === 1 ? "" : "s")
          + (r.unknown_state_kept ? " · " + r.unknown_state_kept + " unchecked kept" : ""), "ok");
    reload();
  } catch (e) { toast("purge: " + e.message, "err"); }
}

async function importFromApp(reload) {
  try {
    const r = await api.post("/admin/dash/import");
    await api.post("/admin/pool/reload");
    toast("Imported " + (r.written ?? 0) + " account(s)", "ok");
    reload();
  } catch (e) { toast("import: " + e.message, "err"); }
}

async function reloadPool(reload) {
  try {
    const r = await api.post("/admin/pool/reload");
    toast("Pool reloaded · " + r.loaded + " account(s), " + r.added + " new", "ok");
    reload();
  } catch (e) { toast("reload: " + e.message, "err"); }
}

/* ------------------------------------------------------- login modal ---- */

async function loginWithZcode(reload, signal) {
  if (signal.aborted) return;
  let poll = null;
  let closed = false;
  const controller = new AbortController();
  const status = el("div", { class: "field__help", text: "Opening the approval page…" });
  const link = el("a", {
    class: "btn btn--primary btn--sm",
    target: "_blank",
    rel: "noopener",
    text: "Open login page",
    style: { display: "none" },
  });

  const modal = el("div", { class: "modal", role: "dialog", "aria-modal": "true" }, [
    el("div", { class: "modal__head" }, el("div", { class: "modal__title", text: "Login with Zcode" })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: "Open the login page and approve access. No Zcode install needed." }),
      el("p", { class: "text-warn text-sm", text:
        "Sign in with the account you want to add — if the browser is already signed in as someone else, sign out first." }),
      link,
      status,
    ]),
    el("div", { class: "modal__foot" }, button({
      label: "Cancel", variant: "ghost",
      onClick: cancel,
    })),
  ]);
  const overlay = el("div", { class: "overlay" }, modal);
  document.body.append(overlay);

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(poll);
    controller.abort();
    signal.removeEventListener("abort", cancel);
    overlay.remove();
  }
  function cancel() {
    if (closed) return;
    close();
    api.post("/admin/dash/auth/login/cancel").catch(() => {});
  }
  signal.addEventListener("abort", cancel, { once: true });

  try {
    const start = await api.post("/admin/dash/auth/login/start", {}, { signal: controller.signal });
    if (closed || signal.aborted) return;
    if (start?.verification_url) {
      link.href = start.verification_url;
      link.style.display = "";
      status.textContent = "Waiting for approval…";
      window.open(start.verification_url, "_blank", "noopener");
    }
  } catch (e) {
    if (closed || signal.aborted || e.name === "AbortError") return;
    status.textContent = "Could not start login: " + e.message;
    status.className = "field__error";
    return;
  }

  async function checkStatus() {
    if (closed || signal.aborted) return;
    try {
      const st = await api.get("/admin/dash/auth/login/status", { signal: controller.signal });
      if (closed || signal.aborted) return;
      if (st.verification_url) {
        link.href = st.verification_url;
        link.style.display = "";
        status.textContent = "Waiting for approval...";
      }
      if (st.state === "done") {
        close();
        toast("Account added", "ok");
        reload();
        return;
      } else if (["error", "failed", "expired", "cancelled"].includes(st.state)) {
        status.textContent = st.error || ("Login " + st.state + ". Start again to retry.");
        status.className = "field__error";
        return;
      }
    } catch (_) { /* keep waiting */ }
    if (!closed && !signal.aborted) poll = setTimeout(checkStatus, 1500);
  }
  if (!closed && !signal.aborted) poll = setTimeout(checkStatus, 1500);
}

/* ------------------------------------------------------------ render ---- */

function applyFilter(root) {
  const q = query.trim().toLowerCase();
  let shown = 0, total = 0;
  for (const tr of $$("tbody tr[data-search]", root)) {
    total++;
    const hit = !q || tr.dataset.search.includes(q);
    tr.hidden = !hit;
    if (hit) shown++;
  }
  const none = root.querySelector("[data-nomatch]");
  // Only a filtered-out non-empty table has "no matches"; an empty pool shows
  // its own "No accounts yet" panel, not a filter message underneath it.
  if (none) none.hidden = !(total > 0 && shown === 0);
}

function render(root, accounts, pool, reload, batch, signal, campaign = {}) {
  const rows = accounts.map((a) => Object.assign(a, {
    __searchText: [a.email, a.id, a.state, a.source, ...(a.capped_models || [])]
      .filter(Boolean).join(" ").toLowerCase(),
  }));
  // Weekend Build is a drop, not a permanent offer. Before the window opens
  // the per-account claim would only burn the marketing impression and come
  // back empty, so the buttons stay locked until Beijing Saturday 00:00.
  const campaignOpen = !campaign.gate || campaign.active;
  const campaignNote = campaign.active
    ? "Weekend Build is open — closes " + relative(parseTime(campaign.ends_at))
    : campaign.gate
      ? "Weekend Build opens " + relative(parseTime(campaign.next_starts_at))
      : null;
  const claimTitle = (mode) => campaignOpen
    ? "Claim Start Plan / Weekend Build card (" + mode + ")"
    : "Locked until the Weekend Build window opens";

  const counts = pool.by_state || {};
  const summary = el("div", { class: "grid grid--metrics" }, [
    metric({ label: "Accounts", value: num(pool.accounts ?? rows.length) }),
    metric({ label: "Ready", value: num(pool.ready ?? 0), tone: (pool.ready ?? 0) > 0 ? "ok" : "warn" }),
    metric({ label: "Cooling", value: num(counts.cooling ?? 0), tone: counts.cooling ? "warn" : null }),
    metric({ label: "Dead", value: num(counts.dead ?? 0), tone: counts.dead ? "err" : null }),
    metric({ label: "Strategy", value: pool.strategy || "—" }),
  ]);
  const quotaSummary = aggregateModelQuotas(accounts, Date.now() / 1000);
  const quotaTotals = el("div", { class: "grid grid--metrics" }, quotaSummary.models
    .filter(model => ["GLM-5.3", "GLM-5.3-Flash"].includes(model.model)).map(model => {
    const node = metric({ label: model.model + " tokens remaining",
      value: model.known ? num(model.remaining) : "Unknown",
      hint: model.known + "/" + accounts.length + " accounts with known quota | "
        + num(model.routable_remaining) + " quota-eligible estimate"
        + (model.stale ? " | " + model.stale + " stale/error" : "") });
    node.title = "Sum of current quota windows across loaded accounts, including capped accounts. Future and elapsed windows are not counted. Unchecked accounts are unknown, not zero.";
    return node;
  }));

  const bar = toolbar([
    button({ label: "Login with Zcode", icon: "key", variant: "primary", onClick: () => loginWithZcode(reload, signal) }),
    button({ label: "Import account", icon: "key", onClick: () => importJwt(reload) }),
    // No desktop Zcode app on a server: the import would always 404.
    gate(button({ label: "Import from app", icon: "copy", onClick: () => importFromApp(reload) }), "import_from_app"),
    button({ label: "Reload pool", icon: "refresh", onClick: () => reloadPool(reload) }),
    button({ label: "Refresh quotas", icon: "chart", onClick: async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      let failed = 0;
      try {
        // Sequential checks avoid turning a transient upstream block into a
        // burst of requests from every account at once.
        for (const account of accounts) {
          try {
            const result = await api.post("/admin/accounts/" + encodeURIComponent(account.id) + "/balance");
            if (result.quota_check_error) failed++;
          } catch (_) { failed++; }
        }
        toast(failed ? failed + " quota check(s) failed; last known values kept" : "All quotas refreshed",
              failed ? "warn" : "ok");
        await reload();
      } finally { control.disabled = false; }
    } }),
    button({ label: "Purge drained", variant: "danger", title:
      "Remove accounts with 0 tokens left on every model", onClick: () => purgeDrained(reload) }),
    socksBadge,
  ], searchBox({
    placeholder: "Filter accounts…",
    value: query,
    onInput: (v) => { query = v; applyFilter(root); },
  }));

  const body = table({
    columns: [
      { label: "Account" }, { label: "State" }, { label: "Plan" }, { label: "Token" },
      { label: "Daily quota" }, { label: "Runs", align: "right" }, { label: "Capped" },
      { label: "Errors" }, { label: "Actions", width: "110px" },
    ],
    rows,
    empty: {
      title: "No accounts yet",
      text: "Sign in with Zcode to add the first one — no Zcode install needed.",
      actions: [
        button({ label: "Login with Zcode", icon: "key", variant: "primary", onClick: () => loginWithZcode(reload, signal) }),
        can("import_from_app")
          ? button({ label: "Import from app", onClick: () => importFromApp(reload) })
          : null,
      ].filter(Boolean),
    },
    render: (a) => [
      el("div", {}, [
        el("div", { class: "row row--tight" }, [
          el("span", { text: a.email || a.id, style: { fontWeight: "500" } }),
          a.imported_jwt ? el("span", { class: "badge badge--info", text: "Imported",
            title: "Imported account credential. Reimport a current export or log in again if the token expires or is revoked." }) : null,
        ]),
        el("div", { class: "mono faint text-xs", text: a.id }),
      ]),
      stateBadge(a.state),
      planCell(a),
      tokenCell(a),
      quotaCell(a),
      String(a.in_flight ?? 0),
      capCell(a),
      errorCell(a),
      el("div", { class: "account-actions" }, [
        button({ icon: "copy", title: "Copy account for sharing", onClick: () => copyAccountForSharing(a.id) }),
        button({ icon: "refresh", title: "Reload token", onClick: () => accountAction("refresh", a.id, reload) }),
        button({ icon: "chart", title: "Check quotas", onClick: () => accountAction("balance", a.id, reload) }),
        button({ icon: "bolt", title: claimTitle("proxy"), disabled: !campaignOpen,
                 onClick: () => claimStart(a.id, "proxy") }),
        button({ icon: "terminal", title: claimTitle("direct"), disabled: !campaignOpen,
                 onClick: () => claimStart(a.id, "direct") }),
        button({ icon: "check", title: "Enable", onClick: () => accountAction("enable", a.id, reload) }),
        button({ icon: "eyeOff", title: "Remove", variant: "danger", onClick: () => removeAccount(a, reload) }),
      ]),
    ],
  });

  const tableCard = card({
    title: "Pool",
    desc: rows.length
      ? "Quota resets and plan expiry use your local timezone."
        + (campaignNote ? " " + campaignNote : "")
      : null,
    body: [body, el("div", {
      class: "empty__text", "data-nomatch": "", hidden: true,
      text: "No accounts match that filter.",
    })],
    flush: true,
  });

  fill(root, el("div", { class: "stack" }, [summary, quotaTotals, bar, batch.node, tableCard]));
  applyFilter(root);
  paintSocks();
}

export async function mount({ root, signal }) {
  const batch = createClaimBatch({ signal, compact: true });
  const load = async () => {
    if (signal.aborted) return;
    const [accs, pool, campaign] = await Promise.all([
      api.get("/admin/accounts", { signal }),
      api.get("/admin/pool/state", { signal }),
      api.get("/admin/accounts/campaign_window", { signal }).catch(() => ({ gate: false })),
    ]);
    if (signal.aborted) return;
    // Do not rebuild while the user is typing in the filter box.
    const active = document.activeElement;
    if (active && active.type === "search" && root.contains(active)) {
      return;
    }
    render(root, accs.accounts || [], pool || {}, load, batch, signal, campaign || {});
    batch.refresh();
  };

  await load();
  return { refresh: load };
}

