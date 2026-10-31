#!/usr/bin/env bash
# linux.sh - package the Zcode Gateway headless server as a single
# Linux binary (plus the captcha-mint sidecar).
#
#   ./scripts/build/linux.sh           build
#   ./scripts/build/linux.sh --clean   wipe .local/build/ .local/dist/ first
#
# Output:
#   .local/dist/ZcodeGateway          onefile headless server (config + dashboard
#                              identical to the Windows build; no WebView shell)
#   .local/dist/captcha-mint/captcha-mint   standalone sidecar for source runs
#
# Run ON Linux (PyInstaller cannot cross-compile). CI: release.yml build-linux.

set -euo pipefail
SCRIPT_PATH="$(realpath -- "${BASH_SOURCE[0]}")"
ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/../.." && pwd -P)"
cd -- "$ROOT"

echo "Zcode Gateway - linux build"

[[ "$(uname -s)" == "Linux" ]] || { echo "linux.sh must run on Linux" >&2; exit 1; }

if [[ "${1:-}" == "--clean" ]]; then
    rm -rf .local/build .local/dist
fi

# 1. Require a native sidecar before installing Python build dependencies.
# Never substitute the Windows .exe or quietly ship reduced functionality.
MINT_DIR="tools/captcha-mint"
if [[ -f "$MINT_DIR/mint.ts" ]]; then
    if command -v bun >/dev/null 2>&1; then
        echo "  compiling captcha-mint (bun, $(uname -m))..."
        case "$(uname -m)" in
            x86_64|amd64) BUN_ARCH=x64 ;;
            aarch64|arm64) BUN_ARCH=arm64 ;;
            *) echo "Unsupported Bun architecture: $(uname -m)" >&2; exit 1 ;;
        esac
        (cd "$MINT_DIR"
         [[ -d node_modules ]] || bun install
          bun build --compile --target=bun-linux-"$BUN_ARCH" \
             --outfile captcha-mint mint.ts)
    else
        echo "  bun not found - a prebuilt native Linux captcha-mint is required"
    fi
fi
MINT_BIN="$MINT_DIR/captcha-mint"
[[ -f "$MINT_BIN" && -x "$MINT_BIN" ]] || {
    echo "Missing executable Linux captcha-mint. Install Bun and rerun, or supply a native prebuilt at $MINT_BIN (not .exe)." >&2
    exit 1
}
python3 - "$MINT_BIN" "$(uname -m)" <<'PY'
from pathlib import Path
import sys

path, arch = sys.argv[1:]
expected = {"x86_64": 62, "amd64": 62, "aarch64": 183, "arm64": 183}.get(arch)
with Path(path).open("rb") as fh:
    header = fh.read(20)
if (expected is None or len(header) != 20 or header[:7] != b"\x7fELF\x02\x01\x01"
        or header[7] not in (0, 3)
        or int.from_bytes(header[16:18], "little") not in (2, 3)
        or int.from_bytes(header[18:20], "little") != expected):
    sys.exit(f"{path} is not a compatible 64-bit Linux ELF executable for {arch}; rebuild it with Bun on this host.")
PY

# 2. python + deps (headless requirements: no webview/tkinter on this path)
PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
    python3 -m venv .venv
    "$PY" -m pip install --upgrade pip
fi
"$PY" -c "import PyInstaller" 2>/dev/null || "$PY" -m pip install pyinstaller
"$PY" -c "import yaml, httpx, fastapi, uvicorn, requests, socks" 2>/dev/null \
    || "$PY" -m pip install -r requirements/server.txt

# 3. stage generator sources (the live folder accumulates browser profiles,
# proxy ledgers and token caches — none of that ships)
GEN_STAGE="$(mktemp -d -t zcoder-generator-XXXXXX)"
MAIL_STAGE=""
trap 'rm -rf "$GEN_STAGE"; if [[ -n "$MAIL_STAGE" ]]; then rm -rf "$MAIL_STAGE"; fi' EXIT
MAIL_STAGE="$(mktemp -d -t zcoder-mail-XXXXXX)"
cp generator/*.py "$GEN_STAGE/"
for f in README.md requirements.txt; do
    [[ -f "generator/$f" ]] && cp "generator/$f" "$GEN_STAGE/"
done

# Whitelist worker sources and generate config without reading local secrets.
"$PY" scripts/packaging/stage_mail_worker.py mail-worker "$MAIL_STAGE"

# 4. pyinstaller onefile (headless entry: zcode_gateway/main.py)
"$PY" -m PyInstaller \
    --noconfirm --clean --onefile \
    --name ZcodeGateway \
    --collect-submodules uvicorn \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.http.h11_impl \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import uvicorn.lifespan.off \
    --hidden-import anyio._backends._asyncio \
    --hidden-import httpcore --hidden-import h11 \
    --hidden-import requests --hidden-import urllib3 \
    --hidden-import urllib3.contrib.socks --hidden-import socks --hidden-import certifi \
    --hidden-import yaml \
    --hidden-import zcode_gateway.generator \
    --hidden-import zcode_gateway.accounts_dir_bridge \
    --hidden-import fastapi.staticfiles \
    --add-data "$ROOT/config.example.yaml:." \
    --add-data "$ROOT/zcode_gateway/dashboard:zcode_gateway/dashboard" \
    --add-data "$MAIL_STAGE:mail-worker" \
    --add-data "$GEN_STAGE:generator" \
    --add-binary "$ROOT/$MINT_BIN:captcha-mint" \
    --paths "$ROOT" \
    --workpath .local/build --distpath .local/dist --specpath .local/build/specs \
    "$ROOT/zcode_gateway/main.py"

[[ -f .local/dist/ZcodeGateway ]] || { echo "expected output missing: .local/dist/ZcodeGateway" >&2; exit 1; }
chmod +x .local/dist/ZcodeGateway
mkdir -p .local/dist/captcha-mint
install -m 755 "$MINT_BIN" .local/dist/captcha-mint/captcha-mint
echo
echo "  built: .local/dist/ZcodeGateway"
echo "  Run it on the VPS:  ./ZcodeGateway   (binds 127.0.0.1 by default)"
echo "  To expose it, edit server.host in config.yaml — see docs/guides/VPS-DEPLOY.md"
echo "  for the required security setup before any non-loopback bind."
