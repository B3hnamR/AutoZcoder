import { bootstrapCaptchaRoute, captchaFetch, type CaptchaRoute } from "./captcha-transport.ts";

/** One absolute budget, including setup, response bodies and SDK readiness. */
export class CaptchaDeadline {
  readonly at: number;
  readonly controller = new AbortController();
  readonly route: CaptchaRoute;
  private readonly timer: ReturnType<typeof setTimeout>;

  constructor(timeoutMs = Number(process.env.CAPTCHA_SOLVE_TIMEOUT_MS || 20_000), at?: number,
    route: CaptchaRoute = bootstrapCaptchaRoute()) {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || (at !== undefined && !Number.isFinite(at))) {
      throw new Error("invalid captcha deadline");
    }
    this.at = Math.min(at ?? Infinity, Date.now() + timeoutMs);
    this.route = route;
    this.timer = setTimeout(() => this.dispose(), Math.max(0, this.at - Date.now()));
  }

  remaining(): number {
    const ms = this.at - Date.now();
    if (ms <= 0 || this.controller.signal.aborted) throw new Error("captcha deadline exceeded");
    return ms;
  }

  async run<T>(operation: () => Promise<T>): Promise<T> {
    this.remaining();
    const signal = this.controller.signal;
    let onAbort: () => void = () => {};
    const aborted = new Promise<never>((_, reject) => {
      onAbort = () => reject(new Error("captcha deadline exceeded"));
      signal.addEventListener("abort", onAbort, { once: true });
    });
    try {
      const value = await Promise.race([operation(), aborted]);
      this.remaining();
      return value;
    } finally {
      signal.removeEventListener("abort", onAbort);
    }
  }

  async fetch<T>(url: string, init: RequestInit, consume: (response: Response) => Promise<T>): Promise<T> {
    let response: Response | undefined;
    let cancelled = false;
    const cancel = () => {
      if (!response || cancelled) return;
      cancelled = true;
      // A locked/disturbed body can throw synchronously or reject cancel().
      // Never let cancellation hide the result or become unhandled.
      try { void response.body?.cancel().catch(() => {}); } catch {}
    };
    const signal = this.controller.signal;
    signal.addEventListener("abort", cancel, { once: true });
    try {
      return await this.run(async () => {
        response = await captchaFetch(url, { ...init, signal }, this.route);
        try {
          this.remaining();
          return await consume(response);
        } finally {
          cancel();
        }
      });
    } finally {
      signal.removeEventListener("abort", cancel);
      cancel();
    }
  }

  dispose(): void {
    clearTimeout(this.timer);
    this.controller.abort();
  }
}
