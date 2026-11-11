"""Prefetched Aliyun captcha tickets for claim/signup jobs.

Aliyun rejects a V3 verification parameter submitted more than ~90 s after
the traceless check (F019), so a ticket can only be minted shortly before it
is used. A claim job otherwise spends 10-30 s minting one right before the
signup POST — while the browser sits idle. This pool mints ahead of time on
a background thread and hands each claim a fresh ticket the moment its route
is resolved.

Disabled by default (`size = 0`): the sidecar costs CPU and a mint that is
never used is wasted. Single-use tokens, so nothing is ever handed out twice.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

__all__ = ["CaptchaTicket", "CaptchaTicketPool", "sidecar_mint"]

log = logging.getLogger(__name__)

# Aliyun's hard limit, plus margin for the business request to reach the
# verifier. A ticket older than `ttl - SAFETY` is treated as dead.
MAX_TICKET_TTL = 90.0
SAFETY_MARGIN = 5.0

# Verified live against /client/configs; overridable for a scene rotation.
DEFAULT_SCENE = "11xygtvd"
DEFAULT_REGION = "sgp"
DEFAULT_PREFIX = "no8xfe"

_MINT_RECORD = re.compile(r"([^\s]+) ([A-Za-z0-9_-]+)(?:\r?\n)?")


@dataclass(frozen=True)
class CaptchaTicket:
    param: str
    region: str
    minted_at: float


def _find_sidecar() -> str:
    """The bundled captcha-mint binary, or '' when this install has none."""
    name = "captcha-mint.exe" if os.name == "nt" else "captcha-mint"
    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(os.path.join(meipass, "captcha-mint"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    roots += [os.path.join(exe_dir, "captcha-mint"),
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", "captcha-mint")]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def sidecar_mint(timeout: float = 45.0) -> CaptchaTicket | None:
    """Mint one ticket by running the sidecar; None when it cannot.

    The token is not bound to an exit IP, so the mint runs directly from the
    gateway host even when the claim itself will use a proxy.
    """
    cmd = (os.environ.get("ZAI_CAPTCHA_CMD") or "").strip().strip('"')
    if not cmd:
        cmd = _find_sidecar()
    if not cmd:
        return None
    env = os.environ.copy()
    for key in list(env):
        if key.upper() in ("ZAI_PROXY", "ZAI_CAPTCHA_PROXY", "HTTP_PROXY",
                           "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            env.pop(key)
    env.update(ZAI_CAPTCHA_SCENE=os.environ.get("ZAI_CAPTCHA_SCENE", DEFAULT_SCENE),
               ZAI_CAPTCHA_REGION=os.environ.get("ZAI_CAPTCHA_REGION", DEFAULT_REGION),
               ZAI_CAPTCHA_PREFIX=os.environ.get("ZAI_CAPTCHA_PREFIX", DEFAULT_PREFIX))
    try:
        out = subprocess.run([cmd], env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout,
                             shell=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("captcha sidecar failed to run: %s", e)
        return None
    record = _MINT_RECORD.fullmatch(out.stdout or "")
    if out.returncode != 0 or not record or len(record[1]) <= 20:
        log.debug("captcha sidecar protocol failure (rc=%s)", out.returncode)
        return None
    return CaptchaTicket(record[1], record[2], time.monotonic())


class CaptchaTicketPool:
    """Keep up to `size` fresh tickets; `take()` consumes one."""

    def __init__(self, size: int = 0, ttl_seconds: float = 75.0,
                 mint=None, clock=time.monotonic) -> None:
        self.size = max(0, int(size))
        self.ttl = min(float(ttl_seconds), MAX_TICKET_TTL)
        self._mint = mint or sidecar_mint
        self._clock = clock
        self._tickets: list[CaptchaTicket] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.minted = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return self.size > 0 and self.ttl > SAFETY_MARGIN

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="captcha-tickets")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def take(self) -> CaptchaTicket | None:
        """Pop the newest live ticket, or None when the pool is cold."""
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            live = [t for t in self._tickets
                    if now - t.minted_at <= self.ttl - SAFETY_MARGIN]
            self._tickets.clear()
            ticket = live[-1] if live else None
        self._wake.set()
        return ticket

    def put(self, ticket: CaptchaTicket) -> None:
        with self._lock:
            if len(self._tickets) < self.size:
                self._tickets.append(ticket)

    def stats(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "size": self.size,
                    "ttl_seconds": self.ttl, "ready": len(self._tickets),
                    "minted": self.minted, "failed": self.failed}

    def _run(self) -> None:
        # Fill once up front, then top up after each take.
        while not self._stop.is_set():
            with self._lock:
                missing = self.size - len(self._tickets)
            if missing <= 0:
                self._wake.wait(timeout=max(self.ttl / 2, 5.0))
                self._wake.clear()
                continue
            ticket = self._mint()
            if self._stop.is_set():
                return
            if ticket is None:
                with self._lock:
                    self.failed += 1
                self._stop.wait(timeout=5.0)
                continue
            with self._lock:
                self.minted += 1
                if len(self._tickets) < self.size:
                    self._tickets.append(ticket)
