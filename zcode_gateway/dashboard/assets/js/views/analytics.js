// One telemetry workspace: all summaries, charts and exports share a period.
import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { card, table, toolbar, metric, button, stateBadge } from "../core/ui.js";
import { barChart, compactNumber } from "../core/chart.js";
import { num, relative, parseTime, dateTime, duration } from "../core/format.js";
import { comparison, downloadCsv } from "./analytics-export.js";

const RANGES = [["1h", "Last hour"], ["6h", "Last 6 hours"], ["24h", "Last 24 hours"],
  ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["all", "All time"]];
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

  const exportButton = button({ label: "Export breakdown", icon: "download", onClick: exportBreakdown, disabled: true });
  const trafficExport = button({ label: "Export traffic", icon: "download", onClick: () => {
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
        title: dateTime(r.ts * 1000) + " | " + num(r.requests) + " requests | " + num(r.tokens)
          + " tokens | " + num(r.errors) + " failed",
      })),
    });
    const peak = Math.max(0, ...rows.map(r => Number(r[chartMetric]) || 0));
    const peakAt = rows.find(r => r[chartMetric] === peak);
    fill(chartBody, [
      el("div", { class: "row row--between analytics-chart-meta" }, [
        el("span", { class: "field__help", text: "Buckets: " + duration(bucket) + " | " + zone }),
        el("span", { class: "field__help", text: peak ? "Peak: " + num(peak) + " " + chartMetric
          + " at " + dateTime(peakAt.ts * 1000) : "No traffic in this period" }),
      ]),
      plot,
      el("p", { class: "field__help", text: "Hover a bar for exact values. Amber/red marks buckets with errors. Zero traffic has no bar." }),
    ]);
  }

  function paintBreakdown() {
    if (!data) return;
    const rows = rowsForDimension();
    const totalTokens = data.summary.total_tokens || 0;
    fill(breakdownBody, table({
      columns: [
        { label: dimension === "key" ? "API key" : dimension === "account" ? "Account" : "Model" },
        ...(dimension === "account" ? [{ label: "State / expiry" }] : []),
        { label: "Requests", align: "right" }, { label: "Success", align: "right" },
        { label: "Tokens", align: "right" }, { label: "Share", align: "right" },
        { label: "Avg latency", align: "right" }, { label: "Last seen", align: "right" },
      ],
      rows,
      empty: { title: query ? "No matches" : "No traffic in this period",
        text: query ? "Try a different filter." : "Requests through the gateway will appear here." },
      render: r => [
        el("div", {}, [el("div", { text: rowName(r) }),
          dimension === "account" && r.email ? el("div", { class: "mono faint text-xs", text: r.account_id }) : null]),
        ...(dimension === "account" ? [el("div", {}, [stateBadge(r.state || "not in pool"),
          el("div", { class: "field__help", text: r.expires_in_s == null ? "Expiry unknown"
            : r.expires_in_s > 1e8 ? "No token expiry" : "Token expires in " + duration(r.expires_in_s) })])] : []),
        num(r.requests),
        el("span", { class: r.requests > r.ok ? "text-warn" : "text-ok",
          text: r.requests ? ((r.ok / r.requests) * 100).toFixed(1) + "%" : "N/A",
          title: num(r.ok) + " succeeded; " + num(r.requests - r.ok) + " failed" }),
        el("span", { class: "mono", text: compactNumber(r.tokens), title: num(r.tokens)
          + " total | " + num(r.prompt_tokens) + " input | " + num(r.completion_tokens) + " output" }),
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
      metric({ label: "Requests", value: num(s.requests), hint: comparison(s.requests, prev?.requests) }),
      metric({ label: "Tokens", value: compactNumber(s.total_tokens), hint: comparison(s.total_tokens, prev?.total_tokens) }),
      metric({ label: "Success rate", value: success == null ? "No traffic" : success.toFixed(1) + "%",
        tone: success == null ? null : success >= 95 ? "ok" : "warn",
        hint: success == null ? "No completed requests" : comparison(success, prevRate, "pp") }),
      metric({ label: "Average latency", value: s.requests ? seconds(s.avg_duration_ms) : "No samples",
        hint: s.requests ? comparison(s.avg_duration_ms, prev?.requests ? prev.avg_duration_ms : null) : "" }),
    ]);
    const details = [
      ["Input tokens", num(s.prompt_tokens)], ["Output tokens", num(s.completion_tokens)],
      ["Succeeded", num(s.ok)], ["Failed", num(s.requests - s.ok)],
      ["Rate limited (429)", num(s.rate_limited)], ["Out of credit (402)", num(s.insufficient_credits)],
      ["Latency p50", lat.samples ? seconds(lat.p50) : "No samples"],
      ["Latency p95", lat.samples ? seconds(lat.p95) : "No samples"],
      ["Latency p99", lat.samples ? seconds(lat.p99) : "No samples"],
    ];
    fill(latencyBody, el("dl", { class: "analytics-facts" }, details.map(([label, value]) =>
      el("div", {}, [el("dt", { class: "field__help", text: label }), el("dd", { text: value })]))));
    periodText.textContent = dateTime(data.range.since * 1000) + " to " + dateTime(data.range.until * 1000)
      + (data.previous ? " | Compared with preceding equal-length period." : " | All stored history; no preceding-period comparison.");
    fill(errorsBody, (data.errors || []).length ? table({
      columns: [{ label: "HTTP" }, { label: "Error code" }, { label: "Count", align: "right" }, { label: "Last seen", align: "right" }],
      rows: data.errors,
      render: r => [String(r.status), el("span", { class: "mono", text: r.code || "Unspecified" }),
        num(r.count), relative(parseTime(r.last_ts))],
    }) : el("p", { class: "analytics-no-errors text-ok", text: "No errors in this period." }));
    paintChart();
    paintBreakdown();
    status.textContent = "Updated " + new Date().toLocaleTimeString();
    exportButton.disabled = false;
    trafficExport.disabled = false;
  }

  async function load() {
    const id = ++generation;
    requestController?.abort();
    requestController = new AbortController();
    status.textContent = "Updating...";
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
      error.textContent = "Could not update analytics: " + e.message + ". Use Refresh to retry.";
      error.hidden = false;
      status.textContent = data ? "Showing last successful result" : "No data loaded";
    }
  }

  const search = el("input", { class: "input analytics-search", type: "search", placeholder: "Filter breakdown...",
    "aria-label": "Filter breakdown", on: { input: e => { query = e.target.value; paintBreakdown(); } } });
  const sort = el("select", { class: "select analytics-sort", "aria-label": "Sort breakdown",
    on: { change: e => { sortBy = e.target.value; paintBreakdown(); } } },
    [["tokens", "Most tokens"], ["requests", "Most requests"], ["avg_duration_ms", "Highest latency"]]
      .map(([value, text]) => el("option", { value, text })));

  fill(root, el("div", { class: "stack analytics-workspace" }, [
    toolbar([picker, el("span", { class: "field__help", text: "All figures use the selected period." })], status),
    error, periodText, summaries,
    card({ title: "Traffic", actions: [
      switches([["tokens", "Tokens"], ["requests", "Requests"], ["errors", "Errors"]], chartMetric,
        "Traffic metric", value => { chartMetric = value; paintChart(); }), trafficExport,
    ], body: chartBody }),
    card({ title: "Token and response detail", desc: "Percentiles use successful attempts; averages include failures. Recorded duration is not time to first token or end-to-end client latency.", body: latencyBody }),
    card({ title: "Breakdown", actions: exportButton, body: el("div", { class: "stack" }, [
      toolbar([switches([["model", "Models"], ["account", "Accounts"], ["key", "API keys"]], dimension,
        "Breakdown dimension", value => { dimension = value; paintBreakdown(); }), sort], search),
      breakdownBody,
    ]) }),
    card({ title: "Errors", body: errorsBody, flush: true }),
    el("p", { class: "field__help", text: "Stored locally in gateway.db. Counts are recorded upstream attempts; retries can create multiple rows for one client request." }),
  ]));

  signal.addEventListener("abort", () => requestController?.abort(), { once: true });
  await load();
  return { refresh: load, destroy: () => requestController?.abort() };
}
