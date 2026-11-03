import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { Worker } from "node:worker_threads";
import { GlobalWindow as Window } from "happy-dom";
import WindowBrowserContext from "happy-dom/lib/window/WindowBrowserContext.js";
import { CaptchaDeadline } from "./captcha-deadline.ts";
import { CaptchaLease } from "./captcha-lease.ts";
import { storeCookieStrings, serializeCookies } from "./captcha-cookies.ts";
import {
  __setCaptchaTransportForTest, createCaptchaRoute, validateCaptchaProxy,
  readBoundedBody, MAX_RESPONSE_BYTES,
} from "./captcha-transport.ts";
import { runSyncFetchMessage, SYNC_WORKER_SRC } from "./captcha-sync-worker.ts";

if (!("Bun" in globalThis)) {
  const { registerHooks } = await import("node:module");
  const directory = new URL("./", import.meta.url).href;
  const names = new Set(["./captcha-worker-dispatch.js", "./captcha-happy.js"]);
  registerHooks({ resolve(specifier, context, nextResolve) {
    if (context.parentURL?.startsWith(directory) && names.has(specifier)) {
      return nextResolve(new URL(specifier.replace(/\.js$/, ".ts"), context.parentURL).href, context);
    }
    return nextResolve(specifier, context);
  } });
}

const {
  __makeInterceptorForTest, primeWindowCookies, solveTraceless,
  __setDomFactoryForTest, __clearReusableWindowForTest,
} = await import("./captcha-happy.ts");
const { readConfig } = await import("./mint.ts");
const { solveViaWorkerOrInProcess, __setInProcessSolverForTest } = await import("./captcha-worker-dispatch.ts");
const forbidNetwork = async (): Promise<Response> => { throw new Error("offline fixture forbids external network"); };
__setCaptchaTransportForTest(forbidNetwork);

const windows: Window[] = [];
const budgets: CaptchaDeadline[] = [];
const routes: ReturnType<typeof createCaptchaRoute>[] = [];
function windowFixture() {
  const w = new Window({ url: "https://zcode.z.ai/" });
  windows.push(w);
  return w;
}
function budget(ms = 1_000, route = createCaptchaRoute("")) {
  routes.push(route);
  const deadline = new CaptchaDeadline(ms, undefined, route);
  budgets.push(deadline);
  return deadline;
}
function container(w: Window) { return new WindowBrowserContext(w).getBrowserFrame().page.context.cookieContainer; }
afterEach(async () => {
  __setCaptchaTransportForTest(forbidNetwork);
  __setDomFactoryForTest(null);
  __setInProcessSolverForTest(null);
  __clearReusableWindowForTest();
  for (const deadline of budgets.splice(0)) deadline.dispose();
  for (const w of windows.splice(0)) await w.happyDOM.close();
  for (const route of routes.splice(0)) await route.dispatcher.close?.();
});

test("Happy DOM 20.14.6 parser preserves key/originURL, expiry, scope, HttpOnly and deletion", () => {
  const w = windowFixture();
  const jar = container(w);
  storeCookieStrings(jar, "https://zcode.z.ai/", [
    "session=a=b==; Path=/; Secure; HttpOnly; SameSite=None; Expires=Wed, 01 Jan 2031 00:00:00 GMT",
    "scoped=path; Path=/only", "expired=gone; Max-Age=0", "invalid=gone; Domain=unrelated.invalid",
  ]);
  assert.equal(serializeCookies(jar, "https://zcode.z.ai/"), "session=a=b==");
  assert.equal(w.document.cookie, "");
  const cookie = jar.getCookies(new URL("https://zcode.z.ai/"), false)[0];
  assert.equal(cookie.key, "session");
  assert.equal(cookie.originURL.href, "https://zcode.z.ai/");
  assert.ok(cookie.expires instanceof Date);
  assert.equal(cookie.sameSite, "None");
  assert.equal(serializeCookies(jar, "http://zcode.z.ai/"), "");
  assert.equal(serializeCookies(jar, "https://other.invalid/"), "");
  assert.match(serializeCookies(jar, "https://zcode.z.ai/only/test"), /scoped=path/);
  storeCookieStrings(jar, "https://zcode.z.ai/", ["session=delete; Path=/; Max-Age=0"]);
  assert.equal(serializeCookies(jar, "https://zcode.z.ai/"), "");
});

test("priming and synthetic cookies serialize real keys without leaking HttpOnly to document", () => {
  const w = windowFixture();
  primeWindowCookies(w, ["primed=from-response; Path=/; HttpOnly"]);
  const header = serializeCookies(container(w), "https://zcode.z.ai/");
  for (const name of ["primed", "zcode_visitor_id", "zcode_device_mid", "visitor_id"]) assert.match(header, new RegExp(`(?:^|; )${name}=`));
  assert.doesNotMatch(header, /undefined=/);
  assert.doesNotMatch(w.document.cookie, /(?:^|; )(?:primed|visitor_id)=/);
});

test("async and sync response cookies are isolated by request window, not host globals", async () => {
  const a = windowFixture();
  const b = windowFixture();
  const deadline = budget();
  const seen: string[] = [];
  __setCaptchaTransportForTest(async (_url, init) => {
    seen.push((init.headers as Record<string, string>).cookie ?? "");
    return new Response("offline", { headers: { "set-cookie": "asyncCookie=a=b; Path=/; HttpOnly" } });
  });
  let syncCookie = "";
  const interceptor = __makeInterceptorForTest(false, () => deadline, (_url, init) => {
    syncCookie = init.headers.cookie;
    return { status: 200, statusText: "OK", headers: {}, setCookie: ["syncCookie=s; Path=/"], body: Buffer.from("offline") };
  });
  primeWindowCookies(a, ["owner=A; Path=/"]);
  await interceptor.beforeAsyncRequest({ request: new a.Request("https://zcode.z.ai/data"), window: a });
  interceptor.beforeSyncRequest({ request: new a.Request("https://zcode.z.ai/data"), window: a });
  assert.match(seen[0], /owner=A/);
  assert.match(syncCookie, /asyncCookie=a=b/);
  assert.match(serializeCookies(container(a), "https://zcode.z.ai/"), /syncCookie=s/);
  assert.equal(serializeCookies(container(b), "https://zcode.z.ai/"), "");
  const omit = new a.Request("https://zcode.z.ai/data", { credentials: "omit" });
  await interceptor.beforeAsyncRequest({ request: omit, window: a });
  assert.equal(seen[1], "");
});

test("proxy validation accepts HTTP(S) and rejects SOCKS/malformed values before transport", () => {
  assert.equal(validateCaptchaProxy(""), "");
  assert.equal(validateCaptchaProxy(" https://user:pass@proxy.invalid:8443 "), "https://user:pass@proxy.invalid:8443/");
  for (const value of ["socks5://proxy.invalid:1080", "socks5h://proxy.invalid:1080", "file:///tmp/p", "garbage", "http://proxy.invalid/path"]) {
    assert.throws(() => createCaptchaRoute(value), /ZAI_CAPTCHA_PROXY/);
  }
});

test("config is routed before solver loading; async and sync requests share explicit route", async () => {
  const route = createCaptchaRoute("http://proxy.invalid:8080");
  const deadline = budget(1_000, route);
  const dispatchers: unknown[] = [];
  const urls: string[] = [];
  __setCaptchaTransportForTest(async (url, init) => {
    dispatchers.push(init.dispatcher);
    assert.equal(init.proxy, route.proxyUrl);
    urls.push(url);
    return url.includes("/configs?")
      ? Response.json({ data: { configs: { captcha: { enabled: true, sceneId: "fixture", region: "sgp", prefix: "offline" } } } })
      : new Response("offline");
  });
  const saved = [process.env.ZAI_CAPTCHA_SCENE, process.env.ZAI_CAPTCHA_PREFIX];
  delete process.env.ZAI_CAPTCHA_SCENE;
  delete process.env.ZAI_CAPTCHA_PREFIX;
  try { assert.equal((await readConfig(deadline)).scene, "fixture"); }
  finally {
    ["ZAI_CAPTCHA_SCENE", "ZAI_CAPTCHA_PREFIX"].forEach((key, i) => { if (saved[i] === undefined) delete process.env[key]; else process.env[key] = saved[i]; });
  }
  const w = windowFixture();
  let syncRoute = "";
  const interceptor = __makeInterceptorForTest(false, () => deadline, (_url, _init, _ms, proxyUrl) => {
    syncRoute = proxyUrl;
    return { status: 200, headers: {}, setCookie: [], body: Buffer.from("offline") };
  });
  await interceptor.beforeAsyncRequest({ request: new w.Request("https://fixture.invalid/async"), window: w });
  interceptor.beforeSyncRequest({ request: new w.Request("https://fixture.invalid/sync"), window: w });
  assert.match(urls[0], /\/configs\?/);
  assert.deepEqual(dispatchers, [route.dispatcher, route.dispatcher]);
  assert.equal(syncRoute, route.proxyUrl);
});

test("explicit direct uses its own dispatcher even with ambient proxies", async () => {
  const old = process.env.HTTPS_PROXY;
  process.env.HTTPS_PROXY = "socks5://ambient.invalid:1080";
  const route = createCaptchaRoute("");
  const deadline = budget(1_000, route);
  let seen: unknown;
  __setCaptchaTransportForTest(async (_url, init) => { seen = init.dispatcher; assert.equal(init.proxy, ""); return new Response("offline"); });
  try {
    await deadline.fetch("https://fixture.invalid/", {}, r => readBoundedBody(r, deadline.controller.signal));
    assert.equal(seen, route.dispatcher);
    assert.equal(route.proxyUrl, "");
  } finally { if (old === undefined) delete process.env.HTTPS_PROXY; else process.env.HTTPS_PROXY = old; }
});

test("async reader cancels incremental oversize and advertised oversize without arrayBuffer", async () => {
  let cancels = 0;
  const controller = new AbortController();
  const response = new Response(new ReadableStream({ pull(c) { c.enqueue(new Uint8Array(5)); }, cancel() { cancels++; } }));
  response.arrayBuffer = () => { throw new Error("must not fully buffer"); };
  await assert.rejects(readBoundedBody(response, controller.signal, 8), /byte limit/);
  assert.equal(cancels, 1);
  const advertised = new Response(new ReadableStream({ cancel() { cancels++; } }), { headers: { "content-length": String(MAX_RESPONSE_BYTES + 1) } });
  await assert.rejects(readBoundedBody(advertised, controller.signal), /byte limit/);
  assert.equal(cancels, 2);
});

test("async deadline cancels a locked stream reader while it waits for bytes", async () => {
  let cancelled = 0;
  const deadline = budget(30);
  __setCaptchaTransportForTest(async () => new Response(new ReadableStream({ pull() { return new Promise(() => {}); }, cancel() { cancelled++; } })));
  await assert.rejects(deadline.fetch("https://fixture.invalid/", {}, r => readBoundedBody(r, deadline.controller.signal)), /deadline/);
  assert.equal(cancelled, 1);
});

test("sync worker streams directly into 8MiB SAB budget and cancels oversize", async () => {
  const sab = new SharedArrayBuffer(64 + MAX_RESPONSE_BYTES);
  let cancellations = 0;
  let reads = 0;
  await runSyncFetchMessage({ sab, url: "https://fixture.invalid/", deadlineAt: Date.now() + 1000, init: {} }, async () => {
    const response = new Response(new ReadableStream({ pull(c) { reads++; c.enqueue(new Uint8Array(1024 * 1024)); }, cancel() { cancellations++; } }));
    response.arrayBuffer = () => { throw new Error("must not fully buffer"); };
    return response;
  });
  const i32 = new Int32Array(sab);
  assert.equal(i32[0], 2);
  assert.match(Buffer.from(sab, 64, i32[5]).toString(), /exceeds shared buffer/);
  assert.equal(cancellations, 1);
  assert.ok(reads <= 9);
});

test("sync worker deadline aborts and cancels a locked body", async () => {
  const sab = new SharedArrayBuffer(64 + 1024);
  let cancellations = 0;
  await runSyncFetchMessage({ sab, url: "https://fixture.invalid/", deadlineAt: Date.now() + 30, init: {} }, async () =>
    new Response(new ReadableStream({ pull() { return new Promise(() => {}); }, cancel() { cancellations++; } })));
  assert.equal(new Int32Array(sab)[0], 2);
  assert.equal(cancellations, 1);
});

test("actual eval worker source is self-contained and forwards explicit dispatcher without network", async () => {
  const module = `export class Agent { kind='direct'; } export class ProxyAgent { constructor(url){this.url=url;} } export async function fetch(url, init) { return new Response(JSON.stringify({url, proxy:init.dispatcher.url ?? '', method:init.method})); }`;
  const worker = new Worker(SYNC_WORKER_SRC, { eval: true, workerData: { undiciURL: `data:text/javascript;base64,${Buffer.from(module).toString("base64")}` } });
  const sab = new SharedArrayBuffer(64 + 4096);
  try {
    const error = new Promise<never>((_, reject) => worker.once("error", reject));
    worker.postMessage({ sab, url: "https://fixture.invalid/", proxyUrl: "http://proxy.invalid:8080/", init: { method: "POST" }, deadlineAt: Date.now() + 2_000 });
    const i32 = new Int32Array(sab);
    await Promise.race([Atomics.waitAsync(i32, 0, 0, 2_000).value, error]);
    assert.equal(i32[0], 1, Buffer.from(sab, 64, i32[5]).toString());
    const offset = 64 + i32[2] + i32[3] + i32[4];
    assert.deepEqual(JSON.parse(Buffer.from(sab, offset, i32[5]).toString()), { url: "https://fixture.invalid/", proxy: "http://proxy.invalid:8080/", method: "POST" });
  } finally { await worker.terminate(); }
});

test("lease excludes concurrent owners and removes expired queued waiters", async () => {
  const lease = new CaptchaLease();
  const release = await lease.acquire(budget());
  await assert.rejects(lease.acquire(budget(20)), /deadline/);
  let acquired = false;
  const next = lease.acquire(budget()).then(unlock => { acquired = true; unlock(); });
  await Promise.resolve();
  assert.equal(acquired, false);
  release();
  release();
  await next;
  assert.equal(acquired, true);
});

test("Bun sync-worker native fetch path receives explicit proxy and explicit direct", async () => {
  const source = `globalThis.fetch = async (url, init) => new Response(JSON.stringify({ url, proxy: init.proxy }));\n${SYNC_WORKER_SRC}`;
  const worker = new Worker(source, { eval: true, workerData: { nativeProxy: true } });
  try {
    for (const proxyUrl of ["http://proxy.invalid:8080/", ""]) {
      const sab = new SharedArrayBuffer(64 + 4096);
      const i32 = new Int32Array(sab);
      worker.postMessage({ sab, url: "https://fixture.invalid/", proxyUrl, init: {}, deadlineAt: Date.now() + 2_000 });
      await Atomics.waitAsync(i32, 0, 0, 2_000).value;
      assert.equal(i32[0], 1, Buffer.from(sab, 64, i32[5]).toString());
      const offset = 64 + i32[2] + i32[3] + i32[4];
      assert.equal(JSON.parse(Buffer.from(sab, offset, i32[5]).toString()).proxy, proxyUrl);
    }
  } finally { await worker.terminate(); }
});

test("public solve serializes reusable windows; expired waiter never touches active SDK", async () => {
  let active = 0;
  let peak = 0;
  let created = 0;
  const token = Buffer.from(JSON.stringify({ securityToken: "offline-fixture-".repeat(20) })).toString("base64");
  __setDomFactoryForTest(async () => {
    created++;
    const w = windowFixture();
    w.initAliyunCaptcha = (opts) => {
      active++;
      peak = Math.max(peak, active);
      setTimeout(() => { active--; opts.success(token); }, 60);
    };
    return { window: w, browserFrame: new WindowBrowserContext(w).getBrowserFrame() };
  });
  const first = solveTraceless({ scene: "offline", timeoutMs: 1_000 });
  const expired = assert.rejects(solveTraceless({ scene: "offline", timeoutMs: 20 }), /deadline/);
  const second = solveTraceless({ scene: "offline", timeoutMs: 1_000 });
  assert.equal(await first, token);
  await expired;
  assert.equal(await second, token);
  assert.equal(peak, 1);
  assert.equal(created, 1);
});

test("fallback dispatch retains ownership until old solver settles, even after deadline", async () => {
  let calls = 0;
  let finish: (value: string) => void = () => {};
  let started: () => void = () => {};
  const ready = new Promise<void>(resolve => { started = resolve; });
  __setInProcessSolverForTest(async () => {
    calls++;
    started();
    return new Promise<string>(resolve => { finish = resolve; });
  });
  const request = { scene: "offline", region: "fixture", prefix: "fixture" };
  const first = solveViaWorkerOrInProcess({ ...request, deadlineAt: Date.now() + 80 });
  await ready;
  await assert.rejects(solveViaWorkerOrInProcess({ ...request, deadlineAt: Date.now() + 20 }), /deadline/);
  assert.equal(calls, 1);
  // Yield past the active deadline without resolving its non-cooperative fixture.
  const elapsed = budget(100);
  await assert.rejects(elapsed.run(() => new Promise(() => {})), /deadline/);
  const result = assert.rejects(first, /deadline/);
  finish("late-offline-fixture");
  await result;
  assert.equal(calls, 1);
});
