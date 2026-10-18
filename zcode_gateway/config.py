"""Configuration: YAML file + environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from .zcode_device import default_device_mid


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


class ClientKey(BaseModel):
    key: str
    name: str = "client"
    rpm: int = 0                 # requests-per-minute limit; 0 = unlimited


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8788
    client_keys: list[ClientKey] = Field(default_factory=list)
    require_client_key: bool = True
    admin_key: str = "gw-admin-change-me"


class Fingerprint(BaseModel):
    user_agent: str = (
        "ZCode/3.14.4 ai-sdk/provider-utils/4.0.27 runtime/node.js/24"
    )
    client_version: str = "3.14.4"
    core_version: str = "0.0.83"
    platform: str = "win32-x64"
    device_mid: str = Field(default_factory=default_device_mid)
    language: str = "en-US"
    timezone: str = "Asia/Tehran"
    os_category: str = "windows"
    os_version: str = "10.0.26200"


class UpstreamConfig(BaseModel):
    base_url: str = "https://zcode.z.ai/api/v1"
    chat_path: str = "/zcode-plan/anthropic/v1/messages"
    timeout_connect: float = 15.0
    timeout_read: float = 600.0
    max_attempts: int = 3
    # Native ZCode retries overloads up to 11 attempts with 2s exponential
    # backoff, capped at 60s, and 50-100% jitter. Quota failover stays separate.
    overload_max_attempts: int = Field(default=11, ge=1, le=30)
    overload_base_delay_seconds: float = Field(default=2, ge=0, le=60, allow_inf_nan=False)
    overload_max_delay_seconds: float = Field(default=60, ge=0, le=60, allow_inf_nan=False)
    # Whether the anthropic body variant carries `cache_control`. The capture
    # always included it; flip to false to test whether it is optional.
    anthropic_cache_control: bool = True
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + self.chat_path


class PoolConfig(BaseModel):
    strategy: str = "drain_first"
    min_balance_micro: int = 0
    refresh_lead_seconds: int = Field(default=600, ge=0)
    cooldown_seconds: int = Field(default=30, ge=0)
    # 0 would make every account permanently busy — all requests 503 forever
    max_in_flight_per_account: int = Field(default=4, ge=1)
    # how long acquire() waits for an in-flight slot when every eligible
    # account is at max_in_flight_per_account (0 = fail immediately)
    acquire_wait_seconds: float = Field(default=5.0, ge=0)
    balance_poll_seconds: int = Field(default=300, ge=10)
    # 405/code 3012 "unusual activity": park this model on the offending
    # account for this long, then rotate. Per model, so other models on the
    # same account stay usable. 15 minutes by default.
    blocked_model_cooldown_seconds: int = 900


class AccountsConfig(BaseModel):
    source: str = "accounts_dir"          # providers_json | pool_file | accounts_dir
    providers_json: str = "~/.zcode/v2/credentials.json"
    pool_file: str = "./pool.json"
    dir: str = "./accounts"


class AutoFreeFallbackConfig(BaseModel):
    enabled: bool = False
    chain: list[str] = Field(default_factory=lambda: ["GLM-5.3", "GLM-5.3-Flash"])


class ModelsConfig(BaseModel):
    aliases: dict[str, str] = Field(default_factory=dict)
    default: str = "GLM-5.3-Flash"
    default_anthropic: str = "GLM-5.3"
    default_max_tokens: int = 128000
    probe_unknown: bool = False
    auto_free_fallback: AutoFreeFallbackConfig = Field(
        default_factory=AutoFreeFallbackConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    # False by default: captures write full request/response bodies to disk.
    # Docs say "off by default"; the code must match.
    capture: bool = False
    capture_dir: str = "./logs"


class StoreConfig(BaseModel):
    sqlite_path: str = "./gateway.db"


class UpdateConfig(BaseModel):
    enabled: bool = False
    repo: str = "B3hnamR/AutoZcoder"     # GitHub releases feed
    interval_hours: float = 6.0


class GeneratorConfig(BaseModel):
    """ZCODEAUTO account-generator integration (optional)."""
    enabled: bool = False
    # '' = auto-detect (searched near the exe, cwd, home and common clone
    # locations). Never ship one machine's absolute path as the default.
    zcodeauto_dir: str = ""
    python_exe: str = ""               # python that can run ZCODEAUTO scripts ('' = auto-detect)
    # 'manual' = you solve the captcha yourself in a visible browser.
    # A visible browser is mandatory then (headless is forced off).
    captcha: str = "manual"            # manual | auto
    headless: bool = False             # only applies when captcha == 'auto'
    claim: bool = True                 # claim the invite Global Build after signup
    invite_code: str = "MT9GNDQ8"      # referral code to claim ('' = off)
    # Which proxy schemes the generator may pick from the pool:
    #   all | socks | socks5 | socks4 | http  (or a comma list: "socks5,http")
    # 'socks' = socks4+5 (default: residential-style passes the Starter gate).
    proxy_types: str = "socks"
    # Legacy bool kept for old config.yaml files; only read when proxy_types
    # is left at its default.
    socks_only: bool = True


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)

    # resolved absolute paths (filled by load())
    _root: Path | None = None
    _config_file: Path | None = None

    def resolved(self, base: Path | None = None) -> "Config":
        """Expand ~ and resolve relative paths against the config file's folder.

        Without the base, a relative path like ../accounts would resolve against
        the process cwd - which breaks a copied .exe launched from elsewhere.
        """
        def fix(value: str) -> str:
            expanded = _expand(value)
            path = Path(expanded)
            if path.is_absolute() or base is None:
                return expanded
            return str((base / path).resolve())

        self.accounts.providers_json = fix(self.accounts.providers_json)
        self.accounts.dir = fix(self.accounts.dir)
        self.accounts.pool_file = fix(self.accounts.pool_file)
        self.logging.capture_dir = fix(self.logging.capture_dir)
        self.store.sqlite_path = fix(self.store.sqlite_path)
        # the folder is the base for relative paths; `_config_file` (set by
        # load_config) is the exact file the dashboard Settings tab writes back
        # to — cwd-relative fallbacks edited the wrong config.yaml when the exe
        # was launched from elsewhere
        self._root = base
        return self

    def ensure_dirs(self) -> None:
        Path(self.logging.capture_dir).mkdir(parents=True, exist_ok=True)
        Path(self.store.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        if self.accounts.source == "accounts_dir":
            # a standalone install must work with an empty folder that exists,
            # rather than failing pool loads until the first import
            Path(self.accounts.dir).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config.yaml (if present) merged over defaults, then env overrides."""
    data: dict[str, Any] = {}

    candidates = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("ZCODE_GATEWAY_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(Path(__file__).resolve().parent.parent / "config.yaml")

    base: Path | None = None
    config_file: Path | None = None
    for c in candidates:
        if c and c.is_file():
            with open(c, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            data = _deep_merge(data, loaded)
            config_file = Path(c).resolve()
            base = config_file.parent
            break

    cfg = Config(**data)
    cfg._config_file = config_file

    # environment overrides (ZCODE_GATEWAY_<SECTION>__<FIELD>)
    env = {k: v for k, v in os.environ.items() if k.startswith("ZCODE_GATEWAY_")}
    for key, val in env.items():
        parts = key[len("ZCODE_GATEWAY_"):].lower().split("__")
        if len(parts) != 2:
            continue
        section, field = parts
        if hasattr(cfg, section) and hasattr(getattr(cfg, section), field):
            target = getattr(cfg, section)
            current = getattr(target, field)
            try:
                if isinstance(current, bool):
                    setattr(target, field, val.lower() in ("1", "true", "yes"))
                elif isinstance(current, int):
                    setattr(target, field, int(val))
                elif isinstance(current, float):
                    setattr(target, field, float(val))
                else:
                    setattr(target, field, val)
            except ValueError:
                pass

    return cfg.resolved(base)
