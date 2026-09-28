"""Symbol behavior rules (PURE).

A rule says what the emulator does with an order once it has been acked:
fill it, partially fill it, cancel it, or reject it outright.  Rules are
matched in config order, first match wins, and the last rule must be a
catch-all so every order gets an answer.

Validation is strict and happens at config load: a rule set that could leave
an order unmatched, or that asks for a fill schedule that cannot be honoured,
is a configuration error, not a runtime surprise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

BEHAVIOR_FULL_FILL = "full_fill"
BEHAVIOR_PARTIAL_FILL = "partial_fill"
BEHAVIOR_CANCEL_AFTER_ACK = "cancel_after_ack"
BEHAVIOR_ACK_ONLY = "ack_only"
BEHAVIOR_REJECT = "reject"

BEHAVIORS = (
    BEHAVIOR_FULL_FILL,
    BEHAVIOR_PARTIAL_FILL,
    BEHAVIOR_CANCEL_AFTER_ACK,
    BEHAVIOR_ACK_ONLY,
    BEHAVIOR_REJECT,
)

THEN_LEAVE = "leave"
THEN_FILL_REST = "fill_rest"
THEN_CANCEL = "cancel"
THEN_VALUES = (THEN_LEAVE, THEN_FILL_REST, THEN_CANCEL)

#: A rule may switch the price band off entirely for its symbols.
BAND_OFF = "off"

MATCH_KEYS = ("symbol", "first_letter", "any")


class RuleError(Exception):
    """Raised for an invalid rule set.  Always names the offending rule."""


@dataclass(frozen=True)
class PercentFill:
    """A fill expressed as a percentage of the order quantity."""

    percent: float

    def shares(self, order_qty: int) -> int:
        return max(1, int(math.floor(order_qty * self.percent / 100.0)))

    def __str__(self) -> str:
        text = f"{self.percent:g}"
        return f"{text}%"


@dataclass(frozen=True)
class ShareFill:
    """A fill expressed as a whole number of shares."""

    quantity: int

    def shares(self, order_qty: int) -> int:
        return self.quantity

    def __str__(self) -> str:
        return str(self.quantity)


@dataclass
class Rule:
    name: str
    behavior: str
    symbol: str | None = None
    first_letter: tuple[str, str] | None = None
    any_symbol: bool = False
    fills: list = field(default_factory=list)
    then: str = THEN_LEAVE
    delay_ms: int | None = None
    reject_code: str = "0"
    text: str | None = None
    #: None = use the global band; a number = override the percent;
    #: BAND_OFF = no band check for symbols matching this rule.
    price_band_pct: object | None = None

    def matches(self, symbol: str) -> bool:
        if self.any_symbol:
            return True
        if self.symbol is not None:
            return symbol == self.symbol
        if self.first_letter is not None and symbol:
            first = symbol[0].upper()
            low, high = self.first_letter
            return low <= first <= high
        return False

    def resolve_fills(self, order_qty: int) -> list[int]:
        """Turn the configured fill schedule into share counts."""
        return [spec.shares(order_qty) for spec in self.fills]

    def delay_for(self, default_delay_ms: int) -> int:
        return default_delay_ms if self.delay_ms is None else self.delay_ms


class RuleSet:
    """An ordered list of rules; first match wins."""

    def __init__(self, rules: list[Rule], default_delay_ms: int = 500) -> None:
        self.rules = rules
        self.default_delay_ms = int(default_delay_ms)

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)

    def match(self, symbol: str) -> Rule | None:
        for rule in self.rules:
            if rule.matches(symbol):
                return rule
        return None


def _fail(rule_name, message: str):
    where = f"rule '{rule_name}'" if rule_name else "rule (unnamed)"
    raise RuleError(f"config: {where}: {message}")


def _parse_first_letter(rule_name, raw) -> tuple[str, str]:
    if not isinstance(raw, str):
        _fail(rule_name, f"'first_letter' must be a string, got {raw!r}")
    value = raw.strip().upper()
    if len(value) == 1 and value.isalpha():
        return value, value
    if len(value) == 3 and value[1] == "-" and value[0].isalpha() and value[2].isalpha():
        low, high = value[0], value[2]
        if low > high:
            _fail(rule_name, f"'first_letter' range is backwards: {raw!r}")
        return low, high
    _fail(rule_name, f"'first_letter' must be 'X' or 'X-Y', got {raw!r}")


def _parse_fill(rule_name, raw):
    if isinstance(raw, bool):
        _fail(rule_name, f"fill entry {raw!r} is not a quantity or percentage")
    if isinstance(raw, int):
        if raw <= 0:
            _fail(rule_name, f"share fill must be a positive integer, got {raw!r}")
        return ShareFill(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text.endswith("%"):
            try:
                percent = float(text[:-1])
            except ValueError:
                _fail(rule_name, f"percentage fill {raw!r} is not a number")
            if not 0 < percent <= 100:
                _fail(rule_name,
                      f"percentage fill must be >0 and <=100, got {raw!r}")
            return PercentFill(percent)
    _fail(rule_name,
          f"fill entry must be a positive integer or an 'N%' string, got {raw!r}")


def parse_rule(raw, index: int = 0) -> Rule:
    """Build one Rule from its config mapping."""
    if not isinstance(raw, dict):
        raise RuleError(f"config: rules[{index}] must be a mapping, got {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError(
            f"config: rules[{index}] is missing a non-empty 'name'"
        )

    match = raw.get("match")
    if not isinstance(match, dict):
        _fail(name, "'match' must be a mapping")
    unknown = [key for key in match if key not in MATCH_KEYS]
    if unknown:
        _fail(name, f"unknown match key(s): {', '.join(sorted(unknown))}")
    present = [key for key in MATCH_KEYS if key in match]
    if len(present) != 1:
        if not present:
            _fail(name, "'match' needs exactly one key "
                        f"({', '.join(MATCH_KEYS)}), found none")
        _fail(name, "'match' needs exactly one key, found "
                    f"{len(present)}: {', '.join(present)}")

    behavior = raw.get("behavior")
    if behavior not in BEHAVIORS:
        _fail(name, f"unknown behavior {behavior!r} "
                    f"(expected one of {', '.join(BEHAVIORS)})")

    rule = Rule(name=name, behavior=behavior)

    key = present[0]
    if key == "symbol":
        symbol = match["symbol"]
        if not isinstance(symbol, str) or not symbol.strip():
            _fail(name, f"'symbol' must be a non-empty string, got {symbol!r}")
        rule.symbol = symbol
    elif key == "first_letter":
        rule.first_letter = _parse_first_letter(name, match["first_letter"])
    else:
        if match["any"] is not True:
            _fail(name, "'any' must be true if present")
        rule.any_symbol = True

    fills_raw = raw.get("fills")
    if fills_raw is not None:
        if not isinstance(fills_raw, list) or not fills_raw:
            _fail(name, f"'fills' must be a non-empty list, got {fills_raw!r}")
        rule.fills = [_parse_fill(name, entry) for entry in fills_raw]
        percent_total = sum(
            spec.percent for spec in rule.fills if isinstance(spec, PercentFill)
        )
        if percent_total > 100:
            _fail(name, f"percentage fills sum to {percent_total:g}%, over 100%")
    elif behavior == BEHAVIOR_PARTIAL_FILL:
        _fail(name, "behavior 'partial_fill' requires a 'fills' list")

    then = raw.get("then")
    if then is not None:
        if then not in THEN_VALUES:
            _fail(name, f"unknown 'then' value {then!r} "
                        f"(expected one of {', '.join(THEN_VALUES)})")
        rule.then = then

    delay_ms = raw.get("delay_ms")
    if delay_ms is not None:
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
            _fail(name, f"'delay_ms' must be a non-negative integer, got {delay_ms!r}")
        rule.delay_ms = delay_ms

    reject_code = raw.get("reject_code")
    if reject_code is not None:
        if isinstance(reject_code, bool) or not isinstance(reject_code, (int, str)):
            _fail(name, f"'reject_code' must be a number or string, got {reject_code!r}")
        rule.reject_code = str(reject_code)

    band = raw.get("price_band_pct")
    if band is not None:
        if isinstance(band, str):
            if band.strip().lower() != BAND_OFF:
                _fail(name, f"'price_band_pct' must be a number or "
                            f"'{BAND_OFF}', got {band!r}")
            rule.price_band_pct = BAND_OFF
        elif isinstance(band, bool) or not isinstance(band, (int, float)):
            _fail(name, f"'price_band_pct' must be a number or "
                        f"'{BAND_OFF}', got {band!r}")
        elif band < 0:
            _fail(name, f"'price_band_pct' must be >= 0, got {band!r}")
        else:
            rule.price_band_pct = float(band)

    text = raw.get("text")
    if text is not None:
        if not isinstance(text, str):
            _fail(name, f"'text' must be a string, got {text!r}")
        rule.text = text
    elif behavior == BEHAVIOR_REJECT:
        rule.text = f"Rejected by OrderEcho rule {name}"

    return rule


def load_rules(raw_rules, default_delay_ms: int = 500) -> RuleSet:
    """Build and validate the whole rule set."""
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError("config: 'rules' must be a non-empty list")

    rules = [parse_rule(entry, index) for index, entry in enumerate(raw_rules)]

    names = {}
    for rule in rules:
        if rule.name in names:
            _fail(rule.name, "duplicate rule name")
        names[rule.name] = True

    if not rules[-1].any_symbol:
        _fail(
            rules[-1].name,
            "the last rule must be a catch-all ('match: {any: true}') so that "
            "every order matches a rule",
        )
    for rule in rules[:-1]:
        if rule.any_symbol:
            _fail(rule.name,
                  "a catch-all ('any: true') rule must be last; rules after it "
                  "can never match")

    return RuleSet(rules, default_delay_ms)
