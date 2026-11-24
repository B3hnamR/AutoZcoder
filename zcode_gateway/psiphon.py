"""Psiphon as an IP source.

The bundled `psiphon-tunnel-core` client runs once per chosen region, each with
its own data folder and local SOCKS port. Those ports are what the generator
sends signups through, so every region is a different exit address; a region
can be reconnected ("New address") to land on another server of the same region.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import vpn

ROUTE_ID = "psiphon"
BASE_PORT = 47100
# Regions Psiphon has offered; the real list comes from the client and is saved
# with the settings, so Update servers replaces this.
KNOWN = ["BE", "CA", "CH", "DE", "DK", "ES", "FR", "GB", "IE", "IN", "IT",
         "JP", "NL", "NO", "PL", "SE", "SG", "US"]

REGION_NAMES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CO": "Colombia",
    "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "EE": "Estonia",
    "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GR": "Greece", "HK": "Hong Kong", "HR": "Croatia", "HU": "Hungary",
    "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India",
    "IS": "Iceland", "IT": "Italy", "JP": "Japan", "KR": "South Korea",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MD": "Moldova",
    "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands", "NO": "Norway",
    "NZ": "New Zealand", "PH": "Philippines", "PL": "Poland", "PT": "Portugal",
    "RO": "Romania", "RS": "Serbia", "SE": "Sweden", "SG": "Singapore",
    "SI": "Slovenia", "SK": "Slovakia", "TH": "Thailand", "TR": "Türkiye",
    "TW": "Taiwan", "UA": "Ukraine", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}


class PsiphonPool(vpn.ExitPool):
    kind = ROUTE_ID
    label = "Psiphon"
    base_port = BASE_PORT
    defaults = {"enabled": True, "autoStart": False, "regions": [],
                "mode": "spread", "pinned": "", "available": []}

    def normalize(self, config: dict) -> dict:
        regions = config.get("regions")
        if isinstance(regions, list):
            config["regions"] = sorted({str(r).upper() for r in regions
                                        if vpn.REGION_CODE.match(str(r).upper())})
        config["mode"] = "single" if config.get("mode") == "single" else "spread"
        pinned = str(config.get("pinned") or "").upper()
        config["pinned"] = pinned if vpn.REGION_CODE.match(pinned) else ""
        available = config.get("available")
        if isinstance(available, list):
            config["available"] = sorted({str(r).upper() for r in available
                                          if vpn.REGION_CODE.match(str(r).upper())})
        else:
            config["available"] = []
        if config["mode"] == "single" and config["pinned"] not in config["regions"]:
            config["pinned"] = (config["regions"] or [""])[0]
        return config

    def binary_name(self) -> str:
        machine = platform.machine().lower()
        if sys.platform == "win32":
            return "psiphon-tunnel-core-windows-i686.exe"
        if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
            return "psiphon-tunnel-core-linux-amd64"
        return ""

    def missing_message(self) -> str:
        if not self.binary_name():
            return (f"Psiphon has no official client for {sys.platform} "
                    f"{platform.machine()}. It runs on Windows and Linux x86-64.")
        return ("The Psiphon client is missing from this build. It belongs in "
                "the app's psiphon folder (vendor/psiphon in a source checkout).")

    def folder(self) -> Path | None:
        client = self.find_client()
        return client.parent if client else None

    # ---- client config --------------------------------------------------
    def region_dir(self, region: str) -> Path:
        return self.data_dir / region.lower()

    def store_path(self, region: str) -> Path:
        return self.region_dir(region) / "ca.psiphon.PsiphonTunnel.tunnel-core"

    def nodes_dir(self) -> Path:
        return self.data_dir / "nodes"

    def _prepare(self, inst: vpn.Exit) -> None:
        folder = self.folder()
        if folder is None:
            raise RuntimeError(self.missing_message())
        base = json.loads((folder / "psiphon.json").read_text(encoding="utf-8"))
        target = self.region_dir(inst.region)
        target.mkdir(parents=True, exist_ok=True)
        config = {**base,
                  "DataRootDirectory": str(target),
                  "LocalSocksProxyPort": inst.port,
                  "DisableLocalHTTPProxy": True,
                  "DisableRemoteServerListFetcher": True,
                  "EnableUpgradeDownload": False,
                  "EnableFeedbackUpload": False,
                  "EstablishTunnelTimeoutSeconds": 0}
        if inst.region:
            config["EgressRegion"] = inst.region
        inst.extra["config_file"] = str(target / "autozcoder.config")
        Path(inst.extra["config_file"]).write_text(json.dumps(config), encoding="utf-8")
        inst.extra["server_list"] = str(folder / "server-list.dat")

    def _fresh(self, inst: vpn.Exit) -> None:
        shutil.rmtree(self.store_path(inst.region), ignore_errors=True)
        nodes = self.nodes_dir() / "ca.psiphon.PsiphonTunnel.tunnel-core"
        store = self.store_path(inst.region)
        if nodes.is_dir() and not store.exists():
            try:
                shutil.copytree(nodes, store)
            except OSError:
                pass

    def _spawn(self, inst: vpn.Exit) -> subprocess.Popen:
        client = self.find_client()
        if client is None:
            raise RuntimeError(self.missing_message())
        proc = subprocess.Popen(
            [str(client), "-config", inst.extra["config_file"],
             "-serverList", inst.extra["server_list"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=self._creationflags())
        return proc

    def _on_line(self, inst: vpn.Exit, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            notice = json.loads(line)
        except ValueError:
            return
        kind = notice.get("noticeType")
        data = notice.get("data") or {}
        if kind == "AvailableEgressRegions":
            self._save_regions(data.get("regions"), replace=False)
        elif kind == "Tunnels":
            count = data.get("count") or 0
            if count > 0:
                self._mark_ready(inst)
            elif inst.state == "ready":
                inst.state = "reconnecting"
        elif kind in ("Error", "Alert"):
            message = str(data.get("message") or "")
            if message:
                inst.error = message[:300]

    # ---- region list ----------------------------------------------------
    def _save_regions(self, regions, replace: bool = False) -> None:
        if not isinstance(regions, list):
            return
        reported = {str(r).upper() for r in regions if vpn.REGION_CODE.match(str(r).upper())}
        if not reported:
            return
        with self._lock:
            current = set(self._settings.get("available") or [])
            merged = sorted(reported if replace else current | reported)
            if merged == sorted(current):
                return
            self._settings = {**self._settings, "available": merged}
            self._store()

    def available(self) -> list[str]:
        cfg = self.config
        offered = cfg.get("available") or []
        return sorted(set(offered or KNOWN) | set(cfg.get("regions") or []))

    def public_config(self) -> dict:
        cfg = self.config
        return {"enabled": cfg.get("enabled", True),
                "autoStart": cfg.get("autoStart", False),
                "regions": cfg.get("regions") or [],
                "mode": cfg.get("mode", "spread"),
                "pinned": cfg.get("pinned", "")}

    def status(self) -> dict:
        base = super().status()
        base["available"] = self.available()
        base["regionNames"] = REGION_NAMES
        return base

    # ---- update servers -------------------------------------------------
    def update(self, timeout: float = 60.0) -> dict:
        """Run one client with no region so the handshake brings Psiphon's list."""
        if not self.find_client():
            raise RuntimeError(self.missing_message())
        with self._lock:
            self._busy = "updating nodes"
        try:
            nodes = self.nodes_dir()
            shutil.rmtree(nodes, ignore_errors=True)
            nodes.mkdir(parents=True, exist_ok=True)
            client = self.find_client()
            port = vpn.free_port(self.base_port + 90, self._used_ports())
            base = json.loads((client.parent / "psiphon.json").read_text(encoding="utf-8"))
            config_file = nodes / "update.config"
            config_file.write_text(json.dumps({
                **base, "DataRootDirectory": str(nodes),
                "LocalSocksProxyPort": port, "DisableLocalHTTPProxy": True,
                "EnableUpgradeDownload": False, "EnableFeedbackUpload": False,
                "EstablishTunnelTimeoutSeconds": 0}), encoding="utf-8")
            proc = subprocess.Popen(
                [str(client), "-config", str(config_file),
                 "-serverList", str(client.parent / "server-list.dat")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=self._creationflags())
            regions: list[str] = []
            connected = False
            deadline = time.time() + timeout

            def read():
                nonlocal regions, connected
                assert proc.stdout is not None
                for raw in proc.stdout:
                    if time.time() > deadline:
                        break
                    try:
                        notice = json.loads(raw.decode("utf-8", "replace").strip())
                    except ValueError:
                        continue
                    kind = notice.get("noticeType")
                    data = notice.get("data") or {}
                    if kind == "AvailableEgressRegions":
                        regions = [str(r).upper() for r in data.get("regions") or []
                                   if vpn.REGION_CODE.match(str(r).upper())]
                    elif kind == "Tunnels" and (data.get("count") or 0) > 0:
                        connected = True

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(timeout)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not connected and not regions:
                raise RuntimeError("Psiphon could not connect to update its servers. "
                                   "Check this computer's connection and try again.")
            self._save_regions(regions, replace=True)
        finally:
            with self._lock:
                self._busy = ""
        return self.status()
