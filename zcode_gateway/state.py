"""Shared application state container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .pool import PoolManager
from .ratelimit import RateLimiter
from .registry import Registry
from .service import ChatService
from .store import JsonlCapture, Store
from .tokens import TokenManager
from .upstream import UpstreamClient


@dataclass
class AppState:
    cfg: Config
    pool: PoolManager
    tokens: TokenManager
    client: UpstreamClient
    registry: Registry
    store: Store
    capture: JsonlCapture
    service: ChatService
    limiter: RateLimiter
    updater: Any = None          # updater.UpdateChecker, wired in lifespan
    catalog: Any = None          # model_catalog.ModelCatalog, wired in lifespan
    generator: Any = None        # generator.GeneratorManager, lazily created
