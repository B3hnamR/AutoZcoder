import { Agent, ProxyAgent, fetch as undiciFetch, type Dispatcher } from "undici";

export const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
export interface CaptchaRoute {
  readonly proxyUrl: string;
  readonly dispatcher: Dispatcher;
}

export function validateCaptchaProxy(value: string): string {
  const raw = value.trim();
  if (!raw) return "";
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("invalid ZAI_CAPTCHA_PROXY URL"); }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("unsupported ZAI_CAPTCHA_PROXY scheme: only HTTP/HTTPS proxies are supported; SOCKS cannot fall back to direct");
  }
  if (!url.hostname || url.hash || url.search || url.pathname !== "/") {
    throw new Error("invalid ZAI_CAPTCHA_PROXY: expected an HTTP/HTTPS proxy origin");
  }
  return url.href;
}

export function createCaptchaRoute(value = ""): CaptchaRoute {
  const proxyUrl = validateCaptchaProxy(value);
  return Object.freeze({ proxyUrl, dispatcher: proxyUrl ? new ProxyAgent(proxyUrl) : new Agent() });
}

let route: CaptchaRoute | undefined;
/** Snapshot the explicit sidecar contract before config/bootstrap network I/O. */
export function bootstrapCaptchaRoute(): CaptchaRoute {
  return route ??= createCaptchaRoute(process.env.ZAI_CAPTCHA_PROXY ?? "");
}

type Transport = (url: string, init: RequestInit & { dispatcher: Dispatcher; proxy?: string }) => Promise<Response>;
let testTransport: Transport | undefined;
export function __setCaptchaTransportForTest(value?: Transport): void { testTransport = value; }
const nativeFetch = globalThis.fetch;

export async function captchaFetch(url: string, init: RequestInit, context = bootstrapCaptchaRoute()): Promise<Response> {
  // Never use the global dispatcher or ambient HTTP(S)_PROXY/ALL_PROXY.
  const options = { ...init, dispatcher: context.dispatcher, proxy: context.proxyUrl };
  if (testTransport) return testTransport(url, options);
  // Bun's built-in undici compatibility classes are inert, not dispatchers.
  // Native fetch supports an explicit HTTP(S) proxy; '' disables env proxies.
  if (typeof Bun !== "undefined") return nativeFetch(url, { ...init, proxy: context.proxyUrl } as RequestInit);
  return (undiciFetch as unknown as Transport)(url, options);
}

/** Incremental ceiling, including when a caller holds a locked response body. */
export async function readBoundedBody(response: Response, signal: AbortSignal, limit = MAX_RESPONSE_BYTES): Promise<Buffer> {
  if (!Number.isSafeInteger(limit) || limit < 0) throw new Error("invalid response byte limit");
  const length = response.headers.get("content-length");
  if (length && /^\d+$/.test(length) && Number(length) > limit) {
    try { void response.body?.cancel().catch(() => {}); } catch {}
    throw new Error("captcha response exceeds byte limit");
  }
  const reader = response.body?.getReader();
  if (!reader) return Buffer.alloc(0);
  const chunks: Buffer[] = [];
  let total = 0;
  const cancel = () => { try { void reader.cancel().catch(() => {}); } catch {} };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    if (signal.aborted) throw new Error("captcha deadline exceeded");
    while (true) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error("captcha deadline exceeded");
      if (done) break;
      if (value.byteLength > limit - total) throw new Error("captcha response exceeds byte limit");
      total += value.byteLength;
      chunks.push(Buffer.from(value));
    }
    return Buffer.concat(chunks, total);
  } catch (err) {
    cancel();
    throw err;
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}
