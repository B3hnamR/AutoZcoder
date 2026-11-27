// Accounts: the pool table, per-account actions, and the Zcode login flow.

import { el, fill, $$ } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast, confirmDialog } from "../core/toast.js";
import { card, table, toolbar, searchBox, button, stateBadge, badge, metric } from "../core/ui.js";
import { num, duration, dateTime, relative, parseTime } from "../core/format.js";
import { can, gate } from "../core/caps.js";
import { importJwt, copyAccountForSharing, copyAllAccountsForSharing } from "./accounts-jwt.js";
import { createClaimBatch } from "./claim-batch.js";
import { aggregateModelQuotas } from "./model-quota.js";
import { t } from "../core/i18n.js";

let query = "";

// Claims and signups need SOCKS proxies; an empty pool is the usual reason a
// claim fails, so it is worth a glance from here.
const socksBadge = el("span", { class: "badge", hidden: true });

async function paintSocks() {
  try {
    const r = await api.get("/admin/dash/proxy/socks_ready");
    const n = r.socks_ready;
    socksBadge.hidden = false;
    socksBadge.textContent = n < 0 ? t("acc.pool.unknown") : t("acc.pool.ready", { n });
    socksBadge.className = "badge" + (n < 0 ? "" : n === 0 ? " badge--err" : " badge--ok");
    socksBadge.title = r.last_verified_at
      ? t("acc.pool.last_verified", { when: relative(parseTime(r.last_verified_at)) })
      : t("acc.pool.never_verified");
  } catch (_) {
    socksBadge.hidden = true;        // no generator configured: not an error
  }
}

/* ------------------------------------------------------------- cells ---- */

function quotaCell(account) {
  const entries = Object.entries(account.quotas || {});
  const status = account.quota_check_error
    ? el("div", { class: "text-warn text-xs", text: account.quota_check_error,
                  title: t("acc.quota.refresh_retry") })
    : account.balance_checked_at
      ? el("div", { class: "faint text-xs", text: t("acc.quota.checked", { when: relative(parseTime(account.balance_checked_at)) }) })
      : null;
  if (!entries.length) return el("div", {}, [status || el("span", {
    class: "faint", text: t("acc.quota.awaiting") }),
    el("div", { class: "field__help", text: account.balance_checked_at ? t("acc.quota.no_buckets") : t("acc.quota.refresh_retry") })]);
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
          ? el("div", { class: "faint text-xs", text: t("acc.quota.resets", { when: relative(parseTime(q.resets_at)) }) })
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
          ? t("acc.cap.in", { time: duration(info.release_in_s) })
          : t("acc.cap.unknown");
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
      ? badge(t("acc.plan.no_entitlement"), "err")
      : el("span", { class: a.quota_check_error ? "text-warn text-xs" : "faint",
          text: a.quota_check_error ? t("acc.plan.check_failed") : t("acc.plan.awaiting"),
          title: a.quota_check_error || t("acc.plan.updated_with_quota") });
  }
  return el("div", { style: { display: "flex", flexDirection: "column", gap: "3px" } },
    plans.map((p) => el("div", {}, [
      el("div", { text: p.name || p.plan_id }),
      el("div", { class: "faint text-xs", text: (p.status || "") + (p.ends_at ? " · " + dateTime(parseTime(p.ends_at)) : " · " + t("acc.plan.no_expiry")) }),
    ])));
}

function tokenCell(a) {
  const ms = Number(a.expires_at) || 0;
  if (!ms) return el("span", { class: "faint", text: t("acc.token.no_expiry"),
    title: t("acc.token.no_expiry.help") });
  const left = ms - Date.now();
  if (left <= 0) return badge(t("acc.token.expired"), "err");
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
    text: t("acc.errors", { n: errs.length }),
    title: detail,
    style: { cursor: "help" },
  });
}

/* ----------------------------------------------------------- actions ---- */

async function accountAction(action, id, reload) {
  try {
    if (action === "balance") {
      const result = await api.post("/admin/accounts/" + encodeURIComponent(id) + "/balance");
      toast(result.quota_check_error || t("acc.action.balance_ok"), result.quota_check_error ? "warn" : "ok");
    } else {
      await api.post("/admin/accounts/" + encodeURIComponent(id) + "/" + action);
      toast(t("acc.action.generic", { action, id: id.split("-")[0] }), "ok");
    }
    reload();
  } catch (e) { toast(e.message, "err"); }
}

async function removeAccount(a, reload) {
  const ok = await confirmDialog({
    title: t("acc.remove.title"),
    body: t("acc.remove.body", { email: a.email || a.id }),
    okLabel: t("acc.remove.ok"),
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del("/admin/accounts/" + encodeURIComponent(a.id));
    toast(t("acc.action.removed"), "ok");
    reload();
  } catch (e) { toast(e.message, "err"); }
}

async function claimStart(id, mode) {
  try {
    const r = await api.post("/admin/accounts/" + encodeURIComponent(id) + "/claim_start", { mode });
    if (r?.error) toast(r.error, "err");
    else toast(t("acc.claim_queued", { mode }), "ok");
  } catch (e) { toast(t("acc.claim_queued.err", { msg: e.message }), "err"); }
}

async function purgeDrained(reload) {
  const ok = await confirmDialog({
    title: t("acc.purge.title"),
    body: t("acc.purge.body"),
    okLabel: t("acc.purge.ok"),
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api.post("/admin/accounts/purge_drained");
    toast(t("acc.purge.done", { n: r.count, plural: r.count === 1 ? "" : "s",
      unknown: r.unknown_state_kept ? t("acc.purge.unknown_kept", { n: r.unknown_state_kept }) : "" }), "ok");
    reload();
  } catch (e) { toast(t("acc.purge.err", { msg: e.message }), "err"); }
}

async function importFromApp(reload) {
  try {
    const r = await api.post("/admin/dash/import");
    await api.post("/admin/pool/reload");
    toast(t("acc.imported.count", { n: r.written ?? 0 }), "ok");
    reload();
  } catch (e) { toast("import: " + e.message, "err"); }
}

async function reloadPool(reload) {
  try {
    const r = await api.post("/admin/pool/reload");
    toast(t("acc.pool.reloaded", { n: r.loaded, added: r.added }), "ok");
    reload();
  } catch (e) { toast("reload: " + e.message, "err"); }
}

/* ------------------------------------------------------- login modal ---- */

async function loginWithZcode(reload, signal) {
  if (signal.aborted) return;
  let poll = null;
  let closed = false;
  const controller = new AbortController();
  const status = el("div", { class: "field__help", text: t("acc.login.opening") });
  const link = el("a", {
    class: "btn btn--primary btn--sm",
    target: "_blank",
    rel: "noopener",
    text: t("acc.login.open_page"),
    style: { display: "none" },
  });

  const modal = el("div", { class: "modal", role: "dialog", "aria-modal": "true" }, [
    el("div", { class: "modal__head" }, el("div", { class: "modal__title", text: t("acc.login.title") })),
    el("div", { class: "modal__body stack" }, [
      el("p", { text: t("acc.login.body") }),
      el("p", { class: "text-warn text-sm", text: t("acc.login.warn") }),
      link,
      status,
    ]),
    el("div", { class: "modal__foot" }, button({
      label: t("acc.login.cancel"), variant: "ghost",
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
      status.textContent = t("acc.login.waiting");
      window.open(start.verification_url, "_blank", "noopener");
    }
  } catch (e) {
    if (closed || signal.aborted || e.name === "AbortError") return;
    status.textContent = t("acc.login.start_failed", { msg: e.message });
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
        status.textContent = t("acc.login.waiting");
      }
      if (st.state === "done") {
        close();
        toast(t("acc.login.approved", { email: st.email || "" }), "ok");
        reload();
        return;
      } else if (["error", "failed", "expired", "cancelled"].includes(st.state)) {
        status.textContent = st.error || t("acc.login.state", { state: st.state });
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

function render(root, accounts, pool, reload, batch, signal, campaign = {}, models) {
  const rows = accounts.map((a) => Object.assign(a, {
    __searchText: [a.email, a.id, a.state, a.source, ...(a.capped_models || [])]
      .filter(Boolean).join(" ").toLowerCase(),
  }));
  // Weekend Build is a drop, not a permanent offer. Before the window opens
  // the per-account claim would only burn the marketing impression and come
  // back empty, so the buttons stay locked until Beijing Saturday 00:00.
  const campaignOpen = !campaign.gate || campaign.active;
  const campaignNote = campaign.active
    ? t("acc.campaign.open", { when: relative(parseTime(campaign.ends_at)) })
    : campaign.gate
      ? t("acc.campaign.locked", { when: relative(parseTime(campaign.next_starts_at)) })
      : null;
  const claimTitle = (mode) => campaignOpen
    ? t("acc.campaign.claim", { mode })
    : t("acc.campaign.locked_note");

  const counts = pool.by_state || {};
  const summary = el("div", { class: "grid grid--metrics" }, [
    metric({ label: t("acc.m.accounts"), value: num(pool.accounts ?? rows.length) }),
    metric({ label: t("acc.m.ready"), value: num(pool.ready ?? 0), tone: (pool.ready ?? 0) > 0 ? "ok" : "warn" }),
    metric({ label: t("acc.m.cooling"), value: num(counts.cooling ?? 0), tone: counts.cooling ? "warn" : null }),
    metric({ label: t("acc.m.dead"), value: num(counts.dead ?? 0), tone: counts.dead ? "err" : null }),
    metric({ label: t("acc.m.strategy"), value: pool.strategy || "—" }),
  ]);
  const quotaSummary = aggregateModelQuotas(accounts, Date.now() / 1000, models);
  const quotaTotals = el("div", { class: "grid grid--metrics" }, quotaSummary.models
    .map(model => {
    const node = metric({ label: model.model + " " + t("acc.m.tokens_remaining"),
      value: model.known ? num(model.remaining) : t("acc.m.unknown"),
      hint: t("acc.m.known_quota", { n: model.known, total: accounts.length, routable: num(model.routable_remaining), stale: model.stale ? " | " + model.stale + " " + t("acc.m.stale") : "" }) });
    node.title = t("acc.m.quota_hint");
    return node;
  }));

  const bar = toolbar([
    button({ label: t("acc.btn.login"), icon: "key", variant: "primary", onClick: () => loginWithZcode(reload, signal) }),
    button({ label: t("acc.btn.import"), icon: "key", onClick: () => importJwt(reload) }),
    button({ label: t("acc.btn.copy_all"), icon: "copy", onClick: copyAllAccountsForSharing }),
    // No desktop Zcode app on a server: the import would always 404.
    gate(button({ label: t("acc.btn.import_app"), icon: "copy", onClick: () => importFromApp(reload) }), "import_from_app"),
    button({ label: t("acc.btn.reload"), icon: "refresh", onClick: () => reloadPool(reload) }),
    button({ label: t("acc.btn.quotas"), icon: "chart", onClick: async (event) => {
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
        toast(failed ? t("acc.quotas.failed", { n: failed }) : t("acc.quotas.all_ok"),
              failed ? "warn" : "ok");
        await reload();
      } finally { control.disabled = false; }
    } }),
    button({ label: t("acc.btn.purge"), variant: "danger", title:
      t("acc.btn.purge_help"), onClick: () => purgeDrained(reload) }),
    socksBadge,
  ], searchBox({
    placeholder: t("acc.search.ph"),
    value: query,
    onInput: (v) => { query = v; applyFilter(root); },
  }));

  const body = table({
    columns: [
      { label: t("acc.col.account") }, { label: t("acc.col.state") }, { label: t("acc.col.plan") }, { label: t("acc.col.token") },
      { label: t("acc.col.quota") }, { label: t("acc.col.runs"), align: "right" }, { label: t("acc.col.capped") },
      { label: t("acc.col.errors") }, { label: t("acc.col.actions"), width: "110px" },
    ],
    rows,
    empty: {
      title: t("acc.empty.title"),
      text: t("acc.empty.text"),
      actions: [
        button({ label: t("acc.btn.login"), icon: "key", variant: "primary", onClick: () => loginWithZcode(reload, signal) }),
        can("import_from_app")
          ? button({ label: t("acc.btn.import_app"), onClick: () => importFromApp(reload) })
          : null,
      ].filter(Boolean),
    },
    render: (a) => [
      el("div", {}, [
        el("div", { class: "row row--tight" }, [
          el("span", { text: a.email || a.id, style: { fontWeight: "500" } }),
          a.imported_jwt ? el("span", { class: "badge badge--info", text: t("acc.imported"),
            title: t("acc.imported.help") }) : null,
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
        button({ icon: "copy", title: t("acc.act.copy"), onClick: () => copyAccountForSharing(a.id) }),
        button({ icon: "refresh", title: t("acc.act.refresh"), onClick: () => accountAction("refresh", a.id, reload) }),
        button({ icon: "chart", title: t("acc.act.check"), onClick: () => accountAction("balance", a.id, reload) }),
        button({ icon: "bolt", title: claimTitle("proxy"), disabled: !campaignOpen,
                 onClick: () => claimStart(a.id, "proxy") }),
        button({ icon: "terminal", title: claimTitle("direct"), disabled: !campaignOpen,
                 onClick: () => claimStart(a.id, "direct") }),
        button({ icon: "check", title: t("acc.act.enable"), onClick: () => accountAction("enable", a.id, reload) }),
        button({ icon: "eyeOff", title: t("acc.act.remove"), variant: "danger", onClick: () => removeAccount(a, reload) }),
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
      text: t("acc.no_match"),
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
    render(root, accs.accounts || [], pool || {}, load, batch, signal, campaign || {}, accs.models);
    batch.refresh();
  };

  await load();
  return { refresh: load };
}
