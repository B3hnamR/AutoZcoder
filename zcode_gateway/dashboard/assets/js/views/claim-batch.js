import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { button } from "../core/ui.js";
import { toast, confirmDialog } from "../core/toast.js";

export function createClaimBatch({ signal, compact = false } = {}) {
  let busy = false;
  const mode = el("select", { class: "select", "aria-label": "Batch claim connection",
    style: { maxWidth: "200px" } }, [
    el("option", { value: "direct", text: "Direct (no proxy)", selected: true }),
    el("option", { value: "proxy", text: "Proxy pool, then direct" }),
  ]);
  const status = el("span", { class: "field__help", role: "status", "aria-live": "polite" });
  const start = button({ label: "Claim all pending", icon: "bolt", onClick: async () => {
    if (busy) return;
    const selected = mode.value;
    const ok = await confirmDialog({ title: "Claim Start Plan for pending signups?",
      body: "Only locally saved successful signups without a recorded grant are queued. "
        + "They run one at a time. " + (selected === "direct"
          ? "Direct skips proxy-pool selection; your system VPN/TUN still applies."
          : "Proxy mode can spend time checking the pool, then fall back to direct.")
        + " No new accounts are created. Stop batch cancels the queue and its current claim.", okLabel: "Start batch" });
    if (!ok || signal?.aborted) return;
    busy = true; start.disabled = true;
    try {
      const result = await api.post("/admin/claims/claim_all_pending", { mode: selected }, { signal });
      if (result.error) throw new Error(result.error);
      toast("Batch started. Watch progress in Generator.", "ok");
    } catch (e) { if (e.name !== "AbortError") toast(e.message, "err"); }
    finally { busy = false; await refresh(); }
  } });
  const stop = button({ label: "Stop batch", variant: "danger", onClick: async () => {
    stop.disabled = true;
    try { paint(await api.post("/admin/claims/claim_all_stop", {}, { signal })); }
    catch (e) { if (e.name !== "AbortError") toast(e.message, "err"); }
    finally { stop.disabled = false; }
  } });
  stop.hidden = true;
  function paint(data) {
    const active = !!data.running;
    start.disabled = active || busy;
    mode.disabled = active || busy;
    stop.hidden = !active;
    stop.disabled = data.state === "stopping";
    const counts = (data.completed || 0) + "/" + (data.total || 0) + " processed | "
      + (data.succeeded || 0) + " granted | " + (data.failed || 0) + " not granted | " + (data.skipped || 0) + " skipped";
    status.textContent = data.state === "idle" || !data.state ? ""
      : (data.state === "stopping" ? "Stopping batch... " : active ? "Batch running: " : "Batch " + data.state + ": ") + counts
        + (data.current_email ? " | " + data.current_email : "") + (data.error ? " | " + data.error : "");
  }
  async function refresh() {
    try { paint(await api.get("/admin/claims/claim_all_status", { signal })); }
    catch (e) { if (!signal?.aborted) status.textContent = "Batch status unavailable; refresh to retry."; }
  }
  const node = el("div", { class: "row row--tight" }, compact ? [mode, start, stop, status] : [
    el("strong", { text: "Pending Start Plan claims" }), mode, start, stop, status,
  ]);
  refresh();
  return { node, refresh };
}
