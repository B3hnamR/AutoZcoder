/**
 * captcha-worker-dispatch.ts — solve execution: worker thread with an
 * in-process fallback.
 *
 * Each solve runs in its own worker_threads Worker so the proxy's main event
 * loop never blocks on the solver's Atomics.wait sync XHRs — in-process
 * solving froze every connection during refill bursts (issue #54, PR #55).
 * Worker-per-solve also isolates happy-dom's global browser-frame/cookie
 * state per solve, removing cross-solve races.
 *
 * The worker entry is a build-time FILE ASSET (captcha-worker-asset.ts —
 * `bun build --compile` cannot resolve `new Worker(new URL(...))` at
 * runtime). Resolution here is dynamic and may legitimately fail: the asset
 * exists only after scripts/build-fork-worker.ts runs, and the esbuild
 * Android bundle marks the asset module external. On such deployments we
 * solve IN-PROCESS instead (the pre-worker behavior, still bounded by the
 * CAPTCHA_SYNC_FETCH_TIMEOUT_MS / CAPTCHA_SOLVE_TIMEOUT_MS caps). Mode
 * transitions are announced once on stderr for operators.
 */
import { Worker } from "node:worker_threads";

/** Per-solve timeout: overall deadline the worker gets before termination. */
const SOLVE_WORKER_TIMEOUT_MS = Number(process.env.CAPTCHA_SOLVE_TIMEOUT_MS || 20_000);

interface SolveRequest {
  id: number;
  scene: string;
  region: string;
  prefix: string;
}
type SolveResponse = { id: number; ok: true; param: string } | { id: number; ok: false; error: string };

let nextSolveId = 0;

// Lazy-loaded in-process solver — imported only on the fallback path so
// processes that never solve (coding-plan) never pay the happy-dom startup.
type InProcessSolveFn = (opts: {
  scene: string;
  region: string;
  prefix: string;
}) => Promise<string>;
let inProcessOverride: InProcessSolveFn | null = null;
let happyMod: { solveTraceless: InProcessSolveFn } | null = null;
async function happy(): Promise<{ solveTraceless: InProcessSolveFn }> {
  if (inProcessOverride) return { solveTraceless: inProcessOverride };
  if (!happyMod) {
    happyMod = (await import("./captcha-happy.js")) as { solveTraceless: InProcessSolveFn };
  }
  return happyMod;
}

let entryPathCache: string | null | undefined;
let lastNotedMode = "";

/** Announce solve-mode TRANSITIONS only (worker <-> in-process), once each. */
function noteMode(mode: string, line: string): void {
  if (mode === lastNotedMode) return;
  lastNotedMode = mode;
  try { process.stderr.write(line); } catch {}
}

/**
 * Resolve the pre-bundled worker entry (build-time file asset). Cached
 * because a miss is permanent for the process lifetime: the bundle is a
 * build input that cannot appear while running. Never throws.
 */
async function getWorkerEntryPath(): Promise<string | null> {
  if (entryPathCache !== undefined) return entryPathCache;
  try {
    const m = await import("./captcha-worker-asset.js");
    const p = (m as { default?: unknown }).default;
    entryPathCache = typeof p === "string" && p ? p : null;
  } catch {
    entryPathCache = null;
  }
  if (entryPathCache) {
    noteMode("worker", "[captcha-solver] worker-thread solving active\n");
  } else {
    noteMode(
      "in-process",
      "[captcha-solver] worker entry unavailable — solving in-process " +
        "(run scripts/build-fork-worker.ts / bun run build to embed workers)\n",
    );
  }
  return entryPathCache;
}

/** Worker-entry unusable — the only worker failure that falls back. */
class WorkerUnavailableError extends Error {}

/** Load-stage failures (entry missing/unloadable); runtime crashes do NOT match. */
function isEntryUnavailableError(err: unknown): boolean {
  const e = err as { code?: string; message?: string };
  return (
    e?.code === "ERR_MODULE_NOT_FOUND" ||
    /cannot find module|module not found/i.test(String(e?.message ?? ""))
  );
}

export async function solveViaWorkerOrInProcess(req: {
  scene: string;
  region: string;
  prefix: string;
}): Promise<string> {
  const entryPath = await getWorkerEntryPath();
  if (entryPath === null) {
    return (await happy()).solveTraceless(req);
  }
  try {
    return await solveInWorker(entryPath, req);
  } catch (err) {
    if (err instanceof WorkerUnavailableError) {
      return (await happy()).solveTraceless(req);
    }
    throw err;
  }
}

/**
 * One solve = one Worker. Startup cost is a few ms (happy-dom loads lazily
 * inside the entry on first message); termination guarantees no state leaks
 * between solves. A hung solve cannot wedge anything: the pool's takeToken
 * race deadline (25s) fires first, and the worker is force-terminated here.
 */
function solveInWorker(
  entryPath: string,
  req: { scene: string; region: string; prefix: string },
): Promise<string> {
  const id = ++nextSolveId;
  return new Promise<string>((resolve, reject) => {
    let settled = false;
    let worker: Worker | null = null;
    const settle = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { worker?.terminate(); } catch {}
      fn();
    };
    const timer = setTimeout(() => {
      settle(() => reject(new Error(`captcha worker timeout (${SOLVE_WORKER_TIMEOUT_MS}ms)`)));
    }, SOLVE_WORKER_TIMEOUT_MS);

    try {
      worker = new Worker(entryPath);
    } catch (err) {
      const msg = `captcha worker spawn failed: ${(err as Error).message}`;
      noteMode("in-process", `[captcha-solver] ${msg} — degrading to in-process solving\n`);
      settle(() => reject(new WorkerUnavailableError(msg)));
      return;
    }
    const msg: SolveRequest = { id, ...req };
    worker.on("message", (m: SolveResponse) => {
      if (!m || m.id !== id) return;
      if (m.ok) settle(() => resolve(m.param));
      else settle(() => reject(new Error(m.error)));
    });
    worker.on("error", (err: Error) => {
      // A load-stage failure (entry unloadable) means workers are unusable in
      // this deployment — degrade. Anything else (crash/OOM inside a loaded
      // worker) stays a hard failure: retrying it on the MAIN thread is the
      // #54/#50 failure shape, and the pool's retry ladder rolls a fresh
      // worker instead.
      if (isEntryUnavailableError(err)) {
        noteMode(
          "in-process",
          `[captcha-solver] captcha worker entry failed to load: ${err.message} — degrading to in-process solving\n`,
        );
        settle(() => reject(new WorkerUnavailableError(`captcha worker entry failed to load: ${err.message}`)));
      } else {
        settle(() => reject(new Error(`captcha worker error: ${err.message}`)));
      }
    });
    worker.on("exit", (code) => {
      if (code !== 0 && !settled) {
        settle(() => reject(new Error(`captcha worker exited (code ${code}) before solving`)));
      } else if (!settled) {
        settle(() => reject(new Error("captcha worker exited before responding")));
      }
    });
    worker.postMessage(msg);
  });
}

/** Test-only: clear the entry/happy caches so dispatch order can be re-run. */
export function __resetCaptchaWorkerDispatchForTest(): void {
  entryPathCache = undefined;
  happyMod = null;
  lastNotedMode = "";
}

/**
 * Test-only: substitute the in-process solver. Tests must use this seam
 * instead of mock.module("./captcha-happy.js", …) — a module mock is
 * process-wide in Bun and leaks a PARTIAL export surface into whichever
 * test file loads captcha-happy afterwards (breaking e.g. __captchaMemStats
 * importers, order-dependently across platforms).
 */
export function __setInProcessSolverForTest(fn: InProcessSolveFn | null): void {
  inProcessOverride = fn;
}
