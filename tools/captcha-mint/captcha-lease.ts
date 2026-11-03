import type { CaptchaDeadline } from "./captcha-deadline.ts";

/** One host-global guest realm at a time; timed-out waiters never take ownership. */
export class CaptchaLease {
  private busy = false;
  private waiters: Array<() => void> = [];

  async acquire(deadline: CaptchaDeadline): Promise<() => void> {
    deadline.remaining();
    if (this.busy) {
      await new Promise<void>((resolve, reject) => {
        const signal = deadline.controller.signal;
        const ready = () => { signal.removeEventListener("abort", abort); resolve(); };
        const abort = () => {
          this.waiters = this.waiters.filter(value => value !== ready);
          reject(new Error("captcha deadline exceeded waiting for in-process lease"));
        };
        this.waiters.push(ready);
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
      });
    } else {
      this.busy = true;
    }
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      const next = this.waiters.shift();
      if (next) next();
      else this.busy = false;
    };
    try { deadline.remaining(); } catch (err) { release(); throw err; }
    return release;
  }
}
