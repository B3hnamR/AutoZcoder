// One telemetry workspace: all summaries, charts and exports share a period.
import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { card, table, toolbar, metric, button, stateBadge } from "../core/ui.js";
import { t } from "../core/i18n.js";
import { barChart, compactNumber } from "../core/chart.js";
import { num, relative, parseTime, dateTime, duration } from "../core/format.js";
import { comparison, downloadCsv } from "./analytics-export.js";

const RANGES = [["1h", t("analytics.range.1h")], ["6h", t("analytics.range.6h")], ["24h", t("analytics.range.24h")],
  ["7d", t("analytics.range.7d")], ["30d", t("analytics.range.30d")], ["all", t("analytics.range.all")]];
const rate = s => s?.requests ? (s.ok / s.requests) * 100 : null;
const seconds = ms => ms == null ? "No samples" : (ms / 1000).toFixed(2) + "s";

function switches(options, selected, label, change) {
  const group = el("div", { class: "analytics-switch", role: "group", "aria-label": label });
  fill(group, options.map(([key, text]) => el("button", {
    class: "btn btn--sm btn--quiet" + (selected === key ? " is-selected" : ""),
    type: "button", text, "aria-pressed": String(selected === key),
    on: { click: () => {
      for (const b of group.children) {
        b.classList.toggle("is-selected", b.textContent === text);
        b.setAttribute("aria-pressed", String(b.textContent === text));
      }
      change(key);
    } },
  })));
  return group;
}

export async function mount({ root, signal }) {
  const requested = new URLSearchParams(location.hash.split("?")[1] || "").get("range");
  let range = RANGES.some(([key]) => key === requested) ? requested : "24h";
  let chartMetric = "tokens", dimension = "model", query = "", sortBy = "tokens";
  let data = null, generation = 0, requestController = null;

  const summaries = el("div", { class: "analytics-summary" });
  const latencyBody = el("div");
  const chartBody = el("div");
  const breakdownBody = el("div");
  const errorsBody = el("div");
  const status = el("span", { class: "field__help", role: "status" });
  const error = el("div", { class: "notice notice--err", role: "alert", hidden: true });
  const periodText = el("p", { class: "field__help" });

  const picker = el("select", {
    class: "select analytics-range", "aria-label": "Analytics time range",
    on: { change: () => {
      range = picker.value;
      history.replaceState(null, "", "#/analytics?range=" + range);
      load();
    } },
  }, RANGES.map(([key, text]) => el("option", { value: key, text, selected: key === range })));

  function rowsForDimension() {
    const rows = data?.["by_" + dimension] || [];
    return rows.filter(r => !query || [r.name, r.email, r.model, r.account_id, r.client_key]
      .some(v => String(v || "").toLowerCase().includes(query.toLowerCase())))
      .slice().sort((a, b) => (Number(b[sortBy]) || 0) - (Number(a[sortBy]) || 0));
  }

  function rowName(r) {
    return r.email || r.name || r.model || r.account_id || r.client_key || "Unknown";
  }

  function exportBreakdown() {
    const rows = rowsForDimension();
    downloadCsv("analytics-" + data.range.id + "-" + dimension + ".csv",
      [dimension, "requests", "succeeded", "failed", "tokens", "input_tokens", "output_tokens", "avg_latency_ms", "last_seen_utc"],
      rows.map(r => [rowName(r), r.requests, r.ok, r.requests - r.ok, r.tokens,
        r.prompt_tokens, r.completion_tokens, r.avg_duration_ms,
        r.last_ts ? new Date(parseTime(r.last_ts)).toISOString() : ""]));
  }

  const exportButton = button({ label: t("analytics.export.breakdown"), icon: "download", onClick: exportBreakdown, disabled: true });
  const trafficExport = button({ label: t("analytics.export.traffic"), icon: "download", onClick: () => {
    downloadCsv("analytics-" + data.range.id + "-traffic.csv",
      ["bucket_start_utc", "requests", "succeeded", "failed", "tokens"],
      (data?.series || []).map(r => [new Date(r.ts * 1000).toISOString(), r.requests, r.ok, r.errors, r.tokens]));
  }, disabled: true });

  function paintChart() {
    if (!data) return;
    const rows = data.series || [];
    const bucket = data.range.bucket_seconds;
    const labelOptions = bucket < 3600 ? { hour: "2-digit", minute: "2-digit" }
      : bucket < 86400 ? { hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric" };
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const stride = Math.max(1, Math.ceil(rows.length / 12));
    const plot = barChart({
      height: 190,
      data: rows.map((r, i) => ({
        label: (i % stride === 0 && i < rows.length - Math.max(1, Math.floor(stride / 2))) || i === rows.length - 1
          ? new Date(r.ts * 1000).toLocaleString(undefined, labelOptions) : "",
        value: r[chartMetric], tone: r.errors ? (r.errors > r.requests / 4 ? "err" : "warn") : null,
        title: dateTime(r.ts * 1000) + " | " + num(r.requests) + " " + t("analytics.chart.requests") + " | " + num(r.tokens)
          + " " + t("analytics.chart.tokens") + " | " + num(r.errors) + " " + t("analytics.chart.failed"),
      })),
    });
    const peak = Math.max(0, ...rows.map(r => Number(r[chartMetric]) || 0));
    const peakAt = rows.find(r => r[chartMetric] === peak);
    fill(chartBody, [
      el("div", { class: "row row--between analytics-chart-meta" }, [
        el("span", { class: "field__help", text: t("analytics.chart.buckets") + " " + duration(bucket) + " | " + zone }),
        el("span", { class: "field__help", text: peak ? t("analytics.chart.peak", { n: num(peak), metric: chartMetric, at: dateTime(peakAt.ts * 1000) })
          : t("analytics.chart.no_traffic") }),
      ]),
      plot,
      el("p", { class: "field__help", text: t("analytics.chart.hover_hint") }),
    ]);
  }

  function paintBreakdown() {
    if (!data) return;
    const rows = rowsForDimension();
    const totalTokens = data.summary.total_tokens || 0;
    fill(breakdownBody, table({
      columns: [
        { label: dimension === "key" ? t("analytics.breakdown.col.key") : dimension === "account" ? t("analytics.breakdown.col.account") : t("analytics.breakdown.col.model") },
        ...(dimension === "account" ? [{ label: t("analytics.breakdown.col.state") }] : []),
        { label: t("analytics.breakdown.col.requests"), align: "right" }, { label: t("analytics.breakdown.col.success"), align: "right" },
        { label: t("analytics.breakdown.col.tokens"), align: "right" }, { label: t("analytics.breakdown.col.share"), align: "right" },
        { label: t("analytics.breakdown.col.latency"), align: "right" }, { label: t("analytics.breakdown.col.last_seen"), align: "right" },
      ],
      rows,
      empty: { title: query ? t("analytics.breakdown.no_match") : t("analytics.breakdown.no_traffic"),
        text: query ? t("analytics.breakdown.try_filter") : t("analytics.breakdown.requests_appear") },
      render: r => [
        el("div", {}, [el("div", { text: rowName(r) }),
          dimension === "account" && r.email ? el("div", { class: "mono faint text-xs", text: r.account_id }) : null]),
        ...(dimension === "account" ? [el("div", {}, [stateBadge(r.state || "not in pool"),
          el("div", { class: "field__help", text: r.expires_in_s == null ? t("analytics.breakdown.expiry.unknown")
            : r.expires_in_s > 1e8 ? t("analytics.breakdown.expiry.none") : t("analytics.breakdown.expiry.in", { time: duration(r.expires_in_s) }) })])] : []),
        num(r.requests),
        el("span", { class: r.requests > r.ok ? "text-warn" : "text-ok",
          text: r.requests ? ((r.ok / r.requests) * 100).toFixed(1) + "%" : "N/A",
          title: num(r.ok) + " " + t("analytics.breakdown.succeeded") + "; " + num(r.requests - r.ok) + " " + t("analytics.breakdown.failed_count") }),
        el("span", { class: "mono", text: compactNumber(r.tokens), title: num(r.tokens)
          + " " + t("analytics.breakdown.tokens_total") + " | " + num(r.prompt_tokens) + " " + t("analytics.breakdown.tokens_input") + " | " + num(r.completion_tokens) + " " + t("analytics.breakdown.tokens_output") }),
        totalTokens ? ((r.tokens / totalTokens) * 100).toFixed(1) + "%" : "N/A",
        seconds(r.avg_duration_ms),
        el("span", { class: "faint", text: relative(parseTime(r.last_ts)), title: dateTime(parseTime(r.last_ts)) }),
      ],
    }));
  }

  function paint() {
    const s = data.summary, prev = data.previous, lat = data.latency;
    const success = rate(s), prevRate = rate(prev);
    fill(summaries, [
      metric({ label: t("analytics.summary.requests"), value: num(s.requests), hint: comparison(s.requests, prev?.requests) }),
      metric({ label: t("analytics.summary.tokens"), value: compactNumber(s.total_tokens), hint: comparison(s.total_tokens, prev?.total_tokens) }),
      metric({ label: t("analytics.summary.success"), value: success == null ? t("analytics.summary.no_traffic") : success.toFixed(1) + "%",
        tone: success == null ? null : success >= 95 ? "ok" : "warn",
        hint: success == null ? t("analytics.summary.no_completed") : comparison(success, prevRate, "pp") }),
      metric({ label: t("analytics.summary.latency"), value: s.requests ? seconds(s.avg_duration_ms) : t("analytics.summary.no_samples"),
        hint: s.requests ? comparison(s.avg_duration_ms, prev?.requests ? prev.avg_duration_ms : null) : "" }),
    ]);
    const details = [
      [t("analytics.detail.input"), num(s.prompt_tokens)], [t("analytics.detail.output"), num(s.completion_tokens)],
      [t("analytics.detail.succeeded"), num(s.ok)], [t("analytics.detail.failed"), num(s.requests - s.ok)],
      [t("analytics.detail.rate_limited"), num(s.rate_limited)], [t("analytics.detail.out_of_credit"), num(s.insufficient_credits)],
      [t("analytics.detail.p50"), lat.samples ? seconds(lat.p50) : t("analytics.summary.no_samples")],
      [t("analytics.detail.p95"), lat.samples ? seconds(lat.p95) : t("analytics.summary.no_samples")],
      [t("analytics.detail.p99"), lat.samples ? seconds(lat.p99) : t("analytics.summary.no_samples")],
    ];
    fill(latencyBody, el("dl", { class: "analytics-facts" }, details.map(([label, value]) =>
      el("div", {}, [el("dt", { class: "field__help", text: label }), el("dd", { text: value })]))));
    periodText.textContent = dateTime(data.range.since * 1000) + " — " + dateTime(data.range.until * 1000)
      + (data.previous ? " | " + t("analytics.period.compared") : " | " + t("analytics.period.all_history"));
    fill(errorsBody, (data.errors || []).length ? table({
      columns: [{ label: t("analytics.errors.col.http") }, { label: t("analytics.errors.col.code") }, { label: t("analytics.errors.col.count"), align: "right" }, { label: t("analytics.errors.col.last_seen"), align: "right" }],
      rows: data.errors,
      render: r => [String(r.status), el("span", { class: "mono", text: r.code || t("analytics.errors.unspecified") }),
        num(r.count), relative(parseTime(r.last_ts))],
    }) : el("p", { class: "analytics-no-errors text-ok", text: t("analytics.errors.none") }));
    paintChart();
    paintBreakdown();
    status.textContent = t("analytics.updated", { time: new Date().toLocaleTimeString() });
    exportButton.disabled = false;
    trafficExport.disabled = false;
  }

  async function load() {
    const id = ++generation;
    requestController?.abort();
    requestController = new AbortController();
    status.textContent = t("analytics.updating");
    exportButton.disabled = true;
    trafficExport.disabled = true;
    error.hidden = true;
    try {
      const next = await api.get("/admin/dash/analytics/overview?range=" + range,
        { signal: requestController.signal });
      if (id !== generation || signal.aborted) return;
      data = next;
      paint();
    } catch (e) {
      if (e.name === "AbortError" || signal.aborted || id !== generation) return;
      error.textContent = t("analytics.error.update", { msg: e.message });
      error.hidden = false;
      status.textContent = data ? t("analytics.error.showing_last") : t("analytics.error.no_data");
    }
  }

  const search = el("input", { class: "input analytics-search", type: "search", placeholder: t("analytics.breakdown.search.ph"),
    "aria-label": t("analytics.breakdown.search.aria"), on: { input: e => { query = e.target.value; paintBreakdown(); } } });
  const sort = el("select", { class: "select analytics-sort", "aria-label": t("analytics.breakdown.sort.aria"),
    on: { change: e => { sortBy = e.target.value; paintBreakdown(); } } },
    [["tokens", t("analytics.breakdown.sort.tokens")], ["requests", t("analytics.breakdown.sort.requests")], ["avg_duration_ms", t("analytics.breakdown.sort.latency")]]
      .map(([value, text]) => el("option", { value, text })));

  fill(root, el("div", { class: "stack analytics-workspace" }, [
    toolbar([picker, el("span", { class: "field__help", text: t("analytics.period.hint") })], status),
    error, periodText, summaries,
    card({ title: t("analytics.traffic.title"), actions: [
      switches([["tokens", t("analytics.traffic.tokens")], ["requests", t("analytics.traffic.requests")], ["errors", t("analytics.traffic.errors")]], chartMetric,
        t("analytics.traffic.aria"), value => { chartMetric = value; paintChart(); }), trafficExport,
    ], body: chartBody }),
    card({ title: t("analytics.detail.title"), desc: t("analytics.detail.desc"), body: latencyBody }),
    card({ title: t("analytics.breakdown.title"), actions: exportButton, body: el("div", { class: "stack" }, [
      toolbar([switches([["model", t("analytics.breakdown.models")], ["account", t("analytics.breakdown.accounts")], ["key", t("analytics.breakdown.keys")]], dimension,
        t("analytics.breakdown.sort.aria"), value => { dimension = value; paintBreakdown(); }), sort], search),
      breakdownBody,
    ]) }),
    card({ title: t("analytics.errors.title"), body: errorsBody, flush: true }),
    el("p", { class: "field__help", text: t("analytics.storage.note") }),
  ]));

  signal.addEventListener("abort", () => requestController?.abort(), { once: true });
  await load();
  return { refresh: load, destroy: () => requestController?.abort() };
}
