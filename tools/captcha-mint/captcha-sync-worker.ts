// Self-contained worker logic also runs against an injected offline transport in tests.
export async function runSyncFetchMessage(m: any, transport: typeof fetch): Promise<void> {
  const enc = new TextEncoder();
  const i32 = new Int32Array(m.sab);
  const u8 = new Uint8Array(m.sab);
  const payloadAt = 64;
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const cancel = () => { try { void reader?.cancel().catch(() => {}); } catch {} };
  controller.signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(), Math.max(0, m.deadlineAt - Date.now()));
  try {
    if (Date.now() >= m.deadlineAt) throw new Error("sync fetch timeout");
    const res = await transport(m.url, { ...m.init, signal: controller.signal });
    const headers: Record<string, string> = {};
    for (const [key, value] of res.headers) headers[key] = value;
    const setCookie = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
    const metadata = [enc.encode(res.statusText || ""), enc.encode(JSON.stringify(headers)), enc.encode(JSON.stringify(setCookie))];
    let off = payloadAt;
    for (let n = 0; n < metadata.length; n++) {
      const bytes = metadata[n];
      if (bytes.length > u8.length - off) throw new Error("sync fetch response exceeds shared buffer");
      u8.set(bytes, off);
      i32[n + 2] = bytes.length;
      off += bytes.length;
    }
    const bodyAt = off;
    const length = res.headers.get("content-length");
    if (length && /^\d+$/.test(length) && Number(length) > u8.length - off) {
      try { void res.body?.cancel().catch(() => {}); } catch {}
      throw new Error("sync fetch response exceeds shared buffer");
    }
    reader = res.body?.getReader();
    while (reader) {
      if (controller.signal.aborted) throw new Error("sync fetch timeout");
      const { done, value } = await reader.read();
      if (controller.signal.aborted) throw new Error("sync fetch timeout");
      if (done) break;
      if (value.byteLength > u8.length - off) throw new Error("sync fetch response exceeds shared buffer");
      u8.set(value, off);
      off += value.byteLength;
    }
    i32[5] = off - bodyAt;
    i32[1] = res.status;
    Atomics.store(i32, 0, 1);
  } catch (err: any) {
    cancel();
    const bytes = enc.encode(String(err?.message ?? err)).subarray(0, u8.length - payloadAt);
    u8.set(bytes, payloadAt);
    i32[5] = bytes.length;
    i32[1] = i32[2] = i32[3] = i32[4] = 0;
    Atomics.store(i32, 0, 2);
  } finally {
    clearTimeout(timer);
    controller.abort();
    try { reader?.releaseLock(); } catch {}
    Atomics.notify(i32, 0);
  }
}

export const SYNC_WORKER_SRC = `
  const { parentPort, workerData } = require("node:worker_threads");
  let dispatcher, proxyUrl;
  const run = (${runSyncFetchMessage.toString()});
  parentPort.on("message", m => {
    // Load failure is reported through the same SAB, never a direct retry.
    run(m, async (url, init) => {
      if (workerData.nativeProxy) return globalThis.fetch(url, { ...init, proxy: m.proxyUrl });
      const { fetch, Agent, ProxyAgent } = await import(workerData.undiciURL);
      if (!dispatcher || proxyUrl !== m.proxyUrl) {
        if (dispatcher) void dispatcher.close();
        proxyUrl = m.proxyUrl;
        dispatcher = proxyUrl ? new ProxyAgent(proxyUrl) : new Agent();
      }
      return fetch(url, { ...init, dispatcher });
    });
  });
`;
