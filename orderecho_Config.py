"""YAML config loading and validation.

Raises ConfigError naming the offending key for anything missing or of the
wrong type.  No defaults are invented for required keys: a config that does
not say what it means is an error, not a guess.
"""

from __future__ import annotations

import copy
import dataclasses
import ipaddress
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal

import yaml

from orderecho_FixVersion import UnknownFixVersion, profile_for
from orderecho_Rules import (
    BEHAVIORS,
    MATCH_KEYS,
    THEN_LEAVE,
    THEN_VALUES,
    RuleError,
    RuleSet,
    load_rules,
)


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


@dataclass
class SessionConfig:
    fix_version: str
    sender_comp_id: str
    target_comp_id: str
    host: str
    port: int
    heartbeat_grace_pct: float
    logout_timeout_sec: float
    resend_mode: str = "replay"

    @property
    def name(self) -> str:
        """Session identity as used in file names and logs."""
        return f"{self.sender_comp_id}-{self.target_comp_id}"


@dataclass
class StorageConfig:
    seqnum_dir: str
    evidence_dir: str
    msgstore_dir: str = "data/msgstore"


@dataclass
class LoggingConfig:
    log_dir: str
    fix_delimiter: str
    engine_level: str
    console: bool


@dataclass
class OrdersConfig:
    default_delay_ms: int = 500
    send_pending_acks: bool = False
    replace_ack_ordstatus: str = "current"


@dataclass
class PricingConfig:
    mode: str = "static"
    provider: str = "yfinance"
    cache_seconds: float = 300.0
    timeout_sec: float = 10.0
    static: dict = field(default_factory=dict)
    static_default: Decimal = Decimal("100.00")
    warm_symbols: list = field(
        default_factory=lambda: ["AAPL", "MSFT", "SPY"]
    )


@dataclass
class PriceBandConfig:
    """Sell-side fat-finger collar."""

    enabled: bool = False
    pct: float = 10.0
    mode: str = "aggressive"          # aggressive | both
    enforce_on_fallback: bool = False
    lookup_timeout_ms: float = 3000.0
    on_no_reference: str = "skip"      # skip | reject

    @property
    def lookup_timeout_sec(self) -> float:
        return self.lookup_timeout_ms / 1000.0


@dataclass
class ControlApiConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8090


@dataclass
class EngineConfig:
    """Settings that belong to the engine rather than to one session."""

    host: str = "127.0.0.1"
    fix_port: int = 9878
    default_session: str | None = None
    #: How long a new connection may stay silent before we stop waiting for
    #: its Logon and drop it.
    logon_timeout_sec: float = 30.0


@dataclass
class SessionSpec:
    """One FIX session: its identity and everything it owns."""

    id: str
    session: SessionConfig
    orders: OrdersConfig = field(default_factory=OrdersConfig)
    rules: RuleSet | None = None
    price_band: PriceBandConfig = field(default_factory=PriceBandConfig)

    @property
    def fix_version(self) -> str:
        return self.session.fix_version

    @property
    def sender_comp_id(self) -> str:
        return self.session.sender_comp_id

    @property
    def target_comp_id(self) -> str:
        return self.session.target_comp_id

    @property
    def port(self) -> int:
        return self.session.port

    @property
    def host_port(self) -> str:
        return f"{self.session.host}:{self.session.port}"

    @property
    def triple(self) -> tuple:
        """The FIX identity of a session: BeginString and both CompIDs."""
        return (self.fix_version, self.sender_comp_id, self.target_comp_id)


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Config:
    session: SessionConfig
    storage: StorageConfig
    logging: LoggingConfig
    orders: OrdersConfig = field(default_factory=OrdersConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    control_api: ControlApiConfig = field(default_factory=ControlApiConfig)
    price_band: PriceBandConfig = field(default_factory=PriceBandConfig)
    rules: RuleSet | None = None
    engine: EngineConfig = field(default_factory=EngineConfig)
    sessions: list = field(default_factory=list)
    path: str = ""

    def __post_init__(self):
        # A config built the old way (one `session:` block, or constructed
        # directly by a test) is just a one-session engine.
        if not self.sessions:
            self.sessions = [
                SessionSpec(
                    id=f"{self.session.sender_comp_id}-"
                       f"{self.session.target_comp_id}",
                    session=self.session,
                    orders=self.orders,
                    rules=self.rules,
                    price_band=self.price_band,
                )
            ]

    def session_by_id(self, session_id: str):
        for spec in self.sessions:
            if spec.id == session_id:
                return spec
        return None

    @property
    def default_session_spec(self):
        """The session the legacy, unscoped API routes act on."""
        if len(self.sessions) == 1:
            return self.sessions[0]
        if self.engine.default_session:
            return self.session_by_id(self.engine.default_session)
        return None

    @property
    def ports(self) -> list:
        seen = []
        for spec in self.sessions:
            if spec.port not in seen:
                seen.append(spec.port)
        return seen


_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_VALID_PRICING_MODES = ("live", "static")
_VALID_PROVIDERS = ("yfinance",)
_VALID_REPLACE_ACK = ("current", "replaced")
_VALID_RESEND_MODES = ("replay", "gapfill")
_VALID_BAND_MODES = ("aggressive", "both")
_VALID_NO_REFERENCE = ("skip", "reject")


def _section(raw: dict, name: str) -> dict:
    if name not in raw or raw[name] is None:
        raise ConfigError(f"config: missing required section '{name}'")
    value = raw[name]
    if not isinstance(value, dict):
        raise ConfigError(f"config: section '{name}' must be a mapping")
    return value


def _optional_section(raw: dict, name: str) -> dict:
    """A section that may be absent entirely, but must be a mapping if present."""
    if name not in raw or raw[name] is None:
        return {}
    value = raw[name]
    if not isinstance(value, dict):
        raise ConfigError(f"config: section '{name}' must be a mapping")
    return value


def _require(section: dict, section_name: str, key: str):
    if key not in section or section[key] is None:
        raise ConfigError(f"config: missing required key '{section_name}.{key}'")
    return section[key]


def _require_str(section: dict, section_name: str, key: str) -> str:
    value = _require(section, section_name, key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"config: '{section_name}.{key}' must be a non-empty string, "
            f"got {value!r}"
        )
    return value


def _require_int(section: dict, section_name: str, key: str,
                 minimum: int | None = None) -> int:
    value = _require(section, section_name, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"config: '{section_name}.{key}' must be an integer, got {value!r}"
        )
    if minimum is not None and value < minimum:
        raise ConfigError(
            f"config: '{section_name}.{key}' must be >= {minimum}, got {value!r}"
        )
    return value


def _require_number(section: dict, section_name: str, key: str,
                    minimum: float | None = None) -> float:
    value = _require(section, section_name, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"config: '{section_name}.{key}' must be a number, got {value!r}"
        )
    if minimum is not None and value < minimum:
        raise ConfigError(
            f"config: '{section_name}.{key}' must be >= {minimum}, got {value!r}"
        )
    return float(value)


def _require_bool(section: dict, section_name: str, key: str) -> bool:
    value = _require(section, section_name, key)
    if not isinstance(value, bool):
        raise ConfigError(
            f"config: '{section_name}.{key}' must be true or false, got {value!r}"
        )
    return value


def load_config(path: str) -> Config:
    """Load and validate the YAML config at *path*."""
    if not os.path.exists(path):
        raise ConfigError(f"config: file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: could not parse {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config: {path} must contain a top-level mapping")

    multi = raw.get("sessions") is not None
    storage_raw = _section(raw, "storage")
    logging_raw = _section(raw, "logging")

    if multi:
        if "session" in raw:
            raise ConfigError(
                "config: use either a single 'session:' block or a "
                "'sessions:' list, not both"
            )
        # A placeholder, replaced below by the default session's own config.
        session_raw = {
            "fix_version": "FIX.4.2", "sender_comp_id": "ORDERECHO",
            "target_comp_id": "AGENT", "host": "127.0.0.1", "port": 9878,
            "heartbeat_grace_pct": 20, "logout_timeout_sec": 10,
        }
    else:
        session_raw = _section(raw, "session")

    fix_version = _require_str(session_raw, "session", "fix_version")
    try:
        profile_for(fix_version)
    except UnknownFixVersion as exc:
        raise ConfigError(f"config: 'session.fix_version': {exc}") from exc

    port = _require_int(session_raw, "session", "port", minimum=1)
    if port > 65535:
        raise ConfigError(f"config: 'session.port' must be <= 65535, got {port}")

    session = SessionConfig(
        fix_version=fix_version,
        sender_comp_id=_require_str(session_raw, "session", "sender_comp_id"),
        target_comp_id=_require_str(session_raw, "session", "target_comp_id"),
        host=_require_str(session_raw, "session", "host"),
        port=port,
        heartbeat_grace_pct=_require_number(
            session_raw, "session", "heartbeat_grace_pct", minimum=0
        ),
        logout_timeout_sec=_require_number(
            session_raw, "session", "logout_timeout_sec", minimum=0
        ),
    )

    if "resend_mode" in session_raw:
        resend_mode = _require_str(session_raw, "session", "resend_mode")
        if resend_mode not in _VALID_RESEND_MODES:
            raise ConfigError(
                "config: 'session.resend_mode' must be one of "
                f"{', '.join(_VALID_RESEND_MODES)}, got {resend_mode!r}"
            )
        session.resend_mode = resend_mode

    storage = StorageConfig(
        seqnum_dir=_require_str(storage_raw, "storage", "seqnum_dir"),
        evidence_dir=_require_str(storage_raw, "storage", "evidence_dir"),
    )
    if "msgstore_dir" in storage_raw:
        storage.msgstore_dir = _require_str(storage_raw, "storage",
                                            "msgstore_dir")

    delimiter = _require_str(logging_raw, "logging", "fix_delimiter")
    if delimiter not in ("|", "SOH"):
        raise ConfigError(
            f"config: 'logging.fix_delimiter' must be '|' or 'SOH', got {delimiter!r}"
        )

    level = _require_str(logging_raw, "logging", "engine_level").upper()
    if level not in _VALID_LEVELS:
        raise ConfigError(
            "config: 'logging.engine_level' must be one of "
            f"{', '.join(_VALID_LEVELS)}, got {level!r}"
        )

    log_config = LoggingConfig(
        log_dir=_require_str(logging_raw, "logging", "log_dir"),
        fix_delimiter=delimiter,
        engine_level=level,
        console=_require_bool(logging_raw, "logging", "console"),
    )

    orders = _load_orders(_optional_section(raw, "orders"))
    pricing = _load_pricing(_optional_section(raw, "pricing"))
    control_api = _load_control_api(_optional_section(raw, "control_api"))
    price_band = _load_price_band(_optional_section(raw, "price_band"))

    rules = None
    if "rules" in raw and raw["rules"] is not None:
        try:
            rules = load_rules(raw["rules"], orders.default_delay_ms)
        except RuleError as exc:
            raise ConfigError(str(exc)) from exc

    engine = EngineConfig(host=session.host, fix_port=session.port)
    sessions = []
    if raw.get("sessions") is not None:
        engine, sessions = _load_sessions(raw, control_api)
        default_spec = _pick_default(engine, sessions)
        # The legacy, unscoped fields point at whichever session the
        # unscoped API routes act on, so old code paths keep working.
        session = default_spec.session
        orders = default_spec.orders
        rules = default_spec.rules
        price_band = default_spec.price_band

    return Config(
        session=session,
        storage=storage,
        logging=log_config,
        orders=orders,
        pricing=pricing,
        control_api=control_api,
        price_band=price_band,
        rules=rules,
        engine=engine,
        sessions=sessions,
        path=path,
    )


def _pick_default(engine: EngineConfig, sessions: list) -> SessionSpec:
    if len(sessions) == 1:
        return sessions[0]
    if engine.default_session:
        for spec in sessions:
            if spec.id == engine.default_session:
                return spec
    return sessions[0]


def _merge(base: dict, override: dict) -> dict:
    """Key-by-key override, one level deep."""
    merged = copy.deepcopy(base)
    merged.update(override or {})
    return merged


def _load_engine(section: dict) -> EngineConfig:
    engine = EngineConfig()
    if "host" in section:
        engine.host = _require_str(section, "engine", "host")
    if "fix_port" in section:
        port = _require_int(section, "engine", "fix_port", minimum=1)
        if port > 65535:
            raise ConfigError(f"config: 'engine.fix_port' must be <= 65535, "
                              f"got {port}")
        engine.fix_port = port
    if "default_session" in section and section["default_session"] is not None:
        engine.default_session = _require_str(section, "engine",
                                              "default_session")
    if "logon_timeout_sec" in section:
        engine.logon_timeout_sec = _require_number(
            section, "engine", "logon_timeout_sec", minimum=0
        )
    return engine


def _load_sessions(raw: dict, control_api: ControlApiConfig):
    """Build the session list from the multi-session config shape."""
    engine = _load_engine(_optional_section(raw, "engine"))
    entries = raw.get("sessions")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("config: 'sessions' must be a non-empty list")

    defaults = _optional_section(raw, "defaults")
    default_orders = _optional_section(defaults, "orders")
    default_band = _optional_section(defaults, "price_band")
    default_rules = defaults.get("rules")

    sessions: list = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config: sessions[{index}] must be a mapping, got {entry!r}"
            )
        session_id = entry.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ConfigError(
                f"config: sessions[{index}] is missing a non-empty 'id'"
            )
        session_id = session_id.strip()
        if not SESSION_ID_PATTERN.match(session_id):
            raise ConfigError(
                f"config: session {session_id!r}: 'id' may only contain "
                f"letters, digits, '_' and '-'"
            )

        where = f"sessions.{session_id}"
        for key in ("fix_version", "sender_comp_id", "target_comp_id"):
            if not entry.get(key):
                raise ConfigError(
                    f"config: session {session_id!r}: missing required key "
                    f"'{key}'"
                )
        fix_version = _require_str(entry, where, "fix_version")
        try:
            profile_for(fix_version)
        except UnknownFixVersion as exc:
            raise ConfigError(
                f"config: session {session_id!r}: {exc}"
            ) from exc

        port = engine.fix_port
        if "port" in entry:
            port = _require_int(entry, where, "port", minimum=1)
            if port > 65535:
                raise ConfigError(
                    f"config: session {session_id!r}: 'port' must be <= 65535,"
                    f" got {port}"
                )

        def scalar(key, default, minimum=None):
            source = entry if key in entry else defaults
            if key not in source:
                return default
            return _require_number(source, where, key, minimum=minimum)

        session_config = SessionConfig(
            fix_version=fix_version,
            sender_comp_id=_require_str(entry, where, "sender_comp_id"),
            target_comp_id=_require_str(entry, where, "target_comp_id"),
            host=entry.get("host") or engine.host,
            port=port,
            heartbeat_grace_pct=scalar("heartbeat_grace_pct", 20.0, 0),
            logout_timeout_sec=scalar("logout_timeout_sec", 10.0, 0),
        )
        resend_source = entry if "resend_mode" in entry else defaults
        if "resend_mode" in resend_source:
            resend_mode = _require_str(resend_source, where, "resend_mode")
            if resend_mode not in _VALID_RESEND_MODES:
                raise ConfigError(
                    f"config: session {session_id!r}: 'resend_mode' must be "
                    f"one of {', '.join(_VALID_RESEND_MODES)}, "
                    f"got {resend_mode!r}"
                )
            session_config.resend_mode = resend_mode

        try:
            orders = _load_orders(
                _merge(default_orders, _optional_section(entry, "orders"))
            )
            band = _load_price_band(
                _merge(default_band, _optional_section(entry, "price_band"))
            )
        except ConfigError as exc:
            raise ConfigError(f"config: session {session_id!r}: {exc}") from exc

        # rules replace wholesale; orders and price_band merge.
        rules_raw = entry["rules"] if "rules" in entry else default_rules
        rules = None
        if rules_raw is not None:
            try:
                rules = load_rules(rules_raw, orders.default_delay_ms)
            except RuleError as exc:
                raise ConfigError(
                    f"config: session {session_id!r}: {exc}"
                ) from exc

        sessions.append(SessionSpec(id=session_id, session=session_config,
                                    orders=orders, rules=rules,
                                    price_band=band))

    _validate_sessions(engine, sessions, control_api)
    return engine, sessions


def _validate_sessions(engine: EngineConfig, sessions: list,
                       control_api: ControlApiConfig) -> None:
    seen_ids = {}
    for spec in sessions:
        if spec.id in seen_ids:
            raise ConfigError(
                f"config: session {spec.id!r}: duplicate session id"
            )
        seen_ids[spec.id] = spec

    by_port_triple = {}
    for spec in sessions:
        key = (spec.port, spec.triple)
        if key in by_port_triple:
            other = by_port_triple[key]
            raise ConfigError(
                f"config: session {spec.id!r}: same "
                f"(fix_version, sender_comp_id, target_comp_id) as session "
                f"{other!r} on port {spec.port}; a Logon could not be routed "
                f"between them"
            )
        by_port_triple[key] = spec.id

    if control_api.enabled:
        for spec in sessions:
            if spec.port == control_api.port:
                raise ConfigError(
                    f"config: session {spec.id!r}: FIX port {spec.port} is "
                    f"also the control API port"
                )

    if engine.default_session and \
            engine.default_session not in seen_ids:
        raise ConfigError(
            f"config: 'engine.default_session' names {engine.default_session!r},"
            f" which is not one of the configured sessions "
            f"({', '.join(sorted(seen_ids))})"
        )


def _load_price_band(section: dict) -> PriceBandConfig:
    band = PriceBandConfig()
    if "enabled" in section:
        band.enabled = _require_bool(section, "price_band", "enabled")
    if "pct" in section:
        band.pct = _require_number(section, "price_band", "pct", minimum=0)
    if "mode" in section:
        mode = _require_str(section, "price_band", "mode")
        if mode not in _VALID_BAND_MODES:
            raise ConfigError(
                "config: 'price_band.mode' must be one of "
                f"{', '.join(_VALID_BAND_MODES)}, got {mode!r}"
            )
        band.mode = mode
    if "enforce_on_fallback" in section:
        band.enforce_on_fallback = _require_bool(
            section, "price_band", "enforce_on_fallback"
        )
    if "lookup_timeout_ms" in section:
        band.lookup_timeout_ms = _require_number(
            section, "price_band", "lookup_timeout_ms", minimum=0
        )
    if "on_no_reference" in section:
        value = _require_str(section, "price_band", "on_no_reference")
        if value not in _VALID_NO_REFERENCE:
            raise ConfigError(
                "config: 'price_band.on_no_reference' must be one of "
                f"{', '.join(_VALID_NO_REFERENCE)}, got {value!r}"
            )
        band.on_no_reference = value
    return band


def is_loopback(host: str) -> bool:
    """True for addresses that can only be reached from this machine."""
    if host is None:
        return False
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def _load_control_api(section: dict) -> ControlApiConfig:
    control = ControlApiConfig()
    if "enabled" in section:
        control.enabled = _require_bool(section, "control_api", "enabled")
    if "host" in section:
        control.host = _require_str(section, "control_api", "host")
    if "port" in section:
        port = _require_int(section, "control_api", "port", minimum=1)
        if port > 65535:
            raise ConfigError(
                f"config: 'control_api.port' must be <= 65535, got {port}"
            )
        control.port = port
    # The control API has no authentication by design, so it must never be
    # reachable from off this machine.
    if not is_loopback(control.host):
        raise ConfigError(
            f"config: 'control_api.host' must be a loopback address "
            f"(127.0.0.1, ::1 or localhost), got {control.host!r} -- the "
            f"control API has no authentication and must not be exposed"
        )
    return control


def _load_orders(section: dict) -> OrdersConfig:
    orders = OrdersConfig()
    if "default_delay_ms" in section:
        orders.default_delay_ms = _require_int(
            section, "orders", "default_delay_ms", minimum=0
        )
    if "send_pending_acks" in section:
        orders.send_pending_acks = _require_bool(
            section, "orders", "send_pending_acks"
        )
    if "replace_ack_ordstatus" in section:
        value = _require_str(section, "orders", "replace_ack_ordstatus")
        if value not in _VALID_REPLACE_ACK:
            raise ConfigError(
                "config: 'orders.replace_ack_ordstatus' must be one of "
                f"{', '.join(_VALID_REPLACE_ACK)}, got {value!r}"
            )
        orders.replace_ack_ordstatus = value
    return orders


def _load_pricing(section: dict) -> PricingConfig:
    pricing = PricingConfig()
    if "mode" in section:
        mode = _require_str(section, "pricing", "mode")
        if mode not in _VALID_PRICING_MODES:
            raise ConfigError(
                "config: 'pricing.mode' must be one of "
                f"{', '.join(_VALID_PRICING_MODES)}, got {mode!r}"
            )
        pricing.mode = mode
    if "provider" in section:
        provider = _require_str(section, "pricing", "provider")
        if provider not in _VALID_PROVIDERS:
            raise ConfigError(
                "config: 'pricing.provider' must be one of "
                f"{', '.join(_VALID_PROVIDERS)}, got {provider!r}"
            )
        pricing.provider = provider
    if "cache_seconds" in section:
        pricing.cache_seconds = _require_number(
            section, "pricing", "cache_seconds", minimum=0
        )
    if "timeout_sec" in section:
        pricing.timeout_sec = _require_number(
            section, "pricing", "timeout_sec", minimum=0
        )

    warm_raw = section.get("warm_symbols")
    if warm_raw is not None:
        if not isinstance(warm_raw, list) or not all(
                isinstance(entry, str) and entry.strip() for entry in warm_raw):
            raise ConfigError(
                "config: 'pricing.warm_symbols' must be a list of symbols, "
                f"got {warm_raw!r}"
            )
        pricing.warm_symbols = [entry.strip() for entry in warm_raw]

    static_raw = section.get("static")
    if static_raw is not None:
        if not isinstance(static_raw, dict):
            raise ConfigError("config: 'pricing.static' must be a mapping")
        static_prices = {}
        for symbol, value in static_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ConfigError(
                    f"config: 'pricing.static.{symbol}' must be a number, "
                    f"got {value!r}"
                )
            try:
                price = Decimal(str(value))
            except Exception as exc:
                raise ConfigError(
                    f"config: 'pricing.static.{symbol}' is not a number: {value!r}"
                ) from exc
            if price <= 0:
                raise ConfigError(
                    f"config: 'pricing.static.{symbol}' must be > 0, got {value!r}"
                )
            if str(symbol) == "default":
                pricing.static_default = price
            else:
                static_prices[str(symbol)] = price
        pricing.static = static_prices
    return pricing


# ------------------------------------------------------- the YAML key schema
#
# One description of the file the loader reads, used by two things that must
# never disagree with it: the generated key reference in the guide, and the
# test that stops the guide naming a key we do not actually read.
#
# Key names, types and defaults come from the dataclasses above, so they
# cannot drift.  The nesting is written out here because a YAML section and a
# dataclass are not the same shape -- `defaults:` feeds three of them.


@dataclass(frozen=True)
class SchemaKey:
    """One key the loader accepts."""

    name: str
    type: str
    default: object
    required: bool
    choices: tuple = ()
    note: str = ""

    @property
    def default_text(self) -> str:
        if self.required:
            return "required"
        if isinstance(self.default, bool):
            return "true" if self.default else "false"
        if self.default is None:
            return "none"
        if isinstance(self.default, (list, tuple)):
            return "[" + ", ".join(str(item) for item in self.default) + "]"
        if isinstance(self.default, dict):
            return "{}" if not self.default else str(self.default)
        return str(self.default)


@dataclass(frozen=True)
class ConfigSection:
    """One YAML section, its keys, and the sections nested inside it."""

    path: str
    summary: str
    keys: tuple = ()
    nests: tuple = ()


def _schema_keys(cls, only=None, exclude=(), choices=None, notes=None) -> tuple:
    """Keys from a dataclass, in declaration order, with their defaults."""
    choices = choices or {}
    notes = notes or {}
    rows = []
    for spec in dataclasses.fields(cls):
        if only is not None and spec.name not in only:
            continue
        if spec.name in exclude:
            continue
        required = (spec.default is dataclasses.MISSING
                    and spec.default_factory is dataclasses.MISSING)
        if spec.default is not dataclasses.MISSING:
            default = spec.default
        elif spec.default_factory is not dataclasses.MISSING:
            default = spec.default_factory()
        else:
            default = None
        rows.append(SchemaKey(
            name=spec.name,
            type=str(spec.type),
            default=default,
            required=required,
            choices=tuple(choices.get(spec.name, ())),
            note=notes.get(spec.name, ""),
        ))
    if only is not None:
        order = {name: index for index, name in enumerate(only)}
        rows.sort(key=lambda row: order[row.name])
    return tuple(rows)


#: Keys a session takes wherever a session is described.
_SESSION_KEYS = _schema_keys(
    SessionConfig,
    choices={"resend_mode": _VALID_RESEND_MODES},
    notes={
        "fix_version": "FIX.4.2 or FIX.4.4",
        "heartbeat_grace_pct": "how much longer than HeartBtInt we wait",
        "logout_timeout_sec": "how long we wait for a Logout reply",
        "resend_mode": "replay the real messages, or gap-fill them",
    },
)

CONFIG_SCHEMA = (
    ConfigSection(
        "engine",
        "Settings that belong to the process, not to one session. "
        "Multi-session configs only.",
        _schema_keys(EngineConfig, notes={
            "fix_port": "the port a session without its own port listens on",
            "default_session": "which session the unscoped API routes act on",
            "logon_timeout_sec": "how long a new connection may stay silent "
                                 "before we stop waiting for its Logon",
        }),
    ),
    ConfigSection(
        "session",
        "One session, the original shape. Use this or `sessions:`, never both.",
        _SESSION_KEYS,
    ),
    ConfigSection(
        "sessions[]",
        "A list of sessions. Each entry takes the session keys plus an `id`, "
        "and may override `orders`, `price_band` and `rules`.",
        (SchemaKey("id", "str", None, True, note="letters, digits, _ and -"),)
        + tuple(key for key in _SESSION_KEYS
                if key.name not in ("host", "port"))
        # In a session entry these are optional: the engine block supplies them.
        + (SchemaKey("host", "str", "engine.host", False),
           SchemaKey("port", "int", "engine.fix_port", False,
                     note="give a session its own acceptor")),
        nests=("orders", "price_band", "rules"),
    ),
    ConfigSection(
        "defaults",
        "What every session in `sessions:` starts from. An entry's own keys "
        "win; `orders` and `price_band` merge key by key, `rules` replace "
        "wholesale.",
        _schema_keys(SessionConfig,
                     only=("heartbeat_grace_pct", "logout_timeout_sec",
                           "resend_mode"),
                     choices={"resend_mode": _VALID_RESEND_MODES}),
        nests=("orders", "price_band", "rules"),
    ),
    ConfigSection(
        "storage",
        "Where state that outlives a run is kept.",
        _schema_keys(StorageConfig, notes={
            "seqnum_dir": "one JSON file per session",
            "evidence_dir": "one JSONL file per run",
            "msgstore_dir": "outbound messages, for a real resend",
        }),
    ),
    ConfigSection(
        "logging",
        "The two logs and how they are written.",
        _schema_keys(LoggingConfig,
                     choices={"engine_level": _VALID_LEVELS,
                              "fix_delimiter": ("|", "SOH")},
                     notes={"console": "also print the engine log to stdout"}),
    ),
    ConfigSection(
        "orders",
        "How orders are answered. Per session in a multi-session config.",
        _schema_keys(OrdersConfig,
                     choices={"replace_ack_ordstatus": _VALID_REPLACE_ACK},
                     notes={
                         "default_delay_ms": "delay before each report, unless "
                                             "a rule says otherwise",
                         "send_pending_acks": "send 150=6/E before the "
                                              "cancel or replace",
                     }),
    ),
    ConfigSection(
        "pricing",
        "Where a reference price comes from.",
        _schema_keys(PricingConfig,
                     choices={"mode": _VALID_PRICING_MODES,
                              "provider": _VALID_PROVIDERS},
                     notes={
                         "static": "symbol -> price, used in static mode and "
                                   "as the live fallback",
                         "static_default": "the price for a symbol not listed",
                         "warm_symbols": "fetched once at startup",
                     }),
    ),
    ConfigSection(
        "price_band",
        "The fat-finger collar. Per session in a multi-session config.",
        _schema_keys(PriceBandConfig,
                     choices={"mode": _VALID_BAND_MODES,
                              "on_no_reference": _VALID_NO_REFERENCE},
                     notes={
                         "pct": "how far from the reference is allowed",
                         "enforce_on_fallback": "apply the band when the "
                                                "price came from the fallback",
                     }),
    ),
    ConfigSection(
        "control_api",
        "The HTTP control API. Loopback addresses only.",
        _schema_keys(ControlApiConfig),
    ),
    ConfigSection(
        "rules[]",
        "How a symbol is answered, first match wins. Per session in a "
        "multi-session config.",
        (
            SchemaKey("name", "str", None, True),
            SchemaKey("match", "mapping", None, True,
                      choices=MATCH_KEYS,
                      note="one of symbol, first_letter, any"),
            SchemaKey("behavior", "str", None, True, choices=BEHAVIORS),
            SchemaKey("fills", "list", [], False,
                      note="shares or percentages, e.g. [400, \"10%\"]"),
            SchemaKey("then", "str", THEN_LEAVE, False, choices=THEN_VALUES),
            SchemaKey("delay_ms", "int", "orders.default_delay_ms", False),
            SchemaKey("reject_code", "str", "0", False,
                      note="the value for tag 103"),
            SchemaKey("text", "str", None, False, note="tag 58"),
            SchemaKey("price_band_pct", "float | \"off\"", None, False,
                      note="override the band for these symbols"),
        ),
    ),
)


def config_key_names() -> set:
    """Every key the loader accepts, flat, for the docs drift test."""
    names = set()
    for section in CONFIG_SCHEMA:
        names.add(section.path.replace("[]", ""))
        for key in section.keys:
            names.add(key.name)
        names.update(section.nests)
    names.update(MATCH_KEYS)
    return names
