// Edge relays: Cloudflare / Vercel / Deno / Netlify workers that carry GLM
// traffic, because ZCode answers 405 to direct residential IPs.

import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { toast, confirmDialog } from "../../core/toast.js";
import { card, table, button, badge, stateBadge } from "../../core/ui.js";
import { relative, parseTime } from "../../core/format.js";

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
    title: "Edge relays",
    desc: "Cloudflare, Vercel, Deno or Netlify. Required for GLM-5.3 from most IPs.",
    actions: statusNote,
    body: el("div", { class: "stack" }, [
      el("div", { class: "notice notice--warn" }, [
        el("b", { text: "Why this exists: " }),
        el("span", { text:
          "ZCode blocks GLM-5.3 from ordinary residential and datacenter IPs with HTTP 405 "
          + "(“unusual activity”). Routing through an edge relay avoids that." }),
      ]),
      controlsBox,
      el("div", { class: "row" }, [
        ...PROVIDERS.map((p) => button({
          label: "Deploy " + p.label, onClick: () => openDeploy(p.id),
        })),
        button({ label: "Add existing URL", variant: "quiet", onClick: () => openDeploy("custom") }),
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
        controls.glm53.checked ? "glm-5.3" : null,
        controls.flash.checked ? "glm-5.3-flash" : null,
      ].filter(Boolean),
    };
    try {
      await api.post("/admin/dash/relays/settings", body);
      statusNote.textContent = "Saved";
      setTimeout(() => { statusNote.textContent = ""; }, 1500);
    } catch (e) { toast("relay settings: " + e.message, "err"); }
  }

  const controls = {
    enabled: el("input", { class: "switch__input", type: "checkbox", on: { change: saveSettings } }),
    glm53: el("input", { type: "checkbox", on: { change: saveSettings } }),
    flash: el("input", { type: "checkbox", on: { change: saveSettings } }),
    strategy: el("select", { class: "select", style: { maxWidth: "190px" }, on: { change: saveSettings } }, [
      el("option", { value: "single", text: "Single (active relay only)" }),
      el("option", { value: "round_robin", text: "Round robin" }),
      el("option", { value: "random", text: "Random" }),
    ]),
  };

  fill(controlsBox, [
    el("label", { class: "switch" }, [
      controls.enabled,
      el("span", { class: "switch__track" }),
      el("span", { class: "text-sm", text: "Enable edge relay" }),
    ]),
    el("div", { class: "relay-controls__sep" }),
    el("span", { class: "text-sm muted", text: "Route models:" }),
    el("label", { class: "checkline" }, [controls.glm53, el("span", { text: "GLM-5.3" }), badge("recommended", "ok")]),
    el("label", { class: "checkline" }, [controls.flash, el("span", { text: "GLM-5.3-Flash" })]),
    el("div", { class: "relay-controls__sep" }),
    el("span", { class: "text-sm muted", text: "Rotation:" }),
    controls.strategy,
    el("span", { class: "faint text-xs", text: "A failed relay is skipped for 60s." }),
  ]);

  /* ---------------------------------------------------------- actions --- */

  async function setActive(id) {
    try { await api.post("/admin/dash/relays/settings", { active_id: id }); toast("Active relay set", "ok"); refresh(); }
    catch (e) { toast(e.message, "err"); }
  }

  async function testRelay(id, btn) {
    const label = btn.querySelector("span:last-child");
    const original = label.textContent;
    btn.disabled = true;
    label.textContent = "Testing…";
    try {
      const r = await api.post("/admin/dash/relays/test", { id });
      toast(r.ok ? "Relay answered in " + Math.round(r.elapsed_ms || 0) + " ms"
                 : (r.error || "Relay did not answer"), r.ok ? "ok" : "err");
      refresh();
    } catch (e) { toast("test: " + e.message, "err"); }
    finally { btn.disabled = false; label.textContent = original; }
  }

  async function deleteRelay(relay) {
    const ok = await confirmDialog({
      title: "Delete this relay?",
      body: (relay.name || relay.id) + " is removed from the gateway.\n"
          + "The deployed worker itself is not deleted from the provider.",
      okLabel: "Delete", danger: true,
    });
    if (!ok) return;
    try { await api.del("/admin/dash/relays/" + encodeURIComponent(relay.id)); toast("Relay deleted", "ok"); refresh(); }
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
    const submit = button({ label: isCustom ? "Add relay" : "Deploy", variant: "primary" });

    const modal = el("div", { class: "modal", role: "dialog", "aria-modal": "true", style: { maxWidth: "520px" } }, [
      el("div", { class: "modal__head" }, el("div", {
        class: "modal__title",
        text: isCustom ? "Add an existing relay" : "Deploy a " + provider + " relay",
      })),
      el("div", { class: "modal__body stack" }, [
        el("div", { class: "field" }, [
          el("label", { class: "field__label", text: "Name" }), fields.name,
        ]),
        isCustom
          ? el("div", { class: "stack" }, [
              el("div", { class: "field" }, [el("label", { class: "field__label", text: "Relay URL" }), fields.url]),
              el("div", { class: "field" }, [el("label", { class: "field__label", text: "Relay secret (optional)" }), fields.secret]),
            ])
          : el("div", { class: "stack" }, [
              el("div", { class: "field" }, [
                el("label", { class: "field__label", text: "API token" }), fields.token,
                el("p", { class: "field__help", text: "Used once to deploy. Stored only if the provider needs it again." }),
              ]),
              el("div", { class: "field" }, [
                el("label", { class: "field__label", text: provider === "deno" ? "Organization domain" : "Account ID" }), fields.account,
              ]),
            ]),
        errorBox, logBox,
      ]),
      el("div", { class: "modal__foot" }, [
        button({ label: "Cancel", variant: "ghost", onClick: () => overlay.remove() }),
        submit,
      ]),
    ]);
    const overlay = el("div", { class: "overlay" }, modal);
    if (provider === "deno") fields.account.placeholder = "Your Deno organization domain";
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });

    submit.addEventListener("click", async () => {
      submit.disabled = true;
      submit.querySelector("span").textContent = isCustom ? "Adding…" : "Deploying…";
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
        if (r.ok === false) throw new Error(r.error || "Deployment failed");
        toast(isCustom ? "Relay added" : "Relay deployed", "ok");
        overlay.remove();
        refresh();
      } catch (e) {
        errorBox.textContent = e.message;
        errorBox.hidden = false;
      } finally {
        submit.disabled = false;
        submit.querySelector("span").textContent = isCustom ? "Add relay" : "Deploy";
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
    controls.glm53.checked = models.some((m) => /5\.3$/i.test(m));
    controls.flash.checked = models.some((m) => /flash/i.test(m));

    fill(listBox, table({
      columns: [
        { label: "Relay" }, { label: "Provider" }, { label: "Status" },
        { label: "Latency", align: "right" }, { label: "Checked", align: "right" }, { label: "", width: "150px" },
      ],
      rows: relays,
      empty: {
        title: "No relays yet",
        text: "Deploy one above. GLM-5.3 needs a relay on most connections.",
      },
      render: (r) => {
        const isActive = s.active_id === r.id;
        const testBtn = button({ label: "Test" });
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
            isActive ? null : button({ label: "Use", onClick: () => setActive(r.id) }),
            testBtn,
            button({ icon: "eyeOff", title: "Delete", variant: "danger", onClick: () => deleteRelay(r) }),
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
