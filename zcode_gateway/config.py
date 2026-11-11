"""Configuration: YAML file + environment overrides."""

from __future__ import annotations

import json
import os
import ssl
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, ClassVar, get_origin

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from .zcode_device import default_device_mid


_CERT_HOSTS_CACHE: dict[tuple[str, int, int], tuple[str, ...]] = {}


def _read_certificate_hosts(path: str) -> tuple[str, ...]:
    """DNS/IP subjectAltNames of the first certificate in a PEM file."""
    try:
        from cryptography import x509
        with open(path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in san.get_values_for_type(x509.DNSName)]
        names += [str(n) for n in san.get_values_for_type(x509.IPAddress)]
        return tuple(n.lower() for n in names if "*" not in n)
    except ImportError:
        pass                       # Windows build ships without cryptography
    except Exception:
        return ()
    # stdlib fallback: undocumented helper, so tolerate its absence entirely
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        return ()
    try:
        cert = decode(path)
    except Exception:
        return ()
    return tuple(str(value).lower() for kind, value in cert.get("subjectAltName", ())
                 if kind in ("DNS", "IP Address") and "*" not in str(value))


def _certificate_hosts_cached(path: str) -> tuple[str, ...]:
    if not path:
        return ()
    try:
        st = os.stat(path)
    except OSError:
        return ()
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _CERT_HOSTS_CACHE.get(key)
    if hit is None:
        # a renewed certificate (new mtime) replaces the stale entry
        for old in [k for k in _CERT_HOSTS_CACHE if k[0] == path]:
            _CERT_HOSTS_CACHE.pop(old, None)
        hit = _CERT_HOSTS_CACHE[key] = _read_certificate_hosts(path)
    return hit


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
    # Placeholder keys that have shipped in config templates. The public-bind
    # boot guard (app.py) rejects ALL of these, not just the exact default:
    # an example value like "gw-admin-change-me-bytes" must not sail past a
    # character-prefix comparison and boot a VPS with a repo-published key.
    PLACEHOLDER_ADMIN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "gw-admin-change-me",
        "gw-admin-change-me-bytes",
        "gw-admin-local",
    })
    PLACEHOLDER_CLIENT_KEYS: ClassVar[frozenset[str]] = frozenset({
        "gw-client-change-me",
        "gw-local-1",
    })
    # The admin key is local root-equivalent (it can point the generator at
    # arbitrary code, swap the binary, rewrite config). Minimum length keeps
    # an empty/one-char key from ever authenticating (fail closed).
    admin_key: str = Field(default="gw-admin-change-me", min_length=8)
    # VPS / remote bind hardening. When host is non-loopback:
    #   - /admin/* and /dash key injection additionally require these flags
    #     (loopback peers always pass), and the ops endpoints (/health,
    #     /ready, /metrics) answer 404 unless expose_ops is set.
    #   - a missing client key requirement refuses to start (see app.py):
    #     a proxy with no auth on a public interface is an open relay.
    allow_remote_admin: bool = False
    expose_ops: bool = False
    # Built-in HTTPS: when both are set the gateway terminates TLS itself, so a
    # VPS needs no reverse proxy (nginx/Caddy) in front of it.
    tls_cert: str = ""
    tls_key: str = ""
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "::1"])
    child_origin: str = ""

    def _certificate_hosts(self) -> list[str]:
        # the request-path middleware asks on every dashboard call: parse the
        # file once per (path, mtime), not once per request
        return list(_certificate_hosts_cached(self.tls_cert))

    def trusted_hosts(self) -> set[str]:
        hosts = {h.strip().lower().strip("[]") for h in self.allowed_hosts if h.strip() and "*" not in h}
        hosts.update(self._certificate_hosts())
        if self.host not in ("0.0.0.0", "::"):
            hosts.add(self.host.lower().strip("[]"))
        if self.child_origin:
            hosts.add((urlsplit(self.child_origin).hostname or "").lower())
        return hosts

    def child_connection(self) -> tuple[str, str]:
        """Verified child callback origin; certificate SANs supply TLS names."""
        tls = bool(self.tls_cert and self.tls_key)
        if self.child_origin:
            parsed = urlsplit(self.child_origin)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password \
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("server.child_origin must be an absolute HTTP(S) origin without credentials or path")
            if tls and parsed.scheme != "https":
                raise ValueError("server.child_origin must use HTTPS with the TLS listener")
            return self.child_origin.rstrip("/"), self.tls_cert if parsed.scheme == "https" else ""
        host = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        if tls:
            names = self._certificate_hosts()
            if host not in names:
                if self.is_loopback_bind():
                    raise ValueError("TLS callback certificate does not cover the loopback bind; set a reachable server.child_origin")
                if not names:
                    raise ValueError("TLS certificate has no usable SAN; set server.child_origin")
                host = names[0]
        authority = f"[{host}]" if ":" in host else host
        return f"{'https' if tls else 'http'}://{authority}:{self.port}", self.tls_cert if tls else ""

    def ssl_kwargs(self) -> dict:
        """uvicorn TLS arguments; empty when TLS is not configured."""
        cert, key = (self.tls_cert or "").strip(), (self.tls_key or "").strip()
        if not cert and not key:
            return {}
        if not (cert and key):
            raise RuntimeError("server.tls_cert and server.tls_key must be set together")
        for path in (cert, key):
            if not os.path.isfile(path):
                raise RuntimeError(f"TLS file not found: {path}")
        return {"ssl_certfile": cert, "ssl_keyfile": key}

    def is_loopback_bind(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")

    def admin_key_is_placeholder(self) -> bool:
        return self.admin_key in self.PLACEHOLDER_ADMIN_KEYS

    def has_placeholder_client_key(self) -> bool:
        return any(k.key in self.PLACEHOLDER_CLIENT_KEYS
                   for k in self.client_keys)


class Fingerprint(BaseModel):
    user_agent: str = (
        "ZCode/3.18.0 ai-sdk/provider-utils/4.0.27 runtime/node.js/24"
    )
    # The billing preview gift list (and the plan shown on /billing/balance)
    # is version-gated: 3.14.3..3.16.0 answer [], 3.16.1+ name the plan. A
    # 3.14.x fingerprint therefore reads as "this account has no plan" even
    # when the Start Plan is active. 3.18.0 is the build this repo tracks.
    client_version: str = "3.18.0"
    core_version: str = "0.0.83"
    platform: str = "win32-x64"
    device_mid: str = Field(default_factory=default_device_mid)
    language: str = "en-US"
    # Neutral default: a fixed owner-personal zone shipped to every install
    # was a correlation signal (all traffic claiming one geography regardless
    # of the account/relay exit). New installs get UTC; existing configs keep
    # whatever they set explicitly.
    timezone: str = "UTC"
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
    # Inject the full captured ZCode harness (policy + tooling paragraphs)
    # into every request's system block. True = fingerprint-faithful (this is
    # what fixed the 405/3012 wave) but it also steers the model's behavior
    # for every client. False = identity line only, no behavior steering —
    # only flip it off if the upstream still accepts the request shape.
    bootstrap_harness: bool = True
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
    # Keep a conversation on the account that served its first turn, so later
    # turns still see the earlier ones and the upstream prompt cache stays
    # warm. Keyed by the client's session id; 0 disables it.
    conversation_affinity_seconds: int = Field(default=600, ge=0)


class AccountsConfig(BaseModel):
    source: str = "accounts_dir"          # providers_json | pool_file | accounts_dir
    providers_json: str = "~/.zcode/v2/credentials.json"
    pool_file: str = "./pool.json"
    dir: str = "./accounts"


class AutoFreeFallbackConfig(BaseModel):
    enabled: bool = False
    chain: list[str] = Field(default_factory=lambda: ["GLM-5.3", "GLM-5.3-Flash"])


class PacingConfig(BaseModel):
    """Shared randomized gap between upstream sends (WAF/ban prevention).

    With dozens of accounts and parallel workers we otherwise hammer one
    egress IP with zero gap — the exact traffic shape CDN edge blocks are
    trained on. The gap is GLOBAL (one egress), randomized so parallel
    requests don't self-synchronize, and off-able for throughput-first use.
    """
    enabled: bool = True
    min_gap_ms: int = 150
    max_gap_ms: int = 400


class ModelsConfig(BaseModel):
    aliases: dict[str, str] = Field(default_factory=dict)
    default: str = "GLM-5.3-Flash"
    default_anthropic: str = "GLM-5.3"
    default_max_tokens: int = 128000
    probe_unknown: bool = False
    # Reasoning effort used when the client doesn't specify one. 'max' made
    # every unspecified request pay maximum reasoning latency/tokens silently;
    # 'high' is near-identical in quality for coding use. low|high|max.
    default_effort: str = "high"
    auto_free_fallback: AutoFreeFallbackConfig = Field(
        default_factory=AutoFreeFallbackConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    # False by default: captures write full request/response bodies to disk.
    # Docs say "off by default"; the code must match.
    capture: bool = False
    capture_dir: str = "./logs"


class StoreConfig(BaseModel):
    sqlite_path: str = "./gateway.db"


class UpdateConfig(BaseModel):
    # on by default: users get the update badge automatically. Set false only
    # to run fully offline.
    enabled: bool = True
    repo: str = "B3hnamR/AutoZcoder"     # GitHub releases feed
    interval_hours: float = 6.0
    # legacy repos this tool shipped under before the rename; a config that
    # still points at one is migrated to the current repo at runtime
    legacy_repos: tuple[str, ...] = ("B3hnamR/ZcodeGate", "B3hnamR/AutoZCoder-old")


class GeneratorConfig(BaseModel):
    """ZCODEAUTO account-generator integration (optional)."""
    enabled: bool = False
    # '' = auto-detect (searched near the exe, cwd, home and common clone
    # locations). Never ship one machine's absolute path as the default.
    zcodeauto_dir: str = ""
    python_exe: str = ""               # python that can run ZCODEAUTO scripts ('' = auto-detect)
    # 'manual' = you solve the captcha yourself in a visible browser.
    # A visible browser is mandatory then (headless is forced off).
    # 'mint' = no slider: the bundled captcha-mint sidecar mints the Aliyun
    # verify param (traceless, the same source claims use) and the generator
    # sends the signup POST itself; falls back to the drag solver if minting
    # fails.
    captcha: str = Field(default_factory=lambda: "ai" if sys.platform.startswith("linux") else "manual")
    headless: bool = False             # only applies when captcha == 'auto'
    claim: bool = False                # legacy: invite Global Build (100M) claim after signup
    claim500: bool = False             # legacy: 1024 Dev Day 500M giveaway (withdrawn)
    # Start Plan + campaign cards: the signup grant was removed, so the plan is
    # claimed from the billing preview after signup, and the marketing feed is
    # also walked for Global/Weekend Build cards (their ids rotate per drop).
    # This is the default path now.
    claim_start: bool = True
    invite_code: str = "MT9GNDQ8"      # referral code to claim ('' = off)
    # Prefetch Aliyun captcha tickets for claims, so the signup POST does not
    # wait 10-30 s for a mint while the browser idles. Tokens die 90 s after
    # minting, so only keep a couple; 0 disables the background minter.
    captcha_tickets: int = Field(default=0, ge=0, le=8)
    captcha_ticket_ttl_seconds: int = Field(default=75, ge=0, le=90)
    # Which proxy schemes the generator may pick from the pool:
    #   all | socks | socks5 | socks4 | http  (or a comma list: "socks5,http")
    # 'socks' = socks4+5 (default: residential-style passes the Starter gate).
    proxy_types: str = "socks"
    # Legacy bool kept for old config.yaml files; only read when proxy_types
    # is left at its default.
    socks_only: bool = True

    @model_validator(mode="after")
    def _explicit_claim_stays_exclusive(self) -> "GeneratorConfig":
        # claim500 defaults to True, so a config.yaml written for the old
        # schema (`claim: true`, no claim500) would silently flip to the 500M
        # giveaway. An explicit `claim` without an explicit `claim500` keeps
        # meaning exactly what it said.
        if "claim" in self.model_fields_set and "claim500" not in self.model_fields_set:
            self.claim500 = False
        return self


class ClaimsConfig(BaseModel):
    """Optional unattended daily claim-all run.

    Fixed UTC offset, because Windows has no IANA database (`zoneinfo` would
    raise). 8 = Asia/Shanghai, 3.5 = Tehran. Cards go live on Beijing time,
    so the offset matters more than the machine's local clock.
    """
    daily_enabled: bool = False
    daily_time: str = "19:02"
    daily_utc_offset_hours: float = Field(default=8.0, ge=-12.0, le=14.0)
    daily_mode: str = "direct"        # direct | proxy, same as the button
    # Weekend Build drop: Saturday 00:00 -> Monday 00:00 Beijing. The
    # dashboard enables the per-account claim buttons only inside it; set
    # campaign_gate = false to keep them live all week.
    campaign_utc_offset_hours: float = Field(default=8.0, ge=-12.0, le=14.0)
    campaign_gate: bool = True


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)
    claims: ClaimsConfig = Field(default_factory=ClaimsConfig)
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
        if self.server.tls_cert:
            self.server.tls_cert = fix(self.server.tls_cert)
        if self.server.tls_key:
            self.server.tls_key = fix(self.server.tls_key)
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

    # Decode collections explicitly; all overrides then pass through the same
    # schema validation as YAML rather than unchecked setattr assignments.
    for key, val in os.environ.items():
        if not key.startswith("ZCODE_GATEWAY_"):
            continue
        parts = key[len("ZCODE_GATEWAY_"):].lower().split("__")
        if len(parts) != 2:
            continue
        section, field = parts
        section_info = Config.model_fields.get(section)
        if section_info is None:
            continue
        section_type = section_info.annotation
        field_info = section_type.model_fields.get(field)
        if field_info is None:
            continue
        value: Any = val
        if get_origin(field_info.annotation) in (list, tuple, dict):
            try:
                value = json.loads(val)
            except ValueError:
                raise SystemExit(
                    f"config error: {key} ({section}.{field}) must contain valid JSON") from None
        section_data = data.get(section, {})
        data[section] = {**(section_data if isinstance(section_data, dict) else {}), field: value}

    try:
        cfg = Config(**data)
    except ValidationError as e:
        # A previously-accepted config that no longer validates (e.g. a short
        # admin_key, rejected since the min_length=8 hardening) must not die
        # with a raw pydantic traceback — point the operator at the field.
        fields = ", ".join(sorted({
            ".".join(str(p) for p in err.get("loc", ()))
            for err in e.errors()}))
        raise SystemExit(
            f"config error in {config_file or 'config.yaml'}: "
            f"invalid value(s) for {fields}: {e.errors()[0].get('msg', '')} "
            f"— fix the listed field(s) and start again") from e
    cfg._config_file = config_file

    return cfg.resolved(base)
