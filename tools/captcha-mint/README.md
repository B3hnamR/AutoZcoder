# captcha-mint

Standalone Aliyun traceless-captcha minter for the ZCode billing claim.

The solver sources in this folder (`captcha-*.ts`) are vendored from
**TriDefender/zcode-api** (https://github.com/TriDefender/zcode-api), which the
project distributes under **CC BY-NC-SA**. They are unmodified except that the
worker-thread bundle is not built here, so the solver falls back to in-process
happy-dom (same code path, just without the worker isolation).

`mint.ts` is ours: it reads ZCode's own captcha config and prints

    <verifyParam> <region>

so `generator/claim_start.py` can pass it to `billing/claim` as
`x-aliyun-captcha-verify-param` / `-region`.

## build

```powershell
bun install
bun build --compile --outfile captcha-mint.exe mint.ts
```

`build-exe.ps1` does both automatically and bundles the resulting exe at
`captcha-mint/captcha-mint.exe` inside the PyInstaller onefile.
