import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { GlobalWindow as Window, PropertySymbol } from "happy-dom";
import { CaptchaDeadline } from "./captcha-deadline.ts";
import { __setCaptchaTransportForTest } from "./captcha-transport.ts";

// Bun resolves these source-tree .js imports to .ts; Node's native loader does not.
// Register before importing mint's dependency graph, without changing production imports.
if (!("Bun" in globalThis)) {
  const { registerHooks } = await import("node:module");
  const directory = new URL("./", import.meta.url).href;
  const sourceImports = new Set(["./captcha-worker-dispatch.js", "./captcha-happy.js"]);
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (context.parentURL?.startsWith(directory) && sourceImports.has(specifier)) {
        return nextResolve(new URL(specifier.replace(/\.js$/, ".ts"), context.parentURL).href, context);
      }
      return nextResolve(specifier, context);
    },
  });
}

const {
  __makeInterceptorForTest, __waitForForTest, createDom, solveTraceless,
  rememberCdnBody,
  __setDomFactoryForTest,
  installBufferedRequest,
} = await import("./captcha-happy.ts");
const { readConfig } = await import("./mint.ts");
const forbidNetwork = async (): Promise<Response> => { throw new Error("offline fixture forbids external network"); };
__setCaptchaTransportForTest(forbidNetwork);

const deadlines: CaptchaDeadline[] = [];
const windows: Window[] = [];
const deadline = (ms = 1_000, at?: number) => {
  const value = new CaptchaDeadline(ms, at);
  deadlines.push(value);
  return value;
};
const windowFixture = (interceptor?: object) => {
  const w = new Window({
    url: "https://fixture.invalid/",
    settings: { fetch: { disableSameOriginPolicy: true, interceptor } },
  });
  installBufferedRequest(w);
  windows.push(w);
  return w;
};

afterEach(async () => {
  __setDomFactoryForTest(null);
  __setCaptchaTransportForTest(forbidNetwork);
  for (const value of deadlines.splice(0)) value.dispose();
  for (const w of windows.splice(0)) await w.happyDOM.close();
});

test("AUD031: sync XHR forwards the exact buffered form body, not its stream", () => {
  const budget = deadline();
  let captured: Record<string, any> | undefined;
  const interceptor = __makeInterceptorForTest(false, () => budget, (_url, init) => {
    captured = init;
    return { status: 200, statusText: "OK", headers: {}, setCookie: [], body: Buffer.from("fixture-ok") };
  });
  const w = windowFixture(interceptor);
  const xhr = new w.XMLHttpRequest();
  xhr.open("POST", "https://fixture.invalid/submit", false);
  xhr.setRequestHeader("content-type", "application/x-www-form-urlencoded");
  const form = "Action=FixtureOnly&value=a%2Bb%26c&empty=";
  xhr.send(form);
  assert.equal(Buffer.isBuffer(captured?.body), true);
  assert.equal(captured?.body.toString(), form);
  assert.equal(xhr.responseText, "fixture-ok");
});

test("AUD031: Request-as-init reconstruction preserves bytes and request options", () => {
  const w = windowFixture();
  const controller = new w.AbortController();
  const bytes = Buffer.from([0, 255, 38, 43, 61, 128]);
  const original = new w.Request("https://fixture.invalid/source", {
    method: "POST", body: bytes, signal: controller.signal,
    credentials: "include", redirect: "manual", referrerPolicy: "no-referrer",
    headers: { "content-type": "application/octet-stream", "x-fixture": "preserved" },
  });
  const rebuilt = new w.Request(original.url, original);
  assert.deepEqual(rebuilt[PropertySymbol.bodyBuffer], bytes);
  assert.equal(rebuilt.method, original.method);
  assert.equal(rebuilt.credentials, original.credentials);
  assert.equal(rebuilt.redirect, original.redirect);
  assert.equal(rebuilt.referrerPolicy, original.referrerPolicy);
  assert.equal(rebuilt.signal, original.signal);
  assert.equal(rebuilt.headers.get("content-type"), "application/octet-stream");
  assert.equal(rebuilt.headers.get("x-fixture"), "preserved");
  assert.equal(original.bodyUsed, false);
  const empty = new w.Request("https://fixture.invalid/empty", { method: "POST", body: Buffer.alloc(0) });
  assert.deepEqual(new w.Request(empty.url, empty)[PropertySymbol.bodyBuffer], Buffer.alloc(0));
  const absent = new w.Request("https://fixture.invalid/absent");
  assert.equal(new w.Request(absent.url, absent).body, null);
});

test("AUD031: genuine unbuffered sync XHR fails closed without calling transport", () => {
  const budget = deadline();
  let calls = 0;
  const interceptor = __makeInterceptorForTest(false, () => budget, () => { calls++; });
  const w = windowFixture(interceptor);
  const xhr = new w.XMLHttpRequest();
  xhr.open("POST", "https://fixture.invalid/unbuffered", false);
  xhr.send(new ReadableStream({ start(controller) { controller.enqueue(Buffer.from("dummy")); controller.close(); } }));
  assert.equal(xhr.status, 503);
  assert.equal(xhr.responseText, "");
  assert.equal(calls, 0);
});

test("AUD034: cached, network, error and unbuffered sync replies share the Buffer contract", () => {
  const w = windowFixture();
  const budget = deadline();
  const request = new w.Request("https://fixture.invalid/submit", { method: "POST", body: "dummy" });
  const check = (response, status) => {
    assert.equal(response.status, status);
    assert.equal(response.ok, status >= 200 && status < 300);
    assert.equal(Buffer.isBuffer(response.body), true);
    assert.ok(response.headers instanceof w.Headers);
    assert.equal(response[PropertySymbol.virtualServerFile], null);
    assert.equal(typeof response.body.toString(), "string");
    assert.equal(response instanceof w.Response, false);
  };
  const success = __makeInterceptorForTest(false, () => budget, () => ({
    status: 201, statusText: "Created", headers: {}, setCookie: [], body: Buffer.from("dummy"),
  }));
  check(success.beforeSyncRequest({ request, window: w }), 201);
  const failure = __makeInterceptorForTest(false, () => budget, () => ({ error: "dummy failure" }));
  check(failure.beforeSyncRequest({ request, window: w }), 503);
  const throwing = __makeInterceptorForTest(false, () => budget, () => { throw new Error("fixture"); });
  check(throwing.beforeSyncRequest({ request, window: w }), 503);
  request[PropertySymbol.bodyBuffer] = null;
  check(success.beforeSyncRequest({ request, window: w }), 503);
  const url = "https://o.alicdn.com/audit-fixture-only.js";
  rememberCdnBody(url, Buffer.from("/* offline dummy */"));
  check(success.beforeSyncRequest({ request: new w.Request(url), window: w }), 200);
});

test("AUD034: failed sync XHR exposes an empty body, not a stream string", () => {
  const budget = deadline();
  const interceptor = __makeInterceptorForTest(false, () => budget, () => ({ error: "offline dummy failure" }));
  const w = windowFixture(interceptor);
  const xhr = new w.XMLHttpRequest();
  xhr.open("POST", "https://fixture.invalid/fail", false);
  xhr.send("Action=FixtureOnly");
  assert.equal(xhr.status, 503);
  assert.equal(xhr.responseText, "");
});

test("AUD033: expired sync requests fail closed without starting the blocking fetch", () => {
  const budget = deadline(1_000, Date.now() - 1);
  const w = windowFixture();
  let calls = 0;
  const interceptor = __makeInterceptorForTest(false, () => budget, () => { calls++; });
  const reply = interceptor.beforeSyncRequest({ request: new w.Request("https://fixture.invalid/"), window: w });
  assert.equal(reply.status, 503);
  assert.equal(Buffer.isBuffer(reply.body), true);
  assert.equal(calls, 0);
});

test("AUD033: deadline covers response consumption and safely cancels rejecting bodies", async () => {
  const budget = deadline(40);
  let signal: AbortSignal | undefined;
  let cancellations = 0;
  __setCaptchaTransportForTest(async (_url, init) => {
    signal = init?.signal as AbortSignal;
    return {
      body: { cancel: () => { cancellations++; return Promise.reject(new Error("locked fixture")); } },
      arrayBuffer: () => new Promise(() => {}),
    } as unknown as Response;
  });
  await assert.rejects(budget.fetch("https://fixture.invalid/body", {}, r => r.arrayBuffer()), /deadline/);
  assert.equal(signal?.aborted, true);
  assert.equal(cancellations, 1);
});

test("AUD033: synchronous body cancellation errors do not replace successful results", async () => {
  __setCaptchaTransportForTest(async () => ({
    body: { cancel: () => { throw new Error("locked dummy body"); } },
  } as unknown as Response));
  assert.equal(await deadline().fetch("https://fixture.invalid/", {}, async () => "dummy-ok"), "dummy-ok");
});

test("AUD033: late fetch responses are cancelled after the caller timed out", async () => {
  let respond: (response: Response) => void = () => {};
  let cancellations = 0;
  __setCaptchaTransportForTest(() => new Promise<Response>(resolve => { respond = resolve; }));
  const budget = deadline(30);
  await assert.rejects(budget.fetch("https://fixture.invalid/late", {}, async () => "unexpected"), /deadline/);
  respond({ body: { cancel: async () => { cancellations++; } } } as unknown as Response);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(cancellations, 1);
});

test("AUD033: config body parsing cannot outlive the original budget", async () => {
  const saved = { ...process.env };
  try {
    delete process.env.ZAI_CAPTCHA_SCENE;
    delete process.env.ZAI_CAPTCHA_PREFIX;
    let signal: AbortSignal | undefined;
    __setCaptchaTransportForTest(async (_url, init) => {
      signal = init?.signal as AbortSignal;
      return new Response(new ReadableStream({ pull() { return new Promise(() => {}); } }));
    });
    await assert.rejects(readConfig(deadline(40)), /deadline/);
    assert.equal(signal?.aborted, true);
  } finally {
    for (const key of ["ZAI_CAPTCHA_SCENE", "ZAI_CAPTCHA_PREFIX"]) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
});

test("AUD033: setup aborts cookie priming before any DOM or SDK network load", async () => {
  let signal: AbortSignal | undefined;
  let calls = 0;
  __setCaptchaTransportForTest((_url, init) => {
    calls++;
    signal = init?.signal as AbortSignal;
    return new Promise(() => {});
  });
  await assert.rejects(createDom("fixture", "fixture", deadline(40)), /deadline/);
  assert.equal(signal?.aborted, true);
  assert.equal(calls, 1);
});

test("AUD033: an expired absolute budget never starts config or solve setup", async () => {
  let calls = 0;
  __setCaptchaTransportForTest(async () => { calls++; throw new Error("offline fixture forbids network"); });
  await assert.rejects(readConfig(deadline(1_000, Date.now() - 1)), /deadline/);
  await assert.rejects(solveTraceless({ scene: "fixture", deadlineAt: Date.now() - 1, reuseWindow: false }), /deadline/);
  assert.equal(calls, 0);
});

test("AUD033: readiness uses the setup deadline rather than resetting its timeout", async () => {
  const budget = deadline(1_000, Date.now() + 40);
  await assert.rejects(__waitForForTest(() => false, budget, 5), /deadline/);
  assert.throws(() => budget.remaining(), /deadline/);
});

test("AUD033: pending SDK solve expires and destroys the DOM; late callbacks are inert", async () => {
  let options: any;
  let closes = 0;
  const w = windowFixture();
  const close = w.happyDOM.close.bind(w.happyDOM);
  w.happyDOM.close = async () => { closes++; await close(); };
  __setDomFactoryForTest(async () => {
    w.initAliyunCaptcha = (value) => { options = value; };
    return { window: w, browserFrame: {} };
  });
  await assert.rejects(solveTraceless({ scene: "dummy", timeoutMs: 300, reuseWindow: false }), /timeout|deadline/);
  assert.equal(closes, 1);
  assert.equal(w.__capDeadline.controller.signal.aborted, true);
  let starts = 0;
  options.getInstance({ startTracelessVerification: () => { starts++; } });
  options.success("dummy-late-token-not-a-real-captcha");
  assert.equal(starts, 0);
});

test("AUD033: synchronous SDK failure clears solve timers and destroys its DOM", async () => {
  const w = windowFixture();
  __setDomFactoryForTest(async () => {
    w.initAliyunCaptcha = () => { throw new Error("offline fixture SDK failure"); };
    return { window: w, browserFrame: {} };
  });
  await assert.rejects(solveTraceless({ scene: "dummy", timeoutMs: 1_000, reuseWindow: false }),
    /offline fixture SDK failure/);
  assert.equal(w.__capDeadline.controller.signal.aborted, true);
});
