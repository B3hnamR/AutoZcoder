"""Surfshark as an IP source.

Uses the account's own WireGuard key pair (Surfshark account → VPN → Manual
setup → WireGuard) and Surfshark's public server list. A bundled `wireproxy`
client runs once per chosen location, each offering a local SOCKS5 port; no
network adapter, route or DNS on this computer changes.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import vpn

ROUTE_ID = "surfshark"
BASE_PORT = 47400
DEFAULT_ADDRESS = "10.14.0.2/16"
DNS = "162.252.172.57"
WG_PORT = 51820
MTU = 1280
LIST_URLS = ["https://api.surfshark.com/v4/server/clusters/generic",
             "https://api.surfshark.com/v3/server/clusters"]

LOCATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


def public_key_of(private_key: str) -> str:
    """The WireGuard public key of a private key, so a mismatched pair is caught."""
    private_key = (private_key or "").strip()
    if not KEY_RE.match(private_key):
        raise ValueError("That is not a WireGuard private key: it is 44 characters "
                         "ending in \"=\". Copy it again from your Surfshark account.")
    raw = base64.b64decode(private_key, validate=True)
    if len(raw) != 32:
        raise ValueError("That is not a WireGuard private key (wrong length).")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    key = X25519PrivateKey.from_private_bytes(raw)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(public).decode()


def _place(server: dict) -> str:
    country = server.get("country") or server.get("id") or ""
    location = server.get("location") or ""
    if location and location != country:
        return f"{country} · {location}"
    return country


class SurfsharkPool(vpn.ExitPool):
    kind = ROUTE_ID
    label = "Surfshark"
    base_port = BASE_PORT
    defaults = {"enabled": True, "autoStart": False, "regions": [],
                "mode": "spread", "pinned": "", "privateKey": "", "publicKey": "",
                "address": DEFAULT_ADDRESS, "servers": [], "serverByRegion": {},
                "updatedAt": 0}

    def normalize(self, config: dict) -> dict:
        regions = config.get("regions")
        if isinstance(regions, list):
            config["regions"] = sorted({str(r).lower() for r in regions
                                        if LOCATION_ID.match(str(r).lower())})
        config["mode"] = "single" if config.get("mode") == "single" else "spread"
        pinned = str(config.get("pinned") or "").lower()
        config["pinned"] = pinned if LOCATION_ID.match(pinned) else ""
        if config["mode"] == "single" and config["pinned"] not in config["regions"]:
            config["pinned"] = (config["regions"] or [""])[0]
        servers = config.get("servers")
        config["servers"] = servers if isinstance(servers, list) else []
        mapping = config.get("serverByRegion")
        config["serverByRegion"] = mapping if isinstance(mapping, dict) else {}
        config["privateKey"] = str(config.get("privateKey") or "").strip()
        config["publicKey"] = str(config.get("publicKey") or "").strip()
        config["address"] = str(config.get("address") or DEFAULT_ADDRESS)
        return config

    def binary_name(self) -> str:
        if sys.platform == "win32":
            return "wireproxy-windows-amd64.exe"
        if sys.platform.startswith("linux"):
            machine = __import__("platform").machine().lower()
            if machine in ("aarch64", "arm64"):
                return "wireproxy-linux-arm64"
            if machine in ("x86_64", "amd64"):
                return "wireproxy-linux-amd64"
        return ""

    def missing_message(self) -> str:
        if not self.binary_name():
            return (f"Surfshark's WireGuard client has no build for {sys.platform} "
                    f"{__import__('platform').machine()}. It runs on Windows x64 and "
                    "Linux x64/arm64.")
        return ("The WireGuard client is missing from this build. It belongs in the "
                "app's surfshark folder (vendor/surfshark in a source checkout).")

    # ---- key pair -------------------------------------------------------
    @property
    def has_keys(self) -> bool:
        return bool(self.config.get("privateKey"))

    def keys_public(self) -> dict:
        cfg = self.config
        return {"saved": bool(cfg.get("privateKey")),
                "publicKey": cfg.get("publicKey", "") if cfg.get("privateKey") else ""}

    def set_keys(self, private_key: str, public_key: str = "") -> dict:
        private_key = (private_key or "").strip()
        public_key = (public_key or "").strip()
        if not private_key:
            raise ValueError("Paste the private key from your Surfshark account.")
        derived = public_key_of(private_key)
        if public_key and public_key != derived:
            raise ValueError("The public key does not belong to this private key. "
                             "Copy both from the same key pair in your Surfshark account.")
        with self._lock:
            replaced = bool(self._settings.get("privateKey")) and self._settings["privateKey"] != private_key
            self.set_settings({"privateKey": private_key, "publicKey": derived})
            if replaced:
                for region in tuple(self._instances):
                    self._start(region, fresh=True)
        return self.status()

    def clear_keys(self) -> dict:
        with self._lock:
            self.stop_all()
            for path in self.data_dir.glob("*/wireproxy*.conf"):
                path.unlink(missing_ok=True)
            self.set_settings({"privateKey": "", "publicKey": ""})
        return self.status()

    # ---- Surfshark's server list ---------------------------------------
    def servers(self) -> list[dict]:
        return self.config.get("servers") or []

    def server_of(self, server_id: str) -> dict | None:
        return next((s for s in self.servers() if s.get("id") == server_id), None)

    def server_for(self, region: str) -> dict | None:
        chosen = (self.config.get("serverByRegion") or {}).get(region)
        if chosen:
            found = self.server_of(chosen)
            if found:
                return found
        return self.server_of(region)

    @staticmethod
    def _parse_servers(payload) -> list[dict]:
        if not isinstance(payload, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            host = str(item.get("connectionName") or "")
            pub = str(item.get("pubKey") or "")
            if not re.match(r"^[a-z0-9-]+\.prod\.surfshark\.com$", host) or not KEY_RE.match(pub):
                continue
            server_id = host.split(".")[0]
            if not LOCATION_ID.match(server_id) or server_id in seen:
                continue
            seen.add(server_id)
            load = item.get("load")
            out.append({"id": server_id, "host": host, "pubKey": pub,
                        "country": str(item.get("country") or server_id),
                        "countryCode": str(item.get("countryCode") or "").upper()[:2],
                        "location": str(item.get("location") or ""),
                        "load": load if isinstance(load, (int, float)) else None})
        return sorted(out, key=lambda s: (s["country"], s["location"], s["id"]))

    def update(self, timeout: float = 30.0) -> dict:
        import requests
        with self._lock:
            self._busy = "updating servers"
        last = ""
        try:
            for url in LIST_URLS:
                try:
                    response = requests.get(url, timeout=timeout,
                                            headers={"User-Agent": "AutoZCoder"})
                    if response.status_code != 200:
                        last = f"HTTP {response.status_code}"
                        continue
                    listing = self._parse_servers(response.json())
                    if not listing:
                        last = "the list was empty"
                        continue
                    offered = {s["id"] for s in listing}
                    with self._lock:
                        cfg = self._settings
                        gone = [r for r in cfg.get("regions") or [] if r not in offered]
                        regions = [r for r in cfg.get("regions") or [] if r in offered]
                        pinned = cfg.get("pinned") if cfg.get("pinned") in offered else (regions or [""])[0]
                        patch = {"servers": listing, "updatedAt": int(time.time() * 1000)}
                        if gone:
                            patch.update({"regions": regions, "pinned": pinned})
                        self.set_settings(patch)
                    return self.status()
                except Exception as exc:
                    last = str(exc)[:120]
            raise RuntimeError(f"Surfshark's server list could not be read ({last}). "
                               "Check this computer's connection and try again.")
        finally:
            with self._lock:
                self._busy = ""

    # ---- client config --------------------------------------------------
    def region_dir(self, region: str) -> Path:
        return self.data_dir / region

    def _config_text(self, server: dict, port: int) -> str:
        cfg = self.config
        address = re.sub(r"/\d+$", "/32", str(cfg.get("address") or DEFAULT_ADDRESS))
        return "\n".join([
            "[Interface]", f"Address = {address}",
            f"PrivateKey = {cfg.get('privateKey', '')}",
            f"DNS = {DNS}", f"MTU = {MTU}", "",
            "[Peer]", f"PublicKey = {server['pubKey']}",
            f"Endpoint = {server['host']}:{WG_PORT}",
            "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25", "",
            "[Socks5]", f"BindAddress = 127.0.0.1:{port}", ""])

    def _prepare(self, inst: vpn.Exit) -> None:
        if not self.has_keys:
            raise RuntimeError("Save your Surfshark key pair first.")
        if self.find_client() is None:
            raise RuntimeError(self.missing_message())
        server = self.server_for(inst.region)
        if not server:
            raise RuntimeError(f"{inst.region} is not in Surfshark's server list. "
                               "Press Update servers.")
        inst.extra["server"] = server
        target = self.region_dir(inst.region)
        target.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=target, prefix="wireproxy-", suffix=".conf")
        inst.extra["config_file"] = name
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(self._config_text(server, inst.port))

    def _cleanup(self, inst: vpn.Exit) -> None:
        name = inst.extra.get("config_file")
        if not name:
            return
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass

    def _spawn(self, inst: vpn.Exit) -> subprocess.Popen:
        client = self.find_client()
        if client is None:
            raise RuntimeError(self.missing_message())
        return subprocess.Popen(
            [str(client), "-c", inst.extra["config_file"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=self._creationflags())

    def _on_line(self, inst: vpn.Exit, line: str) -> None:
        if "Received handshake response" in line:
            self._mark_ready(inst)
            # The client has read its settings (which hold the private key):
            # keep the file only while it starts.
            self._cleanup(inst)
        elif re.search(r"error|failed|could not|unable", line, re.I) and "DEBUG" not in line:
            inst.error = re.sub(r"\s+", " ", line).strip()[:300]

    # ---- new address ----------------------------------------------------
    def renew(self, region: str) -> dict:
        """Move this location to a sibling server, so the exit address really changes."""
        with self._lock:
            if region not in (self._settings.get("regions") or []):
                raise RuntimeError(f"{region} is not one of the chosen locations")
            if region not in self._instances:
                raise RuntimeError(f"{self.label} {region} is not running. Connect it first.")
            current = self.server_for(region)
            if current is None:
                raise RuntimeError(f"{region} is no longer in Surfshark's server list. Update servers first.")
            in_use = {current["id"]}
            for other, inst in self._instances.items():
                if other == region:
                    continue
                for server in (inst.extra.get("server"), self.server_for(other)):
                    if server:
                        in_use.add(server["id"])
            replacement = next((s for s in self.servers()
                                if s.get("countryCode") == current.get("countryCode")
                                and s["id"] not in in_use), None)
            if replacement is None:
                raise RuntimeError(f"No unused Surfshark server is available for {region}.")
            mapping = {**self._settings["serverByRegion"], region: replacement["id"]}
            self._settings = {**self._settings, "serverByRegion": mapping}
            self._store()
            self._start(region, fresh=True)
        return self.status()

    # ---- status ---------------------------------------------------------
    def public_config(self) -> dict:
        cfg = self.config
        return {"enabled": cfg.get("enabled", True),
                "autoStart": cfg.get("autoStart", False),
                "regions": cfg.get("regions") or [],
                "mode": cfg.get("mode", "spread"),
                "pinned": cfg.get("pinned", "")}

    def status(self) -> dict:
        base = super().status()
        cfg = self.config
        servers = [{k: s.get(k) for k in ("id", "country", "countryCode", "location", "load")}
                   for s in self.servers()]
        base["servers"] = servers
        base["keys"] = self.keys_public()
        base["updatedAt"] = cfg.get("updatedAt") or 0
        base["serverByRegion"] = cfg.get("serverByRegion") or {}
        for row in base["regions"]:
            mapping = cfg.get("serverByRegion") or {}
            server = self.server_of(mapping.get(row["region"], "")) or \
                self.server_of(row["region"])
            row["server"] = server["id"] if server else ""
            row["place"] = _place(server) if server else row["region"]
        return base

    def pick_label(self, inst: vpn.Exit) -> str:
        server = inst.extra.get("server") or {}
        return _place(server) if server else inst.region
