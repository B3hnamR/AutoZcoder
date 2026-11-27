// Models: reported quota is separate from the backend's routing verdict.
import { el, fill, $$ } from "../core/dom.js";
import { api } from "../core/api.js";
import { toast } from "../core/toast.js";
import { table, toolbar, searchBox, button, badge } from "../core/ui.js";
import { num, duration, relative, parseTime, dateTime } from "../core/format.js";
import { aggregateModelQuotas } from "./model-quota.js";
import { t } from "../core/i18n.js";

let query = "";
const opened = new Set();

function releaseText(info) {
  const ms = parseTime(info.release_at);
  if (Number.isFinite(ms)) return { text: relative(ms), title: dateTime(ms) };
  if (info.release_in_s != null && Number.isFinite(Number(info.release_in_s))) {
    return { text: t("models.in", { time: duration(info.release_in_s) }), title: "" };
  }
  return { text: t("models.release.unset"), title: "" };
}

function applyFilter(root) {
  const q = query.trim().toLowerCase();
  let shown = 0, total = 0;
  for (const item of $$(".model-entry", root)) {
    total++;
    const hit = !q || item.dataset.search.includes(q);
    item.hidden = !hit;
    if (hit) shown++;
  }
  const none = root.querySelector("[data-nomatch]");
  if (none) none.hidden = !(total > 0 && shown === 0);
}

function accountBreakdown(model, quotas) {
  const byId = new Map((model.accounts || []).map((account) => [account.account_id, account]));
  const details = quotas.accounts.map((account) => {
    const routing = byId.get(account.account_id);
    const release = routing && (routing.release_at || routing.release_in_s != null)
      ? releaseText(routing) : null;
    const freshness = !account.checked ? t("models.freshness.unchecked") : account.stale ? t("models.freshness.stale") : t("models.freshness.checked");
    return { cells: [
      el("div", { class: "model-account" }, [
        el("span", { text: account.email || account.account_id || t("models.acct.unknown") }),
        el("span", { class: "model-muted", text: account.state || t("models.state.unknown") }),
      ]),
      el("div", { class: "model-account" }, [
        el("span", { class: "model-number", text: account.known ? num(account.remaining) : t("models.freshness.unknown") }),
        account.pending ? el("span", { class: "model-muted", text: t("models.opens", { when: relative(account.starts_at * 1000) }) })
          : account.resets_at > 0 ? el("span", { class: "model-muted", text: (account.expired ? t("models.reset_elapsed") : t("models.resets", { when: relative(account.resets_at * 1000) }))
            , title: dateTime(account.resets_at * 1000) }) : null,
      ]),
      el("div", { class: "model-account" }, [
        badge(routing?.status === "available" ? t("models.stat.router") : routing?.status || t("models.state.unknown"),
          routing?.status === "available" ? "ok" : "warn"),
        el("span", { class: "model-muted", text: routing?.reason || account.reason }),
        routing?.message ? el("span", { class: "model-muted", text: routing.message }) : null,
        release ? el("span", { class: "model-muted", text: release.text, title: release.title }) : null,
      ]),
      el("div", { class: "model-account" }, [
        badge(freshness, account.stale || !account.checked ? "warn" : null),
        account.checked ? el("span", { class: "model-muted", text: relative(account.checked_at * 1000),
          title: dateTime(account.checked_at * 1000) }) : null,
        account.error ? el("span", { class: "model-warning", text: account.error }) : null,
      ]),
    ] };
  });
  const breakdown = table({
    columns: [{ label: t("models.detail.col.account") }, { label: t("models.detail.col.quota"), align: "right" },
      { label: t("models.detail.col.routing") }, { label: t("models.detail.col.release") }],
    rows: details, render: (row) => row.cells,
    empty: { title: t("models.empty"), text: t("models.noaccts.refresh") },
  });
  breakdown.classList.add("model-account-table");
  const grid = breakdown.querySelector("table");
  if (grid) {
    grid.classList.add("model-account-grid");
    breakdown.tabIndex = 0;
    breakdown.setAttribute("role", "region");
    breakdown.setAttribute("aria-label", model.model + " per-account quotas; scroll for all columns and accounts");
  }
  return breakdown;
}

function modelCard(model, quotas, index) {
  const serving = model.available_on ?? 0;
  const total = model.total_accounts ?? quotas.accounts.length;
  const status = model.available_on == null ? t("models.st.notcat") : !total ? t("models.st.noaccts") : serving === total ? t("models.st.allok")
    : serving > 0 ? t("models.st.partial") : t("models.st.none");
  const stats = [
    [t("models.stat.known"), quotas.known ? num(quotas.remaining) : t("models.unknown")],
    [t("models.stat.eligible"), quotas.known ? num(quotas.routable_remaining) : t("models.unknown")],
    [t("models.stat.router"), model.available_on == null ? t("models.unknown") : num(serving) + " / " + num(total)],
  ];
  const details = el("details", { class: "model-details", open: opened.has(model.model) }, [
    el("summary", { class: "model-details-toggle", text: t("models.detail", { name: num(total) }) }),
    el("div", { class: "model-details-body" }, accountBreakdown(model, quotas)),
  ]);
  details.addEventListener("toggle", () => {
    if (details.open) opened.add(model.model); else opened.delete(model.model);
  });
  const next = model.next_release_in_s != null ? releaseText({ release_in_s: model.next_release_in_s }) : null;
  return el("article", { class: "model-entry", "aria-labelledby": "model-title-" + index,
    data: { search: [model.model, model.overall, model.lane, status,
      ...quotas.accounts.map((a) => [a.email, a.account_id, a.state, a.reason, a.error].join(" ")),
      ...(model.accounts || []).map((a) => [a.status, a.reason, a.message].join(" ")),
    ].join(" ").toLowerCase() } }, [
    el("div", { class: "model-heading" }, [
      el("h2", { class: "model-title", id: "model-title-" + index, text: model.model }),
      badge(status, serving > 0 ? "ok" : "warn"),
    ]),
    el("dl", { class: "model-stats" }, stats.map(([label, value]) => el("div", { class: "model-stat" }, [
      el("dt", { class: "model-muted", text: label }),
      el("dd", { class: "model-value", text: value }),
    ]))),
    el("div", { class: "model-coverage" }, [
      el("span", { text: quotas.known + " / " + total + " " }),
      el("span", { text: quotas.checked }),
      el("span", { class: quotas.unchecked ? "model-warning" : "", text: quotas.unchecked }),
      el("span", { class: quotas.stale ? "model-warning" : "", text: quotas.stale }),
      el("span", { text: quotas.errors }),
      el("span", { text: quotas.capped }),
      quotas.pending ? el("span", { text: quotas.pending }) : null,
      next ? el("span", { text: next.text, title: next.title }) : null,
    ]),
    details,
  ]);
}

function render(root, data, accounts, reload, actions, actionStatus) {
  const catalogModels = (data.models || []).map((model) => model.model);
  const totals = aggregateModelQuotas(accounts, Date.now() / 1000, catalogModels);
  const byModel = new Map(totals.models.map((model) => [model.model.toLowerCase(), model]));
  const catalog = data.catalog || {};
  const models = [...(data.models || [])].sort((a, b) => a.model.localeCompare(b.model));
  // Quota buckets can outlive a captured catalogue; never hide their totals.
  const listed = new Set(models.map((model) => model.model.toLowerCase()));
  for (const model of totals.models) {
    if (!listed.has(model.model.toLowerCase())) models.push({ model: model.model, accounts: [], total_accounts: accounts.length });
  }
  const eligibleModels = models.filter((model) => model.available_on > 0).length;
  const updatedAt = parseTime(catalog.updated_at);
  const source = catalog.source === "live" ? t("models.live") : t("models.captured");
  const search = searchBox({ placeholder: t("models.search.ph"), value: query,
    onInput: (value) => { query = value; applyFilter(root); } });
  search.classList.add("model-search");
  const bar = toolbar([
    button({ label: t("models.btn.quotas"), icon: "chart", disabled: actions.busy,
      onClick: () => actions.run("quotas", accounts, reload) }),
    button({ label: t("models.btn.catalog"), icon: "refresh", disabled: actions.busy,
      onClick: () => actions.run("catalog", accounts, reload) }),
  ], search);
  bar.classList.add("model-toolbar");
  fill(root, el("div", { class: "model-view" }, [
    el("div", { class: "model-overview" }, [
      el("div", { class: "model-overview-main" }, [
        el("h2", { class: "model-section-title", text: t("models.capacity") }),
        el("p", { class: "model-muted", text: eligibleModels + " / " + models.length + " " + t("models.capacity.sub") }),
      ]),
      el("div", { class: "model-catalog" }, [
        badge(source, catalog.source === "live" ? "ok" : null),
        el("span", { class: "model-muted", text: Number.isFinite(updatedAt) ? relative(updatedAt)
          : "—", title: Number.isFinite(updatedAt) ? dateTime(updatedAt) : "" }),
        el("span", { class: "model-muted", text: data.generated_at ? relative(parseTime(data.generated_at)) : "" }),
      ]),
    ]),
    catalog.error ? el("p", { class: "model-warning", role: "status",
      text: catalog.error }) : null,
    bar,
    actionStatus,
    el("p", { class: "model-explanation", text: t("models.explanation") }),
    el("div", { class: "model-list" }, models.map((model, index) => {
      const quota = byModel.get(model.model.toLowerCase()) || { model: model.model, accounts: [],
        known: 0, remaining: 0, routable_remaining: 0, checked: 0, unchecked: 0, stale: 0, errors: 0, capped: 0, pending: 0 };
      return modelCard({ ...model, model: quota.model }, quota, index);
    })),
    el("div", { class: "model-empty", "data-nomatch": "", hidden: true,
      text: t("models.nomatch") }),
  ]));
  applyFilter(root);
}

export async function mount({ root, signal }) {
  let inFlight = null;
  const actionStatus = el("p", { class: "model-action-status", role: "status", "aria-live": "polite", hidden: true });
  const actions = {
    busy: false,
    async run(kind, accounts, reload) {
      if (actions.busy) return;
      actions.busy = true;
      for (const control of $$(".model-toolbar button", root)) control.disabled = true;
      actionStatus.hidden = false;
      try {
        if (kind === "catalog") {
          actionStatus.textContent = t("models.syncing");
          const result = await api.post("/admin/models/catalog/refresh", undefined, { signal });
          if (result.error) throw new Error(result.error);
          actionStatus.textContent = t("models.synced");
          toast(t("models.synced"), "ok");
        } else {
          let failed = 0;
          for (let index = 0; index < accounts.length; index++) {
            if (signal.aborted) return;
            actionStatus.textContent = t("models.refreshing") + ": " + (index + 1) + " / " + accounts.length;
            try {
              // Sequential checks avoid a burst across the credential pool.
              const result = await api.post("/admin/accounts/" + encodeURIComponent(accounts[index].id) + "/balance", undefined, { signal });
              if (result.quota_check_error) failed++;
            } catch (error) { if (signal.aborted) return; failed++; }
          }
          actionStatus.textContent = !accounts.length ? t("models.noaccts.refresh")
            : failed ? failed + " " + t("models.refresh.failed")
            : t("models.refresh.done");
          toast(actionStatus.textContent, failed ? "warn" : "ok");
        }
      } catch (error) {
        if (signal.aborted) return;
        actionStatus.textContent = "Refresh failed: " + error.message + ". Retry the action.";
        toast(actionStatus.textContent, "err");
      } finally {
        actions.busy = false;
        if (!signal.aborted) {
          for (const control of $$(".model-toolbar button", root)) control.disabled = false;
          try { await reload(true); } catch (error) {
            actionStatus.textContent += " Could not reload results: " + error.message;
          }
        }
      }
    },
  };
  const load = async (force = false) => {
    if (actions.busy && !force) return;
    if (inFlight) { await inFlight; if (!force) return; }
    inFlight = (async () => {
      const [data, result] = await Promise.all([
        api.get("/admin/models/availability", { signal }), api.get("/admin/accounts", { signal }),
      ]);
      if (signal.aborted) return;
      const active = document.activeElement;
      if (!force && active && root.contains(active)) return;
      render(root, data, result.accounts || [], load, actions, actionStatus);
    })();
    try { await inFlight; } finally { inFlight = null; }
  };
  await load();
  return { refresh: load };
}

