/**
 * mint.ts — print one Aliyun verify param for the ZCode billing claim.
 *
 *   bun run mint.ts        ->  "<verifyParam> <region>"
 *
 * Config (sceneId/prefix/region) is read from ZCode's own client config, the
 * same source captcha.ts uses. Env overrides for debugging:
 *   ZAI_APP_VERSION, ZAI_CAPTCHA_SCENE, ZAI_CAPTCHA_PREFIX, ZAI_CAPTCHA_REGION
 *
 * The Python side (generator/claim_start.py `_captcha_token`) invokes this via
 * ZAI_CAPTCHA_CMD and parses the single stdout line.
 */
import { runCaptchaSolve } from "./captcha-solver.ts";
import { CaptchaDeadline } from "./captcha-deadline.ts";
import { bootstrapCaptchaRoute, readBoundedBody } from "./captcha-transport.ts";

const appVersion = (process.env.ZAI_APP_VERSION || "3.16.3").trim();

export async function readConfig(deadline: CaptchaDeadline): Promise<{ scene: string; region: string; prefix: string }> {
  deadline.remaining();
  const envScene = (process.env.ZAI_CAPTCHA_SCENE || "").trim();
  const envPrefix = (process.env.ZAI_CAPTCHA_PREFIX || "").trim();
  const envRegion = (process.env.ZAI_CAPTCHA_REGION || "").trim();
  if (envScene && envPrefix) {
    return { scene: envScene, region: envRegion || "sgp", prefix: envPrefix };
  }
  const url =
    "https://zcode.z.ai/api/v1/client/configs" +
    `?app_version=${encodeURIComponent(appVersion)}&platform=win32-x64`;
  const json = (await deadline.fetch(url, {}, async (resp) => {
    if (!resp.ok) throw new Error(`captcha config HTTP ${resp.status}`);
    return JSON.parse((await readBoundedBody(resp, deadline.controller.signal)).toString("utf8"));
  })) as {
    data?: { configs?: { captcha?: { enabled?: boolean; prefix?: string; sceneId?: string; region?: string } } };
  };
  const cfg = json?.data?.configs?.captcha;
  if (!cfg?.enabled || !cfg.sceneId || !cfg.prefix) {
    throw new Error(`captcha config unavailable: ${JSON.stringify(cfg ?? null)}`);
  }
  return {
    scene: cfg.sceneId,
    region: cfg.region || "sgp",
    prefix: cfg.prefix,
  };
}

export async function main(): Promise<void> {
  bootstrapCaptchaRoute();
  const deadline = new CaptchaDeadline();
  try {
    const cfg = await readConfig(deadline);
    if (process.env.CAPTCHA_MINT_DEBUG) {
      process.stderr.write(`[mint] scene=${cfg.scene} region=${cfg.region} prefix=${cfg.prefix}\n`);
    }
    const param = await deadline.run(() => runCaptchaSolve(cfg.scene, cfg.region, cfg.prefix, deadline.at));
    if (!param || param.length <= 20 || /\s/.test(param) || !/^[A-Za-z0-9_-]+$/.test(cfg.region)) {
      throw new Error("solver returned invalid token protocol");
    }
    process.stdout.write(`${param} ${cfg.region}\n`);
  } finally {
    deadline.dispose();
  }
}

if (import.meta.main) main().then(
  () => process.exit(0),
  (err) => {
    process.stderr.write(`[mint] ${(err as Error)?.stack || String(err)}\n`);
    process.exit(1);
  },
);
