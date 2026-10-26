/**
 * Solver backend dispatch.
 *
 * Backend (ZCODE_CAPTCHA_BACKEND): "happy" (default) — the happy-dom solver
 * in src/proxy/captcha-happy.ts. Execution (one worker thread per solve,
 * with an in-process fallback where the asset is unavailable — issue #54)
 * lives in captcha-worker-dispatch.ts.
 */
import { solveViaWorkerOrInProcess } from "./captcha-worker-dispatch.js";

const BACKEND = process.env.ZCODE_CAPTCHA_BACKEND?.trim().toLowerCase() || "happy";

export async function runCaptchaSolve(scene: string, region: string, prefix: string): Promise<string> {
  if (BACKEND !== "happy") {
    throw new Error(`captcha backend "${BACKEND}" is not available; use ZCODE_CAPTCHA_BACKEND=happy`);
  }
  return solveViaWorkerOrInProcess({ scene, region, prefix });
}

/** Worker-per-solve needs no concurrency plumbing — kept for the pool API. */
export function setCaptchaSolverConcurrency(_n: number): void {}

/** Nothing long-lived to shut down: workers are terminated per solve. */
export function shutdownCaptchaSolver(): void {}

export function captchaSolverConcurrency(): number {
  return Number(process.env.CAPTCHA_DAEMON_CONCURRENCY || 4);
}
