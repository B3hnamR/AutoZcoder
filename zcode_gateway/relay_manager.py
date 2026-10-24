"""Relay manager for edge proxies (Cloudflare, Vercel, Deno, Netlify).

Manages deployed relays, testing, active relay selection, model-level
relay routing (defaulting to GLM-5.3), and rotation strategies:

    single    — always the active relay (legacy behavior)
    round_robin — cycle through all relays in order, one request at a time
    random    — pick a random relay per request

Any strategy falls back to the next relay when one fails (connection error
or a 5xx/429 from the relay itself); failed relays re-enter rotation after
a cool-down.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from .relay_deploy import (
    deploy_cloudflare,
    deploy_deno,
    deploy_netlify,
    deploy_vercel,
    ping_relay,
)

STRATEGIES = ("single", "round_robin", "random")
FAIL_COOLDOWN_S = 60.0          # a failed relay sits out one minute


class RelayManager:
    """Manages serverless edge relay configurations and model routing."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path("./generator-data")
        self.path = self.data_dir / "relays.json"
        # RLock: add/delete/update hold it across their whole
        # read-modify-write AND call get_settings/save_settings, which take
        # it again internally — a plain Lock would self-deadlock.
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._rr_index = 0                 # round-robin cursor
        self._failed: dict[str, float] = {}  # relay id -> cooldown-unix-ts
        # notification center (optional, attached in app setup): relay
        # cooldowns surface in the bell
        self._notify = None
        # background health loop: ping every relay on a timer, feed failures
        # into the rotation cooldown, keep last_* fields fresh for the table
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()

    def start_health_loop(self, interval_s: int = 300) -> None:
        """Ping all relays every interval (default 5 min). Idempotent."""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_stop.clear()
        self._health_interval = max(60, int(interval_s))

        def _loop():
            while not self._health_stop.wait(getattr(self, '_health_interval', 300)):
                try:
                    self.ping_all()
                except Exception:
                    pass

        self._health_thread = threading.Thread(target=_loop, daemon=True)
        self._health_thread.start()

    def ping_all(self) -> dict[str, dict]:
        """Ping every configured relay once; update health fields + cooldowns."""
        st = self.get_settings()
        out: dict[str, dict] = {}
        for r in st.get("relays") or []:
            url = r.get("url") or ""
            if not url:
                continue
            try:
                res = ping_relay(url)
                r["last_status"] = res.get("status")
                r["last_error"] = res.get("error")
                r["last_latency_ms"] = res.get("elapsed_ms")
                r["last_tested_at"] = time.time()
                if res.get("ok"):
                    self.mark_relay_ok(url)
                else:
                    self.mark_relay_failed(url)
                out[r["id"]] = {"ok": bool(res.get("ok")),
                                "latency_ms": res.get("elapsed_ms")}
            except Exception as e:
                r["last_status"] = None
                r["last_error"] = str(e)[:120]
                r["last_tested_at"] = time.time()
                self.mark_relay_failed(url)
                out[r["id"]] = {"ok": False, "latency_ms": None}
        self.save_settings(st)
        return out

    def _default_settings(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "active_url": "",
            "active_id": "",
            "models": ["GLM-5.3"],  # Default to GLM-5.3 only as required
            "strategy": "single",   # single | round_robin | random
            "relays": [],
        }

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        settings = self._default_settings()
                        settings.update(data)
                        self._cache = settings
                        return dict(settings)
                except Exception:
                    pass
            settings = self._default_settings()
            self._cache = settings
            return dict(settings)

    def save_settings(self, settings: dict[str, Any]) -> None:
        with self._lock:
            self._cache = settings
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def _model_matches(self, model: str, models: list[str]) -> bool:
        norm = model.lower().strip()
        lowered = [m.lower() for m in models]
        if norm in lowered or model in models:
            return True
        # Also check alias normalization like glm-5.3 vs GLM-5.3
        if "glm-5.3" in norm and "flash" not in norm and "glm-5.3" in lowered:
            return True
        if "flash" in norm and "glm-5.3-flash" in lowered:
            return True
        return False

    def _failed_keys(self) -> set[str]:
        """Identifiers of relays in failure cool-down (ids AND urls — callers
        know the url at failure time, the id at pick time)."""
        now = time.time()
        with self._lock:
            expired = [k for k, until in self._failed.items() if until <= now]
            for k in expired:
                self._failed.pop(k, None)
            return set(self._failed)

    def _available_relays(self, st: dict[str, Any]) -> list[dict[str, Any]]:
        """Relays not currently in failure cool-down."""
        failed = self._failed_keys()
        return [r for r in st.get("relays") or []
                if r.get("id") not in failed and r.get("url") not in failed]

    def _pick_relay(self, st: dict[str, Any]) -> dict[str, Any] | None:
        """Pick the next relay per the configured strategy."""
        strategy = (st.get("strategy") or "single").lower()
        relays = self._available_relays(st)
        if not relays:
            # everything cooling down: fall back to the full list rather
            # than dropping the relay path entirely
            relays = list(st.get("relays") or [])
            if not relays:
                return None

        if strategy == "round_robin" and len(relays) > 1:
            with self._lock:
                self._rr_index %= len(relays)
                pick = relays[self._rr_index]
                self._rr_index += 1
            return pick
        if strategy == "random" and len(relays) > 1:
            return random.choice(relays)
        # single (or 1 relay left): the active relay, else the first
        active_id = st.get("active_id")
        for r in relays:
            if r.get("id") == active_id:
                return r
        return relays[0]

    def mark_relay_failed(self, relay_url: str) -> None:
        """A relay errored: cool it down so rotation skips it for a while."""
        if not relay_url:
            return
        with self._lock:
            first_fail = relay_url not in self._failed
            self._failed[relay_url] = time.time() + FAIL_COOLDOWN_S
        if first_fail and self._notify is not None:
            try:
                host = relay_url.split("//")[-1].split("/")[0]
                self._notify.emit("relay_cooled",
                                  f"relay {host} cooled {FAIL_COOLDOWN_S:.0f}s "
                                  f"after a failure — rotation switched",
                                  level="warning")
            except Exception:
                pass

    def mark_relay_ok(self, relay_url: str) -> None:
        with self._lock:
            self._failed.pop(relay_url, None)

    def get_relay_count(self) -> int:
        return len(self.get_settings().get("relays") or [])

    def get_relay_for_model(self, model: str) -> str | None:
        """Return a relay URL (per strategy) if enabled and applicable for this model."""
        if not model:
            return None
        st = self.get_settings()
        if not st.get("enabled"):
            return None
        relays = st.get("relays") or []
        if not relays:
            active_url = (st.get("active_url") or "").strip()
            return active_url or None
        if not self._model_matches(model, st.get("models") or []):
            return None
        pick = self._pick_relay(st)
        url = ((pick or {}).get("url") or "").strip()
        return url or (st.get("active_url") or "").strip() or None

    def add_relay(self, name: str, url: str, type_name: str = "custom") -> dict[str, Any]:
        # Hold the lock across the whole read-modify-write: a concurrent
        # delete_relay building its filtered list between our get and save
        # used to drop the just-added relay from the persisted file.
        with self._lock:
            st = self.get_settings()
            # uuid4: timestamp-based ids collided when two adds landed in the same
            # millisecond (delete then removed both)
            import uuid as _uuid
            relay_id = f"relay-{_uuid.uuid4().hex[:12]}"
            entry = {
                "id": relay_id,
                "name": name or f"{type_name}-relay",
                "type": type_name,
                "url": url.strip(),
                "created_at": time.time(),
                "last_status": None,
                "last_error": None,
                "last_latency_ms": None,
                "last_tested_at": None,
            }
            st["relays"].append(entry)
            if not st.get("active_url"):
                st["active_url"] = entry["url"]
                st["active_id"] = entry["id"]
            self.save_settings(st)
            return entry

    def delete_relay(self, relay_id: str) -> bool:
        # Same whole-operation lock as add_relay (see above).
        with self._lock:
            st = self.get_settings()
            orig_len = len(st["relays"])
            st["relays"] = [r for r in st["relays"] if r.get("id") != relay_id]
            if len(st["relays"]) == orig_len:
                return False
            if st.get("active_id") == relay_id:
                if st["relays"]:
                    st["active_id"] = st["relays"][0]["id"]
                    st["active_url"] = st["relays"][0]["url"]
                else:
                    st["active_id"] = ""
                    st["active_url"] = ""
            self.save_settings(st)
            return True

    def delete_relays(self, ids=None, *, all_relays: bool = False) -> int:
        """Remove several relays in ONE read-modify-write (a loop of
        delete_relay() would re-save the file per relay and race the health
        loop). `all_relays=True` empties the list. Returns how many went."""
        with self._lock:
            st = self.get_settings()
            before = st["relays"]
            wanted = {str(i) for i in (ids or [])}
            st["relays"] = [] if all_relays else [r for r in before if r.get("id") not in wanted]
            removed = len(before) - len(st["relays"])
            if not removed:
                return 0
            if not any(r.get("id") == st.get("active_id") for r in st["relays"]):
                if st["relays"]:
                    st["active_id"] = st["relays"][0]["id"]
                    st["active_url"] = st["relays"][0]["url"]
                else:
                    st["active_id"] = ""
                    st["active_url"] = ""
            keep = {r.get("id") for r in st["relays"]} | {r.get("url") for r in st["relays"]}
            for key in list(self._failed):          # cooldown keys are ids AND urls
                if key not in keep:
                    self._failed.pop(key, None)
            self.save_settings(st)
            return removed

    def set_active(self, relay_id: str) -> bool:
        st = self.get_settings()
        for r in st["relays"]:
            if r.get("id") == relay_id:
                st["active_id"] = r["id"]
                st["active_url"] = r["url"]
                self.save_settings(st)
                return True
        return False

    def update_options(self, models: list[str] | None = None,
                       enabled: bool | None = None,
                       active_id: str | None = None,
                       strategy: str | None = None) -> dict[str, Any]:
        # Apply ALL provided fields to one snapshot, then save once.
        # The old flow delegated active_id to set_active(), which re-read the
        # cache BEFORE this call's models/enabled were persisted — so saving
        # options with an active relay silently discarded the toggles.
        # Whole read-modify-write under the lock (see add_relay).
        with self._lock:
            st = self.get_settings()
            if models is not None:
                st["models"] = list(dict.fromkeys(models))
            if enabled is not None:
                st["enabled"] = bool(enabled)
            if strategy is not None:
                s = str(strategy).lower().strip()
                if s not in STRATEGIES:
                    raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
                st["strategy"] = s
            if active_id:
                for r in st["relays"]:
                    if r.get("id") == active_id:
                        st["active_id"] = r["id"]
                        st["active_url"] = r["url"]
                        break
            self.save_settings(st)
            return st

    def test_relay_by_id(self, relay_id: str) -> dict[str, Any]:
        st = self.get_settings()
        target = next((r for r in st["relays"] if r.get("id") == relay_id), None)
        if not target:
            return {"ok": False, "error": "Relay not found"}
        res = ping_relay(target["url"])
        target["last_status"] = res.get("status")
        target["last_error"] = res.get("error")
        target["last_latency_ms"] = res.get("elapsed_ms")
        target["last_tested_at"] = time.time()
        self.save_settings(st)
        return res

    def deploy_and_add(self, relay_type: str, params: dict[str, Any]) -> dict[str, Any]:
        relay_type = (relay_type or "").lower().strip()
        if relay_type == "cloudflare":
            res = deploy_cloudflare(
                account_id=params.get("accountId") or params.get("account_id") or "",
                api_token=params.get("apiToken") or params.get("api_token") or "",
                project_name=params.get("projectName") or params.get("project_name") or "",
            )
        elif relay_type == "vercel":
            res = deploy_vercel(
                vercel_token=params.get("vercelToken") or params.get("token") or "",
                project_name=params.get("projectName") or params.get("project_name") or "",
            )
        elif relay_type == "deno":
            res = deploy_deno(
                deno_token=params.get("denoToken") or params.get("token") or "",
                org_domain=params.get("orgDomain") or params.get("org_domain") or "",
                app_name=params.get("projectName") or params.get("app_name") or "",
            )
        elif relay_type == "netlify":
            res = deploy_netlify(
                netlify_token=params.get("netlifyToken") or params.get("token") or "",
                site_name=params.get("projectName") or params.get("site_name") or "",
            )
        else:
            raise ValueError(f"Unknown relay type: {relay_type}")

        entry = self.add_relay(
            name=res.get("name", f"{relay_type}-relay"),
            url=res.get("url", ""),
            type_name=relay_type,
        )
        return {"ok": True, "relay": entry}
