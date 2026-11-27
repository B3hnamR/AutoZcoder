import { el, fill } from "../core/dom.js";
import { api } from "../core/api.js";
import { button } from "../core/ui.js";
import { toast, confirmDialog } from "../core/toast.js";
import { t } from "../core/i18n.js";

export function createClaimBatch({ signal, compact = false } = {}) {
  let busy = false;
  const mode = el("select", { class: "select", "aria-label": t("claim.batch.aria"),
    style: { maxWidth: "200px" } }, [
    el("option", { value: "direct", text: t("claim.batch.direct"), selected: true }),
    el("option", { value: "proxy", text: t("claim.batch.proxy") }),
  ]);
  const status = el("span", { class: "field__help", role: "status", "aria-live": "polite" });
  const start = button({ label: t("claim.batch.start"), icon: "bolt", onClick: async () => {
    if (busy) return;
    const selected = mode.value;
    const ok = await confirmDialog({ title: t("claim.batch.confirm.title"),
      body: t("claim.batch.confirm.body") + (selected === "direct"
          ? t("claim.batch.confirm.direct")
          : t("claim.batch.confirm.proxy")),
      okLabel: t("claim.batch.confirm.ok") });
    if (!ok || signal?.aborted) return;
    busy = true; start.disabled = true;
    try {
      const result = await api.post("/admin/claims/claim_all_pending", { mode: selected }, { signal });
      if (result.error) throw new Error(result.error);
      toast(t("claim.batch.toast.started"), "ok");
    } catch (e) { if (e.name !== "AbortError") toast(e.message, "err"); }
    finally { busy = false; await refresh(); }
  } });
  const stop = button({ label: t("claim.batch.stop"), variant: "danger", onClick: async () => {
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
    const counts = t("claim.batch.counts", {
      completed: data.completed || 0, total: data.total || 0,
      succeeded: data.succeeded || 0, failed: data.failed || 0, skipped: data.skipped || 0,
    });
    status.textContent = data.state === "idle" || !data.state ? ""
      : (data.state === "stopping" ? t("claim.batch.stopping") + " " : active ? t("claim.batch.running") : t("claim.batch.state", { state: data.state }) + " ") + counts
        + (data.current_email ? t("claim.batch.current", { email: data.current_email }) : "")
        + (data.error ? t("claim.batch.error", { error: data.error }) : "");
  }
  async function refresh() {
    try { paint(await api.get("/admin/claims/claim_all_status", { signal })); }
    catch (e) { if (!signal?.aborted) status.textContent = t("claim.batch.unavailable"); }
  }
  // Campaign cards (Global/Weekend Build) are upstream drops, so the window is
  // read from the delivery, not from a clock. The one stable rule is the name:
  // Weekend Build is a Beijing-weekend campaign, and Beijing is 4:30 ahead of
  // Tehran all year (Iran dropped DST in 2022).
  const hint = el("p", { class: "field__help", text:
    t("claim.batch.hint") });
  const node = compact ? el("div", { class: "row row--tight" }, [mode, start, stop, status])
    : el("div", { class: "stack--tight" }, [
      el("div", { class: "row row--tight" }, [
        el("strong", { text: t("claim.batch.title") }), mode, start, stop, status,
      ]),
      hint,
    ]);
  refresh();
  return { node, refresh };
}
