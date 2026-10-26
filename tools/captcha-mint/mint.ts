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

const appVersion = (process.env.ZAI_APP_VERSION || "3.16.3").trim();

async function readConfig(): Promise<{ scene: string; region: string; prefix: string }> {
  const envScene = (process.env.ZAI_CAPTCHA_SCENE || "").trim();
  const envPrefix = (process.env.ZAI_CAPTCHA_PREFIX || "").trim();
  const envRegion = (process.env.ZAI_CAPTCHA_REGION || "").trim();
  if (envScene && envPrefix) {
    return { scene: envScene, region: envRegion || "sgp", prefix: envPrefix };
  }
  const url =
    "https://zcode.z.ai/api/v1/client/configs" +
    `?app_version=${encodeURIComponent(appVersion)}&platform=win32-x64`;
  const resp = await fetch(url);
  const json = (await resp.json()) as {
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

async function main(): Promise<void> {
  const cfg = await readConfig();
  if (process.env.CAPTCHA_MINT_DEBUG) {
    process.stderr.write(`[mint] scene=${cfg.scene} region=${cfg.region} prefix=${cfg.prefix}\n`);
  }
  const param = await runCaptchaSolve(cfg.scene, cfg.region, cfg.prefix);
  if (!param || param.length < 20) throw new Error("solver returned no verify param");
  process.stdout.write(`${param} ${cfg.region}\n`);
}

main().then(
  () => process.exit(0),
  (err) => {
    process.stderr.write(`[mint] ${(err as Error)?.stack || String(err)}\n`);
    process.exit(1);
  },
);
