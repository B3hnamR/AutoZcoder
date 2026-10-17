"""Detect and configure local AI clients that talk to this gateway.

Why this exists: reasoning/thinking pickers in clients like Kilo Code are
driven by *client-side* model metadata, not by `/v1/models`. Kilo reads only
`id` and `name` from the endpoint (see its `fetch-models.ts`), and shows the
thinking selector only when a model declares `reasoning: true` — then it
auto-generates the effort ladder itself:

    packages/opencode/src/kilocode/provider/provider.ts
        if (!model.capabilities.reasoning || !supported) return variants
        return FALLBACK_EFFORTS.map(effort => [effort, { reasoningEffort: effort }])

So the buttons appear when the model entry in `kilo.jsonc` carries
`"reasoning": true`. This module finds providers that point at our gateway and
adds that flag (plus explicit variants for the kimi-k3 family, whose ladder is
narrower than Kilo's default), without touching anything else in the file.

The file is JSONC (comments + trailing commas), so it is patched textually:
every edit is a minimal, verified string insertion. A backup is written once
before the first change.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("zcode_gateway.client_sync")

# Local endpoints that mean "this provider is our gateway"
GATEWAY_HOST_MARKERS = ("127.0.0.1", "localhost", "0.0.0.0")

# Model families whose thinking ladder we know, and what to offer for them.
# Everything else gets `reasoning: true` only, letting the client generate its
# own ladder (our gateway normalises the effort values it sends).
KNOWN_LADDERS = {"glm-5.3": {
    "low": {"reasoning_effort": "low"},
    "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "max"}}}
NON_REASONING_MARKERS = ()


def _is_gateway_url(url: str, port: int | None = None) -> bool:
    """True when url points at a loopback host (i.e. plausibly this gateway).

    Parse the host rather than substring-matching: 'localhost.evil.com'
    contains 'localhost' but is not a loopback address.
    """
    if not url:
        return False
    from urllib.parse import urlparse
    try:
        host = (urlparse(url if "://" in url else f"http://{url}").hostname
                or "").lower()
    except ValueError:
        return False
    if host not in GATEWAY_HOST_MARKERS:
        return False
    if port is None:
        return True
    try:
        return (urlparse(url if "://" in url else f"http://{url}").port
                == port)
    except ValueError:
        return False


def _ladder_for(model_id: str) -> dict | None:
    low = model_id.lower()
    for family, ladder in KNOWN_LADDERS.items():
        if family in low:
            return ladder
    return None


def _should_flag(model_id: str) -> bool:
    low = model_id.lower()
    if any(marker in low for marker in NON_REASONING_MARKERS):
        return False
    return True


@dataclass
class KiloStatus:
    """What we found in the client's config."""

    found: bool = False
    path: str = ""
    gateway_providers: list[str] = field(default_factory=list)
    models_total: int = 0
    models_flagged: int = 0
    models_missing_flag: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def needs_sync(self) -> bool:
        return bool(self.models_missing_flag)


def kilo_config_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".config" / "kilo" / "kilo.jsonc",
        home / ".config" / "kilo" / "kilo.json",
    ]


def find_kilo_config() -> Path | None:
    for path in kilo_config_paths():
        if path.is_file():
            return path
    return None


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments and trailing commas so json can parse it."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r"(?<!:)\s*//[^\n\"]*$", "", text, flags=re.M)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _provider_blocks(text: str) -> list[tuple[str, int, int]]:
    """(provider_name, start, end) for each provider object in the file.

    Scans the provider map brace-by-brace and reads each key from the quoted
    string immediately before its opening brace.
    """
    out: list[list] = []
    anchor = text.find('"provider"')
    if anchor < 0:
        return []
    brace = text.find("{", anchor)
    if brace < 0:
        return []

    i = brace + 1
    depth = 1
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == '"':                      # skip a string literal
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "{":
            depth += 1
            if depth == 2:
                j = i - 1
                while j > brace and text[j] in " \t\r\n:":
                    j -= 1
                if j > brace and text[j] == '"':
                    k = j - 1
                    while k > brace and text[k] != '"':
                        k -= 1
                    out.append([text[k + 1:j], i, i])
        elif ch == "}":
            depth -= 1
            if depth == 1 and out:
                out[-1][2] = i
        i += 1
    return [(name, start, end) for name, start, end in out]


def inspect_kilo(path: Path | None = None) -> KiloStatus:
    """Report whether Kilo exists and which gateway models lack reasoning."""
    status = KiloStatus()
    path = path or find_kilo_config()
    if path is None or not Path(path).is_file():
        return status
    status.found = True
    status.path = str(path)

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(_strip_comments(text))
    except Exception as exc:
        status.error = f"{exc.__class__.__name__}: {exc}"
        return status

    providers = (data.get("provider") or {})
    if not isinstance(providers, dict):
        return status

    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        url = str((cfg.get("options") or {}).get("baseURL") or "")
        if not _is_gateway_url(url):
            continue
        status.gateway_providers.append(name)
        models = cfg.get("models") or {}
        if not isinstance(models, dict):
            continue
        for model_id, model_cfg in models.items():
            if not isinstance(model_cfg, dict):
                continue
            status.models_total += 1
            has_flag = bool(model_cfg.get("reasoning")) or bool(model_cfg.get("variants"))
            if has_flag:
                status.models_flagged += 1
            elif _should_flag(model_id):
                status.models_missing_flag.append(model_id)
    return status


def _insert_model_flags(text: str, provider: str, models: dict[str, dict]) -> tuple[str, int]:
    """Insert `reasoning: true` (+ variants) into each model object.

    Textual and minimal: finds `"<model_id>": {` inside the provider block and
    inserts the fields right after the opening brace, leaving formatting and
    comments untouched.
    """
    blocks = {name: (start, end) for name, start, end in _provider_blocks(text)}
    if provider not in blocks:
        return text, 0
    start, end = blocks[provider]
    segment = text[start:end]

    changed = 0
    for model_id, fields in models.items():
        pattern = re.compile(
            r'("' + re.escape(model_id) + r'"\s*:\s*\{)')
        m = pattern.search(segment)
        if not m:
            continue
        insertion = "\n        " + ",\n        ".join(
            f'"{k}": {json.dumps(v)}' for k, v in fields.items())
        segment = segment[:m.end()] + insertion + "," + segment[m.end():]
        changed += 1
    if not changed:
        return text, 0
    return text[:start] + segment + text[end:], changed


def sync_kilo(path: Path | None = None, *, dry_run: bool = False) -> dict:
    """Add reasoning metadata to every gateway model in Kilo's config.

    Returns {changed, models, backup, path}. Writes a one-time backup before
    the first modification.
    """
    status = inspect_kilo(path)
    if not status.found:
        return {"changed": 0, "models": [], "backup": None, "path": "",
                "error": "Kilo config not found"}
    if status.error:
        return {"changed": 0, "models": [], "backup": None,
                "path": status.path, "error": status.error}
    if not status.needs_sync:
        return {"changed": 0, "models": [], "backup": None,
                "path": status.path, "error": ""}

    target = Path(status.path)
    text = target.read_text(encoding="utf-8")

    to_add: dict[str, dict] = {}
    for model_id in status.models_missing_flag:
        entry: dict = {"reasoning": True}
        ladder = _ladder_for(model_id)
        if ladder:
            entry["variants"] = ladder
        to_add[model_id] = entry

    if dry_run:
        return {"changed": 0, "models": sorted(to_add), "backup": None,
                "path": status.path, "error": ""}

    total_changed = 0
    for provider in status.gateway_providers:
        text, changed = _insert_model_flags(text, provider, to_add)
        total_changed += changed
    if not total_changed:
        return {"changed": 0, "models": [], "backup": None,
                "path": status.path,
                "error": "no model blocks matched — left untouched"}

    # verify before writing: the patched file must still parse
    try:
        json.loads(_strip_comments(text))
    except Exception as exc:
        return {"changed": 0, "models": [], "backup": None, "path": status.path,
                "error": f"refusing to write invalid config: {exc}"}

    backup = None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = target.with_name(f"{target.name}.bak-{stamp}")
    try:
        shutil.copyfile(target, backup_path)
        backup = str(backup_path)
        # atomic write: a crash mid-`write_text` would corrupt the user's
        # config; write a sibling temp file and os.replace it into place
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        return {"changed": 0, "models": [], "backup": backup,
                "path": status.path, "error": f"write failed: {exc}"}

    return {"changed": total_changed, "models": sorted(to_add), "backup": backup,
            "path": status.path, "error": ""}
