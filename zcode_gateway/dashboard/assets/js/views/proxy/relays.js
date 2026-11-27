// Edge relays: Cloudflare / Vercel / Deno / Netlify workers that carry GLM
// traffic, because ZCode answers 405 to direct residential IPs.

import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { toast, confirmDialog } from "../../core/toast.js";
import { card, table, button, badge, stateBadge } from "../../core/ui.js";
import { relative, parseTime } from "../../core/format.js";
import { t } from "../../core/i18n.js";

const PROVIDERS = [
  { id: "cloudflare", label: "Cloudflare" },
  { id: "vercel", label: "Vercel" },
  { id: "deno", label: "Deno" },
  { id: "netlify", label: "Netlify" },
];

export function createRelaysCard() {
  let data = { relays: [], settings: {} };
  const selected = new Set();

  const listBox = el("div", {});
  const controlsBox = el("div", { class: "relay-controls" });
  const statusNote = el("span", { class: "muted text-xs" });

  const node = card({
    title: t("relay.title"),
    desc: t("relay.desc"),
    actions: statusNote,
    body: el("div", { class: "stack" }, [
      controlsBox,
      el("div", { class: "row" }, [
        ...PROVIDERS.map((p) => button({
          label: t("relay.deploy", { provider: p.label }), onClick: () => openDeploy(p.id),
        })),
        button({ label: t("relay.add_url"), variant: "quiet", onClick: () => openDeploy("custom") }),
        el("span", { class: "spacer" }),
        button({ icon: "refresh", title: "Refresh", variant: "quiet", onClick: () => refresh() }),
      ]),
      listBox,
    ]),
  });

  /* --------------------------------------------------------- settings --- */

  async function saveSettings() {
    const body = {
      enabled: controls.enabled.checked,
      strategy: controls.strategy.value,
      models: [
        controls.glm54.checked ? "glm-5.4" : null,
        controls.flash.checked ? "glm-5.3-flash" : null,
      ].filter(Boolean),
    };
    try {
      await api.post("/admin/dash/relays/settings", body);
      statusNote.textContent = t("relay.saved");
      setTimeout(() => { statusNote.textContent = ""; }, 1500);
    } catch (e) { toast(e.message, "err"); }
  }

  const controls = {
    enabled: el("input", { class: "switch__input", type: "checkbox", on: { change: saveSettings } }),
    glm54: el("input", { type: "checkbox", on: { change: saveSettings } }),
    flash: el("input", { type: "checkbox", on: { change: saveSettings } }),
    strategy: el("select", { class: "select", style: { maxWidth: "190px" }, on: { change: saveSettings } }, [
      el("option", { value: "single", text: t("relay.strategy.single") }),
      el("option", { value: "round_robin", text: t("relay.strategy.round_robin") }),
      el("option", { value: "random", text: t("relay.strategy.random") }),
    ]),
  };

  fill(controlsBox, [
    el("label", { class: "switch" }, [
      controls.enabled,
      el("span", { class: "switch__track" }),
      el("span", { class: "text-sm", text: t("relay.enable") }),
    ]),
    el("div", { class: "relay-controls__sep" }),
    el("span", { class: "text-sm muted", text: t("relay.route_models") }),
    el("label", { class: "checkline" }, [controls.glm54, el("span", { text: t("relay.glm54") }), badge(t("relay.glm54.badge"), "ok")]),
    el("label", { class: "checkline" }, [controls.flash, el("span", { text: t("relay.glm53f") })]),
    el("div", { class: "relay-controls__sep" }),
    el("span", { class: "text-sm muted", text: t("relay.rotation") }),
    controls.strategy,
    el("span", { class: "faint text-xs", text: t("relay.strategy.skip") }),
  ]);

  /* ---------------------------------------------------------- actions --- */

  async function setActive(id) {
    try { await api.post("/admin/dash/relays/settings", { active_id: id }); toast(t("relay.active_set"), "ok"); refresh(); }
    catch (e) { toast(e.message, "err"); }
  }

  async function testRelay(id, btn) {
    const label = btn.querySelector("span:last-child");
    const original = label.textContent;
    btn.disabled = true;
    label.textContent = t("relay.testing");
    try {
      const r = await api.post("/admin/dash/relays/test", { id });
      toast(r.ok ? t("relay.test_ok", { n: Math.round(r.elapsed_ms || 0) })
                 : (r.error || t("relay.test_fail")), r.ok ? "ok" : "err");
      refresh();
    } catch (e) { toast(e.message, "err"); }
    finally { btn.disabled = false; label.textContent = original; }
  }

  async function deleteRelay(relay) {
    const ok = await confirmDialog({
      title: t("relay.delete.title"),
      body: t("relay.delete.body", { name: relay.name || relay.id }),
      okLabel: t("relay.delete.ok"), danger: true,
    });
    if (!ok) return;
    try { await api.del("/admin/dash/relays/" + encodeURIComponent(relay.id)); toast(t("relay.deleted"), "ok"); refresh(); }
    catch (e) { toast(e.message, "err"); }
  }

  /* ----------------------------------------------------- deploy modal --- */

  function openDeploy(provider) {
    const isCustom = provider === "custom";
    const fields = {
      name: el("input", { class: "input", type: "text", placeholder: "My relay" }),
      url: el("input", { class: "input input--mono", type: "text", placeholder: "https://relay.example.workers.dev" }),
      token: el("input", { class: "input", type: "password", autocomplete: "off", placeholder: "API token" }),
      account: el("input", { class: "input input--mono", type: "text", placeholder: "Account ID (optional)" }),
      secret: el("input", { class: "input", type: "password", autocomplete: "off", placeholder: "Relay secret (optional)" }),
    };
    const logBox = el("pre", { class: "code", hidden: true });
    const errorBox = el("p", { class: "field__error", hidden: true });
    const submit = button({ label: isCustom ? t("relay.add_btn") : t("relay.deploy_btn"), variant: "primary" });

    const modal = el("div", { class: "modal", role: "dialog", "aria-modal": "true", style: { maxWidth: "520px" } }, [
      el("div", { class: "modal__head" }, el("div", {
        class: "modal__title",
        text: isCustom ? t("relay.add_existing") : t("relay.deploy_provider", { provider }),
      })),
      el("div", { class: "modal__body stack" }, [
        el("div", { class: "field" }, [
          el("label", { class: "field__label", text: t("relay.name") }), fields.name,
        ]),
        isCustom
          ? el("div", { class: "stack" }, [
              el("div", { class: "field" }, [el("label", { class: "field__label", text: t("relay.url") }), fields.url]),
              el("div", { class: "field" }, [el("label", { class: "field__label", text: t("relay.secret") }), fields.secret]),
            ])
          : el("div", { class: "stack" }, [
              el("div", { class: "field" }, [
                el("label", { class: "field__label", text: t("relay.token") }), fields.token,
                el("p", { class: "field__help", text: t("relay.token.help") }),
              ]),
              el("div", { class: "field" }, [
                el("label", { class: "field__label", text: provider === "deno" ? t("relay.account.denolabel") : t("relay.account") }), fields.account,
              ]),
            ]),
        errorBox, logBox,
      ]),
      el("div", { class: "modal__foot" }, [
        button({ label: t("relay.cancel"), variant: "ghost", onClick: () => overlay.remove() }),
        submit,
      ]),
    ]);
    const overlay = el("div", { class: "overlay" }, modal);
    if (provider === "deno") fields.account.placeholder = "Your Deno organization domain";
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });

    submit.addEventListener("click", async () => {
      submit.disabled = true;
      submit.querySelector("span").textContent = isCustom ? t("relay.adding") : t("relay.deploying");
      errorBox.hidden = true;
      try {
        const r = isCustom
          ? await api.post("/admin/dash/relays/add", {
              name: fields.name.value.trim(), url: fields.url.value.trim(),
              secret: fields.secret.value.trim(),
            })
          : await api.post("/admin/dash/relays/deploy", {
              type: provider, projectName: fields.name.value.trim(),
              ...(provider === "cloudflare"
                ? { api_token: fields.token.value.trim(), account_id: fields.account.value.trim() }
                : { token: fields.token.value.trim() }),
              ...(provider === "deno" ? { org_domain: fields.account.value.trim() } : {}),
            });
        if (r.steps?.length) { logBox.textContent = r.steps.join("\n"); logBox.hidden = false; }
        if (r.ok === false) throw new Error(r.error || t("relay.deploy_failed"));
        toast(isCustom ? t("relay.added") : t("relay.deployed"), "ok");
        overlay.remove();
        refresh();
      } catch (e) {
        errorBox.textContent = e.message;
        errorBox.hidden = false;
      } finally {
        submit.disabled = false;
        submit.querySelector("span").textContent = isCustom ? t("relay.add_btn") : t("relay.deploy_btn");
      }
    });

    document.body.append(overlay);
    fields.name.focus();
  }

  /* ------------------------------------------------------------ paint --- */

  function paint() {
    const relays = data.relays || [];
    const s = data;

    controls.enabled.checked = !!s.enabled;
    controls.strategy.value = s.strategy || "single";
    const models = s.models || [];
    controls.glm54.checked = models.some((m) => /5\.4$/i.test(m));
    controls.flash.checked = models.some((m) => /flash/i.test(m));

    fill(listBox, table({
      columns: [
        { label: t("relay.col.relay") }, { label: t("relay.col.provider") }, { label: t("relay.col.status") },
        { label: t("relay.col.latency"), align: "right" }, { label: t("relay.col.checked"), align: "right" }, { label: "", width: "150px" },
      ],
      rows: relays,
      empty: {
        title: t("relay.empty.title"),
        text: t("relay.empty.body"),
      },
      render: (r) => {
        const isActive = s.active_id === r.id;
        const testBtn = button({ label: t("relay.test") });
        testBtn.addEventListener("click", () => testRelay(r.id, testBtn));
        return [
          el("div", {}, [
            el("div", { class: "row row--tight" }, [
              el("span", { text: r.name || r.id }),
              isActive ? badge("active", "accent") : null,
            ]),
            r.url ? el("div", { class: "mono faint text-xs truncate", text: r.url, title: r.url }) : null,
          ]),
          el("span", { class: "badge", text: r.type || "custom" }),
          el("span", { title: r.last_error || "" }, stateBadge(
            r.last_status >= 200 && r.last_status < 300 ? "ok" : r.last_status ? "failed" : "unknown")),
          r.last_latency_ms != null ? Math.round(r.last_latency_ms) + " ms" : "—",
          el("span", { class: "faint", text: r.last_tested_at ? relative(parseTime(r.last_tested_at)) : "—" }),
          el("div", { class: "row row--tight", style: { justifyContent: "flex-end" } }, [
            isActive ? null : button({ label: t("relay.use"), onClick: () => setActive(r.id) }),
            testBtn,
            button({ icon: "eyeOff", title: t("relay.delete.title"), variant: "danger", onClick: () => deleteRelay(r) }),
          ]),
        ];
      },
    }));
  }

  async function refresh() {
    try {
      data = await api.get("/admin/dash/relays");
      paint();
    } catch (e) {
      fill(listBox, el("p", { class: "field__error", text: e.message }));
    }
  }

  return { node, refresh };
}
