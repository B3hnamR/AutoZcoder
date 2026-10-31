// Email setup: Cloudflare worker + KV + secret, Email Routing, inbox check.
//
// The routing step can come back needing confirmation when a domain already
// has a non-placeholder catch-all: replacing it silently would divert the
// user's real mail, so it is always an explicit choice.

import { el, fill } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { toast } from "../../core/toast.js";
import { card, button } from "../../core/ui.js";

const CF_PERMISSIONS = [
  ["Account", "Workers Scripts", "Edit"],
  ["Account", "Workers KV Storage", "Edit"],
  ["Account", "Account Settings", "Read"],
  ["Zone", "Zone", "Read"],
  ["Zone", "DNS", "Edit"],
  ["Zone", "Email Routing Rules", "Edit"],
  ["Zone", "Zone Settings", "Edit"],
];

export function createMailCard() {
  const state = {
    deploying: false,
    message: "",
    log: "",
    inboxMessage: "",
    review: new Map(),        // domain -> {existing_rule, expected_rule, worker}
    errors: new Map(),
    deferred: new Set(),
    advancedOpen: false,
  };

  const body = el("div", { class: "stack" });
  const node = card({
    title: "Email setup",
    desc: "Enter a domain already on Cloudflare and an API token. Setup creates the worker, storage and secret, configures routing, and verifies the inbox.",
    body,
  });

  /* ------------------------------------------------------------ parts --- */

  function tokenHelp() {
    const table = el("table", { class: "table table--plain" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { text: "Scope" }), el("th", { text: "Permission" }), el("th", { text: "Access" }),
      ])),
      el("tbody", {}, CF_PERMISSIONS.map(([scope, perm, access]) => el("tr", {}, [
        el("td", { text: scope }), el("td", { text: perm }), el("td", { text: access }),
      ]))),
    ]);

    return el("details", { class: "help-details" }, [
      el("summary", { text: "How to create a Cloudflare token" }),
      el("ol", { class: "help-list" }, [
        el("li", {}, [
          el("span", { text: "Open " }),
          el("a", { href: "https://dash.cloudflare.com/profile/api-tokens", target: "_blank", rel: "noopener",
                    text: "My Profile > API Tokens" }),
          el("span", { text: ", choose Create Token > Create Custom Token > Get started." }),
        ]),
        el("li", {}, [el("span", { text: "Name it anything, then add these permissions:" }),
                      el("div", { class: "table-wrap" }, table)]),
        el("li", { text: "Account Resources: include your account. Zone Resources: include the mail zones, or All zones." }),
        el("li", { text: "Leave IP filtering empty unless you need it. Renew the saved token before it expires." }),
        el("li", { text: "Choose Continue to summary > Create Token, then paste it above. It is shown only once." }),
      ]),
    ]);
  }

  function routingRow(domain, m) {
    const done = (m.routing_done || []).includes(domain);
    const review = state.review.get(domain);
    const error = state.errors.get(domain);
    const deferred = state.deferred.has(domain);

    const statusText = review ? "Confirmation needed" : done ? "Routing configured" : "Routing pending";
    const statusClass = review ? "text-warn" : done ? "text-ok" : "muted";

    const header = el("div", { class: "row row--tight" }, [
      el("span", { class: "mono", text: domain }),
      el("span", { class: "text-sm " + statusClass, text: statusText }),
      review && deferred
        ? button({ label: "Review change", disabled: state.deploying,
                   onClick: () => { state.deferred.delete(domain); paint(m); } })
        : null,
    ]);

    if (!review || deferred) {
      return el("div", { class: "stack--tight" }, [
        header,
        error ? el("p", { class: "field__error", role: "alert", text: error }) : null,
      ]);
    }

    const detail = el("div", { class: "routing-review", role: "group", "aria-label": "Routing change for " + domain }, [
      el("div", { class: "kv" }, [
        el("div", { class: "kv__key", text: "Now" }),
        el("div", { class: "kv__val", text: ruleDestination(review.existing_rule) }),
        el("div", { class: "kv__key", text: "After setup" }),
        el("div", { class: "kv__val", text: "Verification inbox (worker " + review.worker + ")" }),
      ]),
      el("p", { class: "text-warn text-sm", text:
        "Your current destination stops receiving unmatched mail on this domain. Named-address rules are unchanged." }),
      el("div", { class: "row" }, [
        button({ label: "Keep current routing", disabled: state.deploying,
                 onClick: () => keepRouting(domain) }),
        button({ label: "Replace routing", variant: "primary", disabled: state.deploying,
                 onClick: () => autoRoute(domain, true) }),
      ]),
      el("details", { class: "help-details" }, [
        el("summary", { text: "Technical details" }),
        el("div", { class: "field__help", text: "Current rule" }),
        el("pre", { class: "code", text: JSON.stringify(review.existing_rule, null, 2) }),
        el("div", { class: "field__help", text: "Proposed rule" }),
        el("pre", { class: "code", text: JSON.stringify(review.expected_rule, null, 2) }),
      ]),
    ]);

    return el("div", { class: "stack--tight" }, [
      header, detail,
      error ? el("p", { class: "field__error", role: "alert", text: error }) : null,
    ]);
  }

  function ruleDestination(rule) {
    const actions = rule?.actions || [];
    const values = actions.flatMap((a) => a.value || []);
    if (!values.length) return "Not forwarded";
    return values.join(", ");
  }

  /* ---------------------------------------------------------- actions --- */

  function rememberRouting(results) {
    for (const result of results || []) {
      const domain = result.domain;
      if (!domain) continue;
      if (result.needs_confirmation) {
        state.review.set(domain, result);
        state.deferred.delete(domain);
      } else if (result.error) {
        state.errors.set(domain, result.error);
      } else if (result.ok) {
        state.review.delete(domain);
        state.errors.delete(domain);
        state.deferred.delete(domain);
      }
    }
  }

  async function deploy(redeploy = false) {
    const domains = fields.domains.value.trim();
    const token = fields.token.value.trim();
    const account = fields.account.value.trim();

    state.deploying = true;
    state.message = "Setting up email…";
    state.log = "";
    state.errors.clear();
    await refresh();

    try {
      const current = await api.get("/admin/dash/generator/mail");
      const added = domains.split(/[,\s]+/).map(d => d.trim()).filter(Boolean);
      if (added.length) {
        await api.post("/admin/dash/generator/mail", {
          domains: [...new Set([...(current.domains || []), ...added])],
        });
        fields.domains.value = "";
      }
      const r = await api.post("/admin/dash/generator/mail/deploy", {
        cf_token: token, cf_account_id: account,
      });
      state.log = (r.steps || []).join("\n");
      rememberRouting(r.routing);
      if (r.ok) {
        toast(r.ready ? "Email setup complete" : "Worker deployed; review routing and inbox readiness",
              r.ready ? "ok" : "warn");
        fields.token.value = "";
        state.message = r.ready ? "" : "Worker deployed; email setup still needs attention.";
      } else {
        state.message = r.error || "Setup did not finish.";
        toast(state.message, "err", 8000);
      }
    } catch (e) {
      state.message = e.message;
      toast("Setup: " + e.message, "err", 8000);
    } finally {
      state.deploying = false;
      await refresh();
    }
  }

  async function autoRoute(domain, replaceExisting = false) {
    state.deploying = true;
    await refresh();
    try {
      const r = await api.post("/admin/dash/generator/mail/routing-auto", {
        domain, replace_existing: replaceExisting,
        ...(replaceExisting ? {
          confirmation_fingerprint: state.review.get(domain)?.confirmation_fingerprint,
        } : {}),
      });
      for (const res of r.results || []) {
        const d = res.domain || domain;
        if (res.needs_confirmation) {
          state.review.set(d, res);
          state.deferred.add(d);
        } else if (res.error) {
          state.errors.set(d, res.error);
        } else {
          state.review.delete(d);
          state.errors.delete(d);
          state.deferred.delete(d);
        }
      }
      toast(r.ok ? "Routing configured" : "Routing needs attention", r.ok ? "ok" : "warn");
    } catch (e) {
      state.errors.set(domain, e.message);
    } finally {
      state.deploying = false;
      await refresh();
    }
  }

  function keepRouting(domain) {
    state.review.delete(domain);
    state.deferred.delete(domain);
    state.errors.set(domain, "Routing left unchanged. Verification mail will not arrive until a catch-all points at the worker.");
    refresh();
  }

  async function saveToken() {
    const token = fields.token.value.trim();
    const account = fields.account.value.trim();
    if (!token && !account) { toast("Enter a token first", "warn"); return; }
    try {
      await api.post("/admin/dash/generator/mail/cf-credentials", { cf_token: token, cf_account_id: account });
      fields.token.value = "";
      toast("Token saved", "ok");
      await refresh();
    } catch (e) { toast("save: " + e.message, "err"); }
  }

  async function clearToken() {
    try {
      await api.post("/admin/dash/generator/mail/cf-credentials", { cf_token: "", clear: true });
      toast("Saved token removed", "ok");
      await refresh();
    } catch (e) { toast("remove: " + e.message, "err"); }
  }

  async function testInbox() {
    state.inboxMessage = "Testing…";
    await refresh();
    try {
      const r = await api.get("/admin/dash/generator/mail/test");
      state.inboxMessage = r.ok ? "Worker inbox answered." : (r.error || "Inbox did not answer.");
      toast(state.inboxMessage, r.ok ? "ok" : "err");
    } catch (e) {
      state.inboxMessage = e.message;
    }
    await refresh();
  }

  async function saveManual() {
    try {
      await api.post("/admin/dash/generator/mail", {
        inbox_url: fields.inboxUrl.value.trim(),
        inbox_secret: fields.inboxSecret.value.trim(),
      });
      fields.inboxSecret.value = "";
      toast("Inbox settings saved", "ok");
      await refresh();
    } catch (e) { toast("save: " + e.message, "err"); }
  }

  async function removeDomain(domain) {
    try {
      const current = await api.get("/admin/dash/generator/mail");
      await api.post("/admin/dash/generator/mail", {
        domains: (current.domains || []).filter(d => d !== domain),
      });
      toast("Domain removed", "ok");
      await refresh();
    } catch (e) { toast("remove: " + e.message, "err"); }
  }

  async function markRoutingDone(domain) {
    try {
      await api.post("/admin/dash/generator/mail/routing", { domain, done: true });
      toast("Marked as routed", "ok");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  /* ------------------------------------------------------------ paint --- */

  // Inputs are created once and never replaced, so a repaint cannot wipe a
  // half-typed domain, token or secret.
  const fields = {
    domains: el("textarea", {
      class: "textarea", id: "mail-new-domain", rows: 2, spellcheck: false,
      autocapitalize: "none", placeholder: "example.com, another-domain.com",
    }),
    token: el("input", { class: "input", id: "mail-cf-token", type: "password", autocomplete: "off" }),
    account: el("input", { class: "input", id: "mail-cf-account", type: "text", autocomplete: "off" }),
    inboxUrl: el("input", { class: "input input--mono", id: "mail-inbox-url", type: "text" }),
    inboxSecret: el("input", { class: "input", id: "mail-inbox-secret", type: "password", autocomplete: "off" }),
  };

  const statusLine = el("div", { class: "text-sm", role: "status" });
  const messageLine = el("p", { class: "text-sm" });
  const routingBox = el("div", { class: "stack--tight" });
  const logBox = el("pre", { class: "code", hidden: true });
  const inboxLine = el("span", { class: "muted text-xs", role: "status" });
  const domainList = el("div", { class: "stack--tight" });
  const tokenNote = el("div", { class: "field__help" });
  const clearBtnSlot = el("span");

  const deployBtn = button({ label: "Set up email", variant: "primary", onClick: () => deploy(false) });

  function paint(m) {
    const doms = m.domains || [];
    const credentialsSaved = !!m.inbox_url && m.inbox_secret_set === true;
    const inboxVerified = credentialsSaved && m.inbox_ok === true;
    const routingDone = doms.length > 0 && doms.every((d) => (m.routing_done || []).includes(d));
    const needsConfirmation = doms.some((d) => state.review.has(d));
    const ready = inboxVerified && routingDone && !needsConfirmation;

    const status = state.deploying ? "Setting up email…"
      : needsConfirmation ? "Routing change needs approval"
      : ready ? "Email ready"
      : !doms.length ? "Enter your domain and Cloudflare API token to begin"
      : !credentialsSaved ? "Worker setup pending"
      : !inboxVerified && !routingDone ? "Inbox verification and domain routing pending"
      : !inboxVerified ? "Credentials saved; inbox verification pending"
      : "Inbox verified; domain routing pending";

    fill(statusLine, el("b", { text: status }));
    statusLine.className = "text-sm " + (ready ? "text-ok" : needsConfirmation ? "text-warn" : "muted");

    messageLine.textContent = state.message;
    messageLine.hidden = !state.message;

    fields.token.placeholder = m.cf_token_set
      ? "Token saved; leave empty to keep it" : "Paste your Cloudflare API token";
    tokenNote.textContent = m.cf_token_set ? "API token saved. You do not need to enter it again." : "";
    tokenNote.hidden = !m.cf_token_set;

    fields.account.placeholder = m.cf_account_id
      ? "Account saved; leave empty to keep it" : "Only needed if your token sees several accounts";
    fields.inboxSecret.placeholder = m.inbox_secret_set
      ? "Secret saved; leave empty to keep it" : "Filled automatically after setup";
    // Do not fight the user's cursor; only adopt the server value when idle.
    if (document.activeElement !== fields.inboxUrl) fields.inboxUrl.value = m.inbox_url || "";

    fill(clearBtnSlot, m.cf_token_set
      ? button({ label: "Remove saved token", variant: "quiet", onClick: clearToken })
      : null);

    fill(routingBox, doms.map((d) => routingRow(d, m)));
    fill(domainList, doms.map((d) => el("div", { class: "row row--tight" }, [
      el("span", { class: "mono text-sm", text: d }),
      button({ label: "Mark routing done", onClick: () => markRoutingDone(d) }),
      button({ label: "Remove", variant: "quiet", onClick: () => removeDomain(d) }),
    ])));

    logBox.textContent = state.log;
    logBox.hidden = !state.log;
    inboxLine.textContent = state.inboxMessage;

    deployBtn.disabled = state.deploying;
    deployBtn.querySelector("span").textContent = state.deploying ? "Setting up email…" : "Set up email";
    for (const f of Object.values(fields)) f.disabled = state.deploying;
  }

  function build() {
    fill(body, [
      el("div", { class: "field" }, [
        el("label", { class: "field__label", for: "mail-new-domain", text: "Domain(s)" }),
        fields.domains,
        el("div", { class: "field__help", text: "Separate domains with commas or new lines." }),
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field__label", for: "mail-cf-token", text: "Cloudflare API token" }),
        fields.token,
        tokenNote,
      ]),
      tokenHelp(),
      el("div", { class: "row" }, [
        deployBtn,
        el("span", { class: "field__help", text: "First setup takes 1–2 minutes. Retries reuse the saved worker." }),
      ]),
      statusLine,
      messageLine,
      routingBox,
      inboxLine,
      el("p", { class: "field__help", text:
        "An inbox test only proves the worker answers. Send a real message to confirm end-to-end delivery." }),
      logBox,
      el("details", { class: "help-details", open: state.advancedOpen,
        on: { toggle: (e) => { state.advancedOpen = e.target.open; } } }, [
        el("summary", { text: "Advanced / manual recovery" }),
        el("div", { class: "stack" }, [
          el("div", { class: "field" }, [
            el("label", { class: "field__label", for: "mail-cf-account", text: "Cloudflare account ID (optional)" }),
            fields.account,
            el("p", { class: "field__help", text: "Cloudflare home > your account > Copy account ID. Leave empty unless setup asks." }),
          ]),
          el("div", { class: "row" }, [
            button({ label: "Save token only", onClick: saveToken }),
            clearBtnSlot,
          ]),
          el("div", { class: "field" }, [
            el("label", { class: "field__label", for: "mail-inbox-url", text: "Worker inbox URL" }),
            fields.inboxUrl,
          ]),
          el("div", { class: "field" }, [
            el("label", { class: "field__label", for: "mail-inbox-secret", text: "Inbox secret" }),
            fields.inboxSecret,
          ]),
          el("div", { class: "row" }, [
            button({ label: "Save inbox settings", onClick: saveManual }),
            button({ label: "Test worker inbox", onClick: testInbox }),
            button({ label: "Redeploy worker", onClick: () => deploy(true) }),
          ]),
          el("p", { class: "field__help", text:
            "Manual fallback: in Cloudflare > your domain > Email > Email Routing, enable routing and point the catch-all at the deployed worker." }),
          domainList,
        ]),
      ]),
    ]);
  }

  async function refresh() {
    try {
      const m = await api.get("/admin/dash/generator/mail");
      if (!body.children.length) build();
      paint(m);
    } catch (e) {
      fill(body, el("p", { class: "field__error", text: e.message }));
    }
  }

  return { node, refresh };
}
