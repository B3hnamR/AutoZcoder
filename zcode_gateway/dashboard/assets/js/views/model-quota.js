// Pure public-account quota aggregation, shared by Models and Accounts.
const CONFIG_MODELS = ["GLM-5.3", "GLM-5.3-Flash"];
const key = (name) => String(name).trim().toLowerCase();
const finite = (value) => value !== null && value !== undefined && value !== ""
  && typeof value !== "boolean" && Number.isFinite(Number(value));
const seconds = (value) => {
  if (finite(value)) return Number(value);
  if (typeof value === "string" && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return Date.parse(value) / 1000;
  return NaN;
};

/**
 * Input: /admin/accounts public rows and an explicit epoch-seconds clock.
 * remaining: sum of finite, nonnegative remaining in started, unexpired buckets,
 * including blocked accounts and retained snapshots after check failures.
 * routable_remaining: that same sum restricted to quota-eligible accounts;
 * this is an estimate, not the backend's routing verdict (no credentials here).
 * Missing/future/expired/invalid buckets are unknown, never an assumed zero.
 * checked/unchecked partition accounts by a successful balance_checked_at.
 * stale overlaps them: a failed latest check or an elapsed bucket reset. There
 * is no arbitrary age TTL. errors counts failed latest checks, not last_error.
 * Model keys are case-insensitive only; aliases/namespaces are not conflated.
 */
export function aggregateModelQuotas(accounts, nowSeconds) {
  if (!Number.isFinite(nowSeconds)) throw new TypeError("nowSeconds must be finite epoch seconds");
  const rows = Array.isArray(accounts) ? accounts : [];
  const names = new Map(CONFIG_MODELS.map((name) => [key(name), name]));
  for (const account of rows) {
    for (const name of [...Object.keys(account.quotas || {}),
      ...Object.keys(account.model_caps || {}), ...(account.capped_models || [])]) {
      if (key(name) && !names.has(key(name))) names.set(key(name), String(name).trim());
    }
  }
  const models = [...names].map(([normalized, model]) => {
    const result = { model, remaining: 0, routable_remaining: 0, known: 0, unknown: 0,
      checked: 0, unchecked: 0, stale: 0, errors: 0, capped: 0, eligible: 0,
      pending: 0, expired: 0, accounts: [] };
    for (const account of rows) {
      const quotaEntries = Object.entries(account.quotas || {});
      const quota = (quotaEntries.find(([name, value]) => name === model && value != null)
        || quotaEntries.find(([name, value]) => key(name) === normalized && value != null))?.[1];
      const checkedAt = seconds(account.balance_checked_at);
      const checked = checkedAt > 0;
      const error = String(account.quota_check_error || "");
      const startsAt = seconds(quota?.starts_at);
      const resetsAt = seconds(quota?.resets_at);
      const pending = startsAt > nowSeconds;
      const expired = resetsAt > 0 && resetsAt <= nowSeconds;
      const invalidWindow = (quota?.starts_at != null && quota.starts_at !== "" && !Number.isFinite(startsAt))
        || (quota?.resets_at != null && quota.resets_at !== "" && !Number.isFinite(resetsAt));
      const known = !!quota && finite(quota.remaining) && Number(quota.remaining) >= 0
        && !pending && !expired && !invalidWindow;
      const remaining = known ? Number(quota.remaining) : null;
      const capEntry = Object.entries(account.model_caps || {}).find(([name]) => key(name) === normalized);
      const cap = capEntry?.[1];
      const releaseAt = seconds(typeof cap === "object" ? cap?.release_at : cap);
      const capped = capEntry
        ? (Number.isFinite(releaseAt) ? releaseAt > nowSeconds
          : (finite(cap?.release_in_s) ? Number(cap.release_in_s) > 0 : true))
        : (account.capped_models || []).some((name) => key(name) === normalized);
      const state = String(account.state || "").toLowerCase();
      const plans = account.plans || [];
      const planInactive = plans.length > 0 && !plans.some((plan) => plan.status === "active"
        && (!plan.starts_at || seconds(plan.starts_at) <= nowSeconds)
        && (!plan.ends_at || seconds(plan.ends_at) > nowSeconds));
      const blocked = ["dead", "exhausted", "cooling"].includes(state)
        || account.has_plan === false || planInactive || capped;
      const eligible = known && remaining > 0 && !blocked;
      const stale = !!error || expired;
      let reason = state === "dead" || state === "exhausted" ? "Account " + state
        : state === "cooling" ? "Account cooling"
        : account.has_plan === false || planInactive ? "No active plan"
        : capped ? "Model capped" : pending ? "Window not open"
        : expired ? "Reset elapsed; refresh needed" : !known ? "Quota unknown"
        : remaining === 0 ? "Quota exhausted" : "Quota eligible";
      const detail = { account_id: account.id, email: account.email, state, checked,
        checked_at: checked ? checkedAt : null, error, stale, known, remaining,
        eligible, capped, pending, expired, starts_at: startsAt, resets_at: resetsAt, reason };
      result.accounts.push(detail);
      result[checked ? "checked" : "unchecked"]++;
      result[known ? "known" : "unknown"]++;
      if (known) result.remaining += remaining;
      if (eligible) { result.eligible++; result.routable_remaining += remaining; }
      if (stale) result.stale++;
      if (error) result.errors++;
      if (capped) result.capped++;
      if (pending) result.pending++;
      if (expired) result.expired++;
    }
    return result;
  });
  const checked = rows.filter((account) => seconds(account.balance_checked_at) > 0).length;
  return { total_accounts: rows.length, checked, unchecked: rows.length - checked,
    errors: rows.filter((account) => !!account.quota_check_error).length,
    stale: rows.filter((account, index) => models.some((model) => model.accounts[index].stale)).length,
    models };
}
