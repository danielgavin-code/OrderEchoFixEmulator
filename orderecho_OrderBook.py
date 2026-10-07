"""Order book (PURE).

Inputs (an application message, the clock, and a price quote) in; outbound
messages and evidence out.  No sockets, no network, no wall clock.  Given the
same messages, the same config and the same quotes, this produces byte-identical
output except for the timestamps the clock supplies.

Orders live in memory.  With `orders.persist` on  every change is
also handed to an injected OrderStore, and a restarted engine reloads its
open orders through restore(); the book itself still never touches a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from orderecho_Codec import format_time
from orderecho_Pricing import SOURCE_LIMIT, SOURCE_STATIC_FALLBACK
from orderecho_Rules import (
    BAND_OFF,
    BEHAVIOR_ACK_ONLY,
    BEHAVIOR_CANCEL_AFTER_ACK,
    BEHAVIOR_FULL_FILL,
    BEHAVIOR_PARTIAL_FILL,
    BEHAVIOR_REJECT,
    THEN_CANCEL,
    THEN_FILL_REST,
)
from orderecho_FixVersion import (
    FIX42,
    CancelReject,
    ExecReport,
    Reason,
    ReportKind,
    ResponseTo,
)
from orderecho_Session import AppSend, Evidence, RequestPrice, SessionReject
from orderecho_Config import TIF_NAMES

# ---------------------------------------------------------------------- tags

TAG_ACCOUNT = 1
TAG_AVG_PX = 6
TAG_CL_ORD_ID = 11
TAG_CUM_QTY = 14
TAG_EXEC_ID = 17
TAG_EXEC_TRANS_TYPE = 20
TAG_HANDL_INST = 21
TAG_LAST_PX = 31
TAG_LAST_SHARES = 32
TAG_ORDER_ID = 37
TAG_ORDER_QTY = 38
TAG_ORD_STATUS = 39
TAG_ORD_TYPE = 40
TAG_ORIG_CL_ORD_ID = 41
TAG_PRICE = 44
TAG_SIDE = 54
TAG_SYMBOL = 55
TAG_TEXT = 58
TAG_TIME_IN_FORCE = 59
TAG_TRANSACT_TIME = 60
TAG_CXL_REJ_REASON = 102
TAG_ORD_REJ_REASON = 103
TAG_EXEC_TYPE = 150
TAG_LEAVES_QTY = 151
TAG_CXL_REJ_RESPONSE_TO = 434

MSG_NEW_ORDER_SINGLE = "D"
MSG_ORDER_CANCEL_REQUEST = "F"
MSG_ORDER_CANCEL_REPLACE_REQUEST = "G"
MSG_ORDER_STATUS_REQUEST = "H"
MSG_EXECUTION_REPORT = "8"
MSG_ORDER_CANCEL_REJECT = "9"

APP_MSG_TYPES = frozenset(
    {MSG_NEW_ORDER_SINGLE, MSG_ORDER_CANCEL_REQUEST,
     MSG_ORDER_CANCEL_REPLACE_REQUEST}
)

# ExecType (150) belongs to the profile now; OrdStatus (39) is the same in
# both versions we speak.
STATUS_NEW = "0"
STATUS_PARTIALLY_FILLED = "1"
STATUS_FILLED = "2"
STATUS_CANCELED = "4"
STATUS_REPLACED = "5"
STATUS_PENDING_CANCEL = "6"
STATUS_REJECTED = "8"
STATUS_PENDING_REPLACE = "E"
STATUS_DONE_FOR_DAY = "3"
STATUS_PENDING_NEW = "A"
STATUS_EXPIRED = "C"

STATUS_NAMES = {
    STATUS_NEW: "NEW",
    STATUS_PARTIALLY_FILLED: "PARTIALLY_FILLED",
    STATUS_FILLED: "FILLED",
    STATUS_CANCELED: "CANCELED",
    STATUS_REPLACED: "REPLACED",
    STATUS_PENDING_CANCEL: "PENDING_CANCEL",
    STATUS_REJECTED: "REJECTED",
    STATUS_PENDING_REPLACE: "PENDING_REPLACE",
    STATUS_DONE_FOR_DAY: "DONE_FOR_DAY",
    STATUS_PENDING_NEW: "PENDING_NEW",
    STATUS_EXPIRED: "EXPIRED",
}

# OrdRejReason (103) and CxlRejReason (102) values now live in the version
# profiles: the order book names a neutral Reason and the profile picks the
# code.  Only the session-level reject reasons are version-independent.

# SessionRejectReason (373)
SESSION_REJECT_REQUIRED_TAG_MISSING = "1"
SESSION_REJECT_VALUE_INCORRECT = "5"
SESSION_REJECT_INCORRECT_DATA_FORMAT = "6"

# Which values exist is the profile's business; which of those we actually
# support is ours, and is the same in both versions.
SUPPORTED_SIDES = frozenset({"1", "2", "5", "6"})
SUPPORTED_ORD_TYPES = frozenset({"1", "2"})
HANDL_INSTS = frozenset({"1", "2", "3"})
ORD_TYPE_MARKET = "1"
ORD_TYPE_LIMIT = "2"
TIME_IN_FORCE_DAY = "0"
TIF_GTC = "1"
TIF_IOC = "3"
TIF_FOK = "4"
TIF_GTX = "5"
TIF_GTD = "6"
TIF_LABELS = {"0": "DAY", "1": "GTC", "3": "IOC", "4": "FOK", "5": "GTX",
              "6": "GTD"}
TAG_EXPIRE_TIME = 126
TAG_EXPIRE_DATE = 432
TAG_ORD_STATUS_REQ_ID = 790

#: Scheduled event kinds that end an order without filling it.
EVENT_CANCEL = "cancel"              # a rule's cancel_after_ack / then: cancel
EVENT_IOC_CANCEL = "ioc_cancel"      # IOC: whatever did not fill at once
EVENT_FOK_KILL = "fok_kill"          # FOK: not fully fillable
EVENT_EXPIRE = "expire"              # GTD: ExpireTime reached
#:  events that close an order and need no price, so a market order
#: still waiting for its quote is not held back by one.  (A rule's own
#: cancel keeps its earlier behaviour and waits for the price like a fill.)
PRICE_FREE_EVENTS = frozenset({EVENT_IOC_CANCEL, EVENT_FOK_KILL,
                               EVENT_EXPIRE})

SIDE_NAMES = {"1": "BUY", "2": "SELL", "5": "SELLSHORT", "6": "SELLSHORTEXEMPT"}
ORD_TYPE_NAMES = {ORD_TYPE_MARKET: "MKT", ORD_TYPE_LIMIT: "LMT"}

class OrderActionError(Exception):
    """A manual order action that cannot be performed.

    Carries a machine-readable ``code`` so the control API can map it to an
    HTTP status without the pure core knowing anything about HTTP.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


CENTS = Decimal("0.01")
FOUR_DP = Decimal("0.0001")
ZERO = Decimal("0")

#: A market order's price is not known at ack time any more: the quote is
#: fetched in the background and delivered through on_price().
PRICE_PENDING = "pending"

BAND_PASS = "pass"
BAND_REJECT = "reject"
BAND_SKIPPED = "skipped"

SIDES_SELLING = frozenset({"2", "5", "6"})


def _fmt_pct(pct: float) -> str:
    return f"{pct:g}"


def evaluate_band(band_config, rule, side: str, limit_price: Decimal, quote):
    """Decide whether a limit price is inside the fat-finger collar.

    Pure: everything it needs is an argument.  Returns
    (outcome, detail, reference, pct) where outcome is pass/reject/skipped.
    """
    if band_config is None or not band_config.enabled:
        return BAND_SKIPPED, "price band disabled", None, None

    pct = band_config.pct
    if rule is not None and rule.price_band_pct is not None:
        if rule.price_band_pct == BAND_OFF:
            return (BAND_SKIPPED,
                    f"rule '{rule.name}' sets price_band_pct: off", None, None)
        pct = float(rule.price_band_pct)

    if quote is None:
        if getattr(band_config, "on_no_reference", "skip") == "reject":
            return (BAND_REJECT, "No reference price available", None, pct)
        return BAND_SKIPPED, "no reference price available", None, pct
    if quote.source == SOURCE_STATIC_FALLBACK and \
            not band_config.enforce_on_fallback:
        return (BAND_SKIPPED,
                f"reference is a fallback price ({quote.source})", quote, pct)

    reference = quote.price
    band = reference * Decimal(str(pct)) / Decimal("100")
    if band_config.mode == "both":
        breached = abs(limit_price - reference) > band
    elif side in SIDES_SELLING:
        breached = limit_price < reference - band
    else:
        breached = limit_price > reference + band

    if breached:
        return (
            BAND_REJECT,
            f"Limit {fmt_price(limit_price)} outside {_fmt_pct(pct)}% band of "
            f"ref {fmt_price(reference)} ({quote.source})",
            quote,
            pct,
        )
    return BAND_PASS, "", quote, pct


def fmt_price(value: Decimal) -> str:
    return str(Decimal(value).quantize(CENTS))


def fmt_avg(value: Decimal) -> str:
    return str(Decimal(value).quantize(FOUR_DP))


def _as_decimal(raw):
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    return value if value.is_finite() else None


def _as_int(raw):
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _parse_utc_timestamp(raw) -> bool:
    """True if *raw* looks like a FIX UTCTimestamp."""
    if not isinstance(raw, str):
        return False
    body, _, fraction = raw.partition(".")
    if len(body) != 17 or body[8] != "-":
        return False
    date, time_part = body[:8], body[9:]
    if not date.isdigit():
        return False
    pieces = time_part.split(":")
    if len(pieces) != 3 or not all(p.isdigit() and len(p) == 2 for p in pieces):
        return False
    if fraction and not fraction.isdigit():
        return False
    try:
        month, day = int(date[4:6]), int(date[6:8])
        hour, minute, second = (int(p) for p in pieces)
    except ValueError:
        return False
    return 1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 \
        and minute <= 59 and second <= 60


def _parse_expire_time(raw):
    """ExpireTime (126), a UTCTimestamp -> aware datetime, or None."""
    if not _parse_utc_timestamp(raw):
        return None
    body, _, fraction = raw.partition(".")
    try:
        when = datetime.strptime(body, "%Y%m%d-%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    if fraction:
        when += timedelta(microseconds=int(fraction[:6].ljust(6, "0")))
    return when


def _parse_expire_date(raw):
    """ExpireDate (432), YYYYMMDD -> the end of that UTC day, or None.

    FIX means a local market date; with no market calendar here the order
    lives through the whole UTC day and expires at its last millisecond.
    """
    if not isinstance(raw, str) or len(raw) != 8 or not raw.isdigit():
        return None
    try:
        day = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return day + timedelta(days=1) - timedelta(milliseconds=1)


def _iso(when):
    if when is None:
        return None
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _from_iso(text):
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------- state


@dataclass
class ScheduledEvent:
    due: object
    kind: str                 # "fill" | "fill_rest" | "cancel"
    shares: int = 0
    sequence: int = 0


@dataclass
class OrderState:
    order_id: str
    cl_ord_id: str
    symbol: str
    side: str
    ord_type: str
    order_qty: int
    price: Decimal | None = None
    account: str | None = None
    cl_ord_id_chain: list = field(default_factory=list)
    cum_qty: int = 0
    leaves_qty: int = 0
    notional: Decimal = ZERO
    ord_status: str = STATUS_NEW
    fill_price: Decimal | None = ZERO
    price_source: str = ""
    rule_name: str = ""
    scheduled: list = field(default_factory=list)
    closed: bool = False
    ack_time: object = None
    # Lifecycle states, TIF, persistence
    time_in_force: str = TIME_IN_FORCE_DAY
    expire_at: object = None          # GTD: when it expires (UTC)
    locked: bool = False              # "fill in progress": F/G get 102=0
    trade_date: str = ""              # YYYYMMDD (UTC) it was accepted
    restored: bool = False            # reloaded from the order store

    @property
    def price_pending(self) -> bool:
        return self.price_source == PRICE_PENDING

    @property
    def avg_px(self) -> Decimal:
        if self.cum_qty <= 0:
            return ZERO
        return self.notional / Decimal(self.cum_qty)

    @property
    def working_status(self) -> str:
        return STATUS_PARTIALLY_FILLED if self.cum_qty > 0 else STATUS_NEW

    def snapshot(self) -> dict:
        return {
            "order_id": self.order_id,
            "cl_ord_id": self.cl_ord_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_qty": str(self.order_qty),
            "cum_qty": str(self.cum_qty),
            "leaves_qty": str(self.leaves_qty),
            "avg_px": fmt_avg(self.avg_px),
            "ord_status": self.ord_status,
            "price_source": self.price_source,
            "rule_name": self.rule_name,
        }

    def describe(self) -> str:
        side = SIDE_NAMES.get(self.side, self.side)
        ord_type = ORD_TYPE_NAMES.get(self.ord_type, self.ord_type)
        price = "?" if self.fill_price is None else fmt_price(self.fill_price)
        return (
            f"{self.order_id} {self.symbol} {side} {self.order_qty} {ord_type} "
            f"px={price} ({self.price_source}) rule={self.rule_name}"
        )


class IdGenerator:
    """Hands out OrderIDs and ExecIDs that are unique across the engine.

    Several sessions share one of these, so an OrderID identifies an order
    without having to say which session it came from.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._order_seq = 0
        self._exec_seq = 0

    def next_order_id(self) -> str:
        self._order_seq += 1
        return f"O-{self.run_id}-{self._order_seq}"

    def next_exec_id(self) -> str:
        self._exec_seq += 1
        return f"E-{self.run_id}-{self._exec_seq}"


# --------------------------------------------------------------- order book


class OrderBook:
    """The order state machine.  Pure: time and prices are injected."""

    def __init__(self, orders_config, rules, clock, run_id: str,
                 price_band=None, profile=None, id_generator=None,
                 order_store=None) -> None:
        self.config = orders_config
        self.rules = rules
        self.clock = clock
        self.run_id = run_id
        self.price_band = price_band
        #: How what happened gets written in FIX.  Defaults to 4.2.
        self.profile = profile or FIX42
        #: Shared across sessions when the engine passes one in.
        self.ids = id_generator or IdGenerator(run_id)

        self._orders: dict[str, OrderState] = {}
        self._event_seq = 0
        self._used_cl_ord_ids: set[str] = set()
        #: §5: where every change goes when orders.persist is on.
        self.order_store = order_store

    # ------------------------------------------------------  options

    def _option(self, name, default):
        return getattr(self.config, name, default)

    @property
    def accepted_tifs(self) -> frozenset:
        names = self._option("time_in_force", ["day"]) or ["day"]
        return frozenset(TIF_NAMES[name] for name in names if name in TIF_NAMES)

    @property
    def answers_status_requests(self) -> bool:
        return bool(self._option("status_requests", False))

    # ------------------------------------------------------------ accessors

    def get_order(self, order_id: str):
        return self._orders.get(order_id)

    def orders(self) -> list:
        return list(self._orders.values())

    def has_pending_events(self) -> bool:
        return any(order.scheduled for order in self._orders.values())

    # -------------------------------------------------------------- helpers

    def _next_order_id(self) -> str:
        return self.ids.next_order_id()

    def _next_exec_id(self) -> str:
        return self.ids.next_exec_id()

    def _find_by_cl_ord_id(self, cl_ord_id: str):
        """Look up by an order's *current* ClOrdID, open orders first."""
        if cl_ord_id is None:
            return None
        for order in self._orders.values():
            if not order.closed and order.cl_ord_id == cl_ord_id:
                return order
        for order in self._orders.values():
            if order.cl_ord_id == cl_ord_id:
                return order
        return None

    def _execution_report(self, order: OrderState, kind: ReportKind,
                          last_shares: int = 0, last_px: Decimal = ZERO,
                          orig_cl_ord_id: str | None = None,
                          ord_status: str | None = None,
                          reason: Reason | None = None,
                          reason_code: str | None = None,
                          text: str | None = None) -> AppSend:
        """Describe what happened; let the profile write it in FIX."""
        report = ExecReport(
            kind=kind,
            order_id=order.order_id,
            cl_ord_id=order.cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            exec_id=self._next_exec_id(),
            account=order.account or None,
            symbol=order.symbol,
            side=order.side,
            order_qty=str(order.order_qty),
            ord_type=order.ord_type,
            ord_status=(ord_status if ord_status is not None
                        else order.ord_status),
            price=fmt_price(order.price) if order.price is not None else None,
            last_qty=str(last_shares),
            last_px=fmt_price(last_px),
            leaves_qty=str(order.leaves_qty),
            cum_qty=str(order.cum_qty),
            avg_px=fmt_avg(order.avg_px),
            transact_time=format_time(self.clock.now()),
            reason=reason,
            reason_code=reason_code,
            text=text,
        )
        return AppSend(MSG_EXECUTION_REPORT,
                       self.profile.render_exec_report(report))

    def _cancel_reject(self, cl_ord_id: str, orig_cl_ord_id: str,
                       ord_status: str, response_to: ResponseTo,
                       reason: Reason, text: str,
                       order_id: str = "NONE") -> AppSend:
        reject = CancelReject(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            ord_status=ord_status,
            response_to=response_to,
            reason=reason,
            text=text,
        )
        return AppSend(MSG_ORDER_CANCEL_REJECT,
                       self.profile.render_cancel_reject(reject))

    def _order_events(self, order: OrderState, line: str) -> list:
        """The §9 pair: an evidence snapshot plus one human log line.

        Every state change of an order passes through here, so this is also
        where it is persisted .
        """
        self._persist(order)
        return [
            Evidence(
                "order",
                f"order {order.order_id} {STATUS_NAMES.get(order.ord_status, order.ord_status)}",
                order=order.snapshot(),
            ),
            Evidence("order lifecycle", line),
        ]

    def _order_line(self, order: OrderState) -> str:
        status = STATUS_NAMES.get(order.ord_status, order.ord_status)
        return f"{'ORDER':<5} {order.describe()} -> {status}"

    def _fill_line(self, order: OrderState, shares: int, price: Decimal) -> str:
        status = STATUS_NAMES.get(order.ord_status, order.ord_status)
        return (
            f"{'FILL':<5} {order.order_id} {shares} @ {fmt_price(price)} "
            f"cum={order.cum_qty} leaves={order.leaves_qty} "
            f"avg={fmt_avg(order.avg_px)} -> {status}"
        )

    def _band_actions(self, cl_ord_id, side, ord_type, price, outcome,
                      detail, quote, pct) -> list:
        side_name = SIDE_NAMES.get(side, side)
        type_name = ORD_TYPE_NAMES.get(ord_type, ord_type)
        reference = (f"ref={fmt_price(quote.price)} ({quote.source})"
                     if quote is not None else "ref=none")
        band = f"band=\u00b1{_fmt_pct(pct)}%" if pct is not None else "band=off"
        if outcome == BAND_SKIPPED:
            tail = f"-> SKIPPED ({detail})"
        elif outcome == BAND_REJECT:
            tail = "-> REJECT"
        else:
            tail = "-> PASS"
        line = (f"{'BAND':<5} {cl_ord_id} {side_name} {type_name} "
                f"{fmt_price(price)} {reference} {band} {tail}")
        if not detail:
            detail = (f"limit {fmt_price(price)} inside {_fmt_pct(pct)}% band "
                      f"of ref {fmt_price(quote.price)} ({quote.source})"
                      if quote is not None else "no band check")
        # A skipped band is worth a WARNING: the collar was not applied.
        line_event = ("price band warning" if outcome == BAND_SKIPPED
                      else "price band")
        return [
            Evidence(f"price band {outcome}", detail),
            Evidence(line_event, line),
        ]

    def _drop_scheduled(self, order: OrderState, why: str) -> list:
        if not order.scheduled:
            return []
        count = len(order.scheduled)
        order.scheduled = []
        return [
            Evidence(
                "scheduled events dropped",
                f"{count} pending event(s) for {order.order_id} dropped: {why}",
            )
        ]

    # ------------------------------------------------------------- dispatch

    def on_app_message(self, msg, market_price=None) -> list:
        msg_type = msg.msg_type
        reject = self._validate_structural(msg, msg_type)
        if reject is not None:
            return [reject]
        if msg_type == MSG_NEW_ORDER_SINGLE:
            return self._on_new_order(msg, market_price)
        if msg_type == MSG_ORDER_CANCEL_REQUEST:
            return self._on_cancel_request(msg)
        if msg_type == MSG_ORDER_CANCEL_REPLACE_REQUEST:
            return self._on_replace_request(msg, market_price)
        if msg_type == MSG_ORDER_STATUS_REQUEST:
            return self._on_status_request(msg)
        return []  # pragma: no cover - the session only routes D/F/G/H here

    # ----------------------------------------------------------- validation

    def _validate_structural(self, msg, msg_type):
        """Required tags, then data format, then enumerations."""
        ref_seq = msg.seq_num

        for tag in self.profile.required_for(msg_type):
            if msg.get(tag) is None:
                return SessionReject(
                    ref_seq, tag, SESSION_REJECT_REQUIRED_TAG_MISSING,
                    f"Required tag missing: {tag}",
                )
        ord_type = msg.get(TAG_ORD_TYPE)
        if ord_type == ORD_TYPE_LIMIT and msg.get(TAG_PRICE) is None:
            return SessionReject(
                ref_seq, TAG_PRICE, SESSION_REJECT_REQUIRED_TAG_MISSING,
                "Required tag missing: 44",
            )

        qty_raw = msg.get(TAG_ORDER_QTY)
        if qty_raw is not None and _as_decimal(qty_raw) is None:
            return SessionReject(
                ref_seq, TAG_ORDER_QTY, SESSION_REJECT_INCORRECT_DATA_FORMAT,
                f"OrderQty is not a number: {qty_raw}",
            )
        price_raw = msg.get(TAG_PRICE)
        if price_raw is not None and _as_decimal(price_raw) is None:
            return SessionReject(
                ref_seq, TAG_PRICE, SESSION_REJECT_INCORRECT_DATA_FORMAT,
                f"Price is not a number: {price_raw}",
            )
        transact_time = msg.get(TAG_TRANSACT_TIME)
        if transact_time is not None and not _parse_utc_timestamp(transact_time):
            return SessionReject(
                ref_seq, TAG_TRANSACT_TIME, SESSION_REJECT_INCORRECT_DATA_FORMAT,
                f"TransactTime is not a UTC timestamp: {transact_time}",
            )

        version = self.profile.label
        side = msg.get(TAG_SIDE)
        if side is not None and side not in self.profile.sides:
            return SessionReject(
                ref_seq, TAG_SIDE, SESSION_REJECT_VALUE_INCORRECT,
                f"Side not a {version} value: {side}",
            )
        if ord_type is not None and ord_type not in self.profile.ord_types:
            return SessionReject(
                ref_seq, TAG_ORD_TYPE, SESSION_REJECT_VALUE_INCORRECT,
                f"OrdType not a {version} value: {ord_type}",
            )
        handl_inst = msg.get(TAG_HANDL_INST)
        if handl_inst is not None and handl_inst not in self.profile.handl_insts:
            return SessionReject(
                ref_seq, TAG_HANDL_INST, SESSION_REJECT_VALUE_INCORRECT,
                f"HandlInst not a {version} value: {handl_inst}",
            )
        time_in_force = msg.get(TAG_TIME_IN_FORCE)
        if time_in_force is not None and \
                time_in_force not in self.profile.times_in_force:
            return SessionReject(
                ref_seq, TAG_TIME_IN_FORCE, SESSION_REJECT_VALUE_INCORRECT,
                f"TimeInForce not a {version} value: {time_in_force}",
            )
        # §3: a GTD order we accept must say when it expires.
        if msg_type == MSG_NEW_ORDER_SINGLE and time_in_force == TIF_GTD \
                and TIF_GTD in self.accepted_tifs:
            expire_time = msg.get(TAG_EXPIRE_TIME)
            expire_date = msg.get(TAG_EXPIRE_DATE)
            if expire_time is None and expire_date is None:
                return SessionReject(
                    ref_seq, TAG_EXPIRE_TIME,
                    SESSION_REJECT_REQUIRED_TAG_MISSING,
                    "Required tag missing: 126 (GTD needs ExpireTime 126 or "
                    "ExpireDate 432)",
                )
            if expire_time is not None and \
                    _parse_expire_time(expire_time) is None:
                return SessionReject(
                    ref_seq, TAG_EXPIRE_TIME,
                    SESSION_REJECT_INCORRECT_DATA_FORMAT,
                    f"ExpireTime is not a UTC timestamp: {expire_time}",
                )
            if expire_time is None and _parse_expire_date(expire_date) is None:
                return SessionReject(
                    ref_seq, TAG_EXPIRE_DATE,
                    SESSION_REJECT_INCORRECT_DATA_FORMAT,
                    f"ExpireDate is not a YYYYMMDD date: {expire_date}",
                )
        return None

    def _business_failure(self, msg, require_order_fields: bool):
        """Return (reject_code, text) for the first business rule broken."""
        if require_order_fields:
            qty = _as_decimal(msg.get(TAG_ORDER_QTY))
            if qty is None or qty <= 0 or qty != qty.to_integral_value():
                return Reason.BAD_QUANTITY, \
                    f"OrderQty must be a positive whole number: {msg.get(TAG_ORDER_QTY)}"

        side = msg.get(TAG_SIDE)
        if side is not None and side not in SUPPORTED_SIDES:
            return Reason.UNSUPPORTED_CHARACTERISTIC, f"Side not supported: {side}"

        if require_order_fields:
            ord_type = msg.get(TAG_ORD_TYPE)
            if ord_type is not None and ord_type not in SUPPORTED_ORD_TYPES:
                return Reason.UNSUPPORTED_CHARACTERISTIC, "OrdType not supported"
            price_raw = msg.get(TAG_PRICE)
            if price_raw is not None:
                price = _as_decimal(price_raw)
                if price is None or price <= 0:
                    return Reason.BAD_PRICE, f"Price must be > 0: {price_raw}"

        time_in_force = msg.get(TAG_TIME_IN_FORCE)
        if time_in_force is not None and \
                time_in_force not in self.accepted_tifs:
            return Reason.UNSUPPORTED_CHARACTERISTIC, "TimeInForce not supported"
        return None, None

    def _account_failure(self, msg):
        """§4: orders.valid_accounts, when set, must include tag 1 if it is
        present.  An order without an Account is not checked."""
        valid = self._option("valid_accounts", []) or []
        if not valid:
            return None
        account = msg.get(TAG_ACCOUNT)
        if account is None or account == "":
            return None
        if account not in valid:
            return f"Unknown account {account}"
        return None

    # ------------------------------------------------ D: NewOrderSingle

    def _on_new_order(self, msg, market_price) -> list:
        cl_ord_id = msg.get(TAG_CL_ORD_ID)
        symbol = msg.get(TAG_SYMBOL)
        side = msg.get(TAG_SIDE)
        ord_type = msg.get(TAG_ORD_TYPE)
        qty_decimal = _as_decimal(msg.get(TAG_ORDER_QTY))
        price = _as_decimal(msg.get(TAG_PRICE)) if msg.get(TAG_PRICE) else None

        order = OrderState(
            order_id=self._next_order_id(),
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            ord_type=ord_type,
            order_qty=int(qty_decimal) if qty_decimal is not None
            and qty_decimal == qty_decimal.to_integral_value() else 0,
            price=price,
            account=msg.get(TAG_ACCOUNT),
            time_in_force=msg.get(TAG_TIME_IN_FORCE) or TIME_IN_FORCE_DAY,
            trade_date=self.clock.now().astimezone(timezone.utc).strftime(
                "%Y%m%d"),
        )
        if order.time_in_force == TIF_GTD:
            order.expire_at = (_parse_expire_time(msg.get(TAG_EXPIRE_TIME))
                               if msg.get(TAG_EXPIRE_TIME) is not None
                               else _parse_expire_date(msg.get(TAG_EXPIRE_DATE)))
        order.cl_ord_id_chain.append(cl_ord_id)
        order.leaves_qty = order.order_qty
        self._orders[order.order_id] = order

        reason, text = self._business_failure(msg, require_order_fields=True)
        if reason is None and cl_ord_id in self._used_cl_ord_ids:
            reason, text = Reason.DUPLICATE_CL_ORD_ID, "Duplicate ClOrdID"
        self._used_cl_ord_ids.add(cl_ord_id)
        if reason is None:
            account_text = self._account_failure(msg)
            if account_text is not None:
                reason, text = Reason.UNKNOWN_ACCOUNT, account_text
        if reason is not None:
            return self._reject_order(order, reason, text)

        rule = self.rules.match(symbol) if self.rules is not None else None
        if rule is None:
            return self._reject_order(
                order, Reason.UNSUPPORTED_CHARACTERISTIC,
                f"No rule matched symbol {symbol}"
            )
        order.rule_name = rule.name

        actions: list = []

        if ord_type == ORD_TYPE_LIMIT:
            order.fill_price = price
            order.price_source = SOURCE_LIMIT
            # The fat-finger collar runs before the rule's behavior takes
            # effect, so a silly price never reaches an ack or a schedule.
            outcome, detail, quote, pct = evaluate_band(
                self.price_band, rule, side, price, market_price
            )
            actions.extend(self._band_actions(cl_ord_id, side, ord_type, price,
                                              outcome, detail, quote, pct))
            if outcome == BAND_REJECT:
                # No quote at all is a different reason from a breached band,
                # and the versions spell them differently (spec 5.4).
                reason = (Reason.NO_REFERENCE if quote is None
                          else Reason.BAND_BREACH)
                return actions + self._reject_order(order, reason, detail)
            actions.append(
                Evidence(
                    "price resolved",
                    f"{symbol} {fmt_price(order.fill_price)} "
                    f"({order.price_source})",
                )
            )
        else:
            # Market order: ack now, price later (the transport is already
            # fetching a quote and will bring it back through on_price).
            order.fill_price = None
            order.price_source = PRICE_PENDING
            actions.append(
                Evidence("price pending",
                         f"{symbol} market order acked before its quote")
            )

        # Announced after the band decision, so the log reads in the order the
        # checks actually happen (spec 3.2).
        actions.append(
            Evidence("rule matched", f"{symbol} matched rule '{rule.name}' "
                                     f"({rule.behavior})")
        )

        if rule.behavior == BEHAVIOR_REJECT:
            return actions + self._reject_order(
                order, Reason.RULE_REJECT, rule.text,
                reason_code=rule.reject_code,
            )

        if self._option("send_pending_new", False):
            # §4: Pending New first, then the normal acknowledgement.
            order.ord_status = STATUS_PENDING_NEW
            actions.append(self._execution_report(order,
                                                  ReportKind.PENDING_NEW))
            actions.extend(self._order_events(order, self._order_line(order)))
        order.ord_status = STATUS_NEW
        order.ack_time = self.clock.now()
        if (order.time_in_force in (TIF_IOC, TIF_FOK)
                and not self._option("ioc_fok_ack", True)):
            # orders.ioc_fok_ack off: the outcome is the first answer.
            actions.append(Evidence(
                "ack skipped",
                f"{order.order_id} {TIF_LABELS.get(order.time_in_force)}: "
                f"orders.ioc_fok_ack is off, the outcome is the first report"))
        else:
            actions.append(self._execution_report(order, ReportKind.ACK))
        actions.extend(self._order_events(order, self._order_line(order)))
        actions.extend(self._schedule(order, rule))
        self._persist(order)               # now with its schedule
        if order.price_pending:
            actions.append(RequestPrice(order.order_id, symbol))
        if order.time_in_force in (TIF_IOC, TIF_FOK) and order.scheduled:
            # IOC and FOK are decided now, not on the next timer tick.
            actions.extend(self._fire_due(self.clock.now(), only_order=order))
        return actions

    def _reject_order(self, order: OrderState, reason: Reason,
                      text: str | None, reason_code: str | None = None) -> list:
        order.ord_status = STATUS_REJECTED
        order.leaves_qty = 0
        order.closed = True
        order.scheduled = []
        report = self._execution_report(
            order, ReportKind.REJECTED, reason=reason,
            reason_code=reason_code, text=text,
        )
        return [report] + self._order_events(order, self._order_line(order))

    def _schedule(self, order: OrderState, rule) -> list:
        if order.time_in_force in (TIF_IOC, TIF_FOK):
            return self._schedule_immediate(order, rule)
        actions = self._schedule_rule(order, rule)
        if order.time_in_force == TIF_GTD and order.expire_at is not None:
            self._event_seq += 1
            order.scheduled.append(ScheduledEvent(
                due=order.expire_at, kind=EVENT_EXPIRE,
                sequence=self._event_seq))
            actions.append(Evidence(
                "order expiry scheduled",
                f"{order.order_id} GTD expires at "
                f"{format_time(order.expire_at)}"))
        return actions

    def _schedule_immediate(self, order: OrderState, rule) -> list:
        """§3: IOC and FOK act on the rule's immediate outcome only."""
        now = self.clock.now()
        planned: list = []
        if order.time_in_force == TIF_IOC:
            if rule.behavior == BEHAVIOR_FULL_FILL:
                planned.append(("fill", order.order_qty))
            elif rule.behavior == BEHAVIOR_PARTIAL_FILL:
                first = rule.resolve_fills(order.order_qty)[:1]
                planned.extend(("fill", shares) for shares in first)
            planned.append((EVENT_IOC_CANCEL, 0))
        else:
            fillable = (rule.behavior == BEHAVIOR_FULL_FILL or (
                rule.behavior == BEHAVIOR_PARTIAL_FILL
                and rule.then == THEN_FILL_REST))
            planned.append(("fill", order.order_qty) if fillable
                           else (EVENT_FOK_KILL, 0))
        for kind, shares in planned:
            self._event_seq += 1
            order.scheduled.append(ScheduledEvent(
                due=now, kind=kind, shares=shares, sequence=self._event_seq))
        label = TIF_LABELS.get(order.time_in_force, order.time_in_force)
        return [Evidence(
            "order events scheduled",
            f"{len(planned)} immediate event(s) for {order.order_id} "
            f"({label}: {', '.join(kind for kind, _ in planned)})")]

    def _schedule_rule(self, order: OrderState, rule) -> list:
        delay_ms = rule.delay_for(self.rules.default_delay_ms)
        step = timedelta(milliseconds=delay_ms)
        base = self.clock.now()
        planned: list = []

        if rule.behavior == BEHAVIOR_FULL_FILL:
            planned.append(("fill", order.order_qty))
        elif rule.behavior == BEHAVIOR_PARTIAL_FILL:
            for shares in rule.resolve_fills(order.order_qty):
                planned.append(("fill", shares))
            if rule.then == THEN_FILL_REST:
                planned.append(("fill_rest", 0))
            elif rule.then == THEN_CANCEL:
                planned.append(("cancel", 0))
        elif rule.behavior == BEHAVIOR_CANCEL_AFTER_ACK:
            planned.append(("cancel", 0))
        elif rule.behavior == BEHAVIOR_ACK_ONLY:
            planned = []

        for index, (kind, shares) in enumerate(planned, start=1):
            self._event_seq += 1
            order.scheduled.append(
                ScheduledEvent(
                    due=base + step * index,
                    kind=kind,
                    shares=shares,
                    sequence=self._event_seq,
                )
            )
        if not planned:
            return []
        return [
            Evidence(
                "order events scheduled",
                f"{len(planned)} event(s) for {order.order_id} every {delay_ms}ms",
            )
        ]

    # ------------------------------------------------------------- timers

    def on_timer(self) -> list:
        return self._fire_due(self.clock.now())

    def on_price(self, order_id: str, quote, fire_due: bool = True) -> list:
        """A background quote has arrived for a market order (spec 4).

        Anything that came due while the price was pending fires now, in the
        order it was scheduled.
        """
        order = self._orders.get(order_id)
        if order is None:
            return [Evidence("price ignored", f"no such order {order_id}")]
        if not order.price_pending:
            return [
                Evidence("price ignored",
                         f"{order_id} is already priced "
                         f"({order.price_source})")
            ]

        delay = ""
        if order.ack_time is not None:
            seconds = (self.clock.now() - order.ack_time).total_seconds()
            delay = f", {seconds:.3f}s after the ack"
        order.fill_price = quote.price
        order.price_source = quote.source
        actions = [
            Evidence(
                "price resolved",
                f"{order.symbol} {fmt_price(quote.price)} ({quote.source})"
                f"{delay}",
            )
        ]
        if fire_due and not order.closed:
            actions.extend(self._fire_due(self.clock.now(), only_order=order))
        return actions

    def _fire_due(self, now, only_order: OrderState | None = None) -> list:
        due: list = []
        for order in self._orders.values():
            if only_order is not None and order is not only_order:
                continue
            events = sorted(order.scheduled,
                            key=lambda item: (item.due, item.sequence))
            for event in events:
                if event.due > now:
                    continue
                if order.price_pending and event.kind not in PRICE_FREE_EVENTS:
                    # Nothing can fill at an unknown price; this and every
                    # later event wait for on_price() (PRICE_FREE_EVENTS do
                    # not).
                    break
                due.append((event.due, event.sequence, order, event))
        due.sort(key=lambda item: (item[0], item[1]))

        actions: list = []
        for _due, _sequence, order, event in due:
            if event not in order.scheduled:
                continue
            order.scheduled.remove(event)
            if event.kind == EVENT_CANCEL:
                actions.extend(self._unsolicited_cancel(order))
            elif event.kind == EVENT_IOC_CANCEL:
                actions.extend(self._unsolicited_cancel(
                    order, "IOC remainder canceled",
                    why="IOC remainder canceled"))
            elif event.kind == EVENT_FOK_KILL:
                actions.extend(self._unsolicited_cancel(
                    order, "FOK not fully fillable",
                    why="FOK not fully fillable"))
            elif event.kind == EVENT_EXPIRE:
                actions.extend(self._close_order(
                    order, STATUS_EXPIRED, ReportKind.EXPIRED,
                    "GTD order expired", "order expired (GTD)"))
            else:
                shares = order.leaves_qty if event.kind == "fill_rest" \
                    else event.shares
                actions.extend(self._apply_fill(order, shares))
        return actions

    def _apply_fill(self, order: OrderState, shares: int,
                    price: Decimal | None = None) -> list:
        if order.closed:
            return [
                Evidence(
                    "fill skipped",
                    f"{order.order_id} is closed "
                    f"({STATUS_NAMES.get(order.ord_status, order.ord_status)})",
                )
            ]
        actions: list = []
        requested = int(shares)
        filled = min(requested, order.leaves_qty)
        if filled != requested:
            actions.append(
                Evidence(
                    "fill clamped",
                    f"{order.order_id} fill of {requested} clamped to "
                    f"{filled} (leaves={order.leaves_qty})",
                )
            )
        if filled <= 0:
            actions.append(
                Evidence("fill skipped",
                         f"{order.order_id} fill of {requested} resolves to 0 shares")
            )
            return actions

        if price is None:
            price = order.fill_price
        if price is None:
            return actions + [
                Evidence("fill skipped",
                         f"{order.order_id} has no price yet")
            ]
        order.cum_qty += filled
        order.leaves_qty -= filled
        order.notional += Decimal(filled) * price
        if order.locked:
            # The fill that was "in progress" has happened.
            order.locked = False
            actions.append(Evidence("order unlocked",
                                    f"{order.order_id} unlocked by a fill"))

        if order.leaves_qty == 0:
            order.ord_status = STATUS_FILLED
            order.closed = True
            kind = ReportKind.FILL
        else:
            order.ord_status = STATUS_PARTIALLY_FILLED
            kind = ReportKind.PARTIAL_FILL

        actions.append(
            self._execution_report(order, kind, last_shares=filled,
                                   last_px=price)
        )
        actions.extend(
            self._order_events(order, self._fill_line(order, filled, price))
        )
        if order.closed:
            actions.extend(self._drop_scheduled(order, "order fully filled"))
        return actions

    def _unsolicited_cancel(self, order: OrderState, text: str | None = None,
                            why: str = "order canceled by rule") -> list:
        if order.closed:
            return [
                Evidence("cancel skipped",
                         f"{order.order_id} is already closed")
            ]
        if text is None:
            text = f"Canceled by OrderEcho rule {order.rule_name}"
        return self._close_order(order, STATUS_CANCELED, ReportKind.CANCELED,
                                 text, why)

    def _close_order(self, order: OrderState, status: str, kind: ReportKind,
                     text: str, why: str) -> list:
        """End an open order without a fill: cancel, expiry, done for day."""
        if order.closed:
            return [Evidence("close skipped",
                             f"{order.order_id} is already closed")]
        order.ord_status = status
        order.leaves_qty = 0
        order.closed = True
        order.locked = False
        actions = [self._execution_report(order, kind, text=text)]
        actions.extend(self._order_events(order, self._order_line(order)))
        actions.extend(self._drop_scheduled(order, why))
        return actions

    # ------------------------------------------------------ manual actions
    #
    # The control API drives these.  They are ordinary order-book operations
    # producing ordinary actions: nothing here bypasses the state machine, and
    # the caller still has to put the results through the session.

    def _require_open(self, order_id: str) -> OrderState:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderActionError("not_found", f"No such order: {order_id}")
        if order.closed:
            raise OrderActionError(
                "order_closed",
                f"Order {order_id} is "
                f"{STATUS_NAMES.get(order.ord_status, order.ord_status)}",
            )
        return order

    @staticmethod
    def _manual_price(order: OrderState, price):
        if price is None:
            if order.price_pending:
                raise OrderActionError(
                    "price_pending",
                    f"Order {order.order_id} is still waiting for its market "
                    f"price; supply an explicit price to fill it now",
                )
            return order.fill_price
        value = _as_decimal(price)
        if value is None:
            raise OrderActionError("invalid_request",
                                   f"price is not a number: {price}")
        if value <= 0:
            raise OrderActionError("invalid_request",
                                   f"price must be > 0, got {price}")
        return value.quantize(CENTS)

    def precheck(self, order_id: str, qty=None, price=None,
                 check_price: bool = False) -> OrderState:
        """Validate a manual action without changing anything.

        The control API calls this before it checks the session state, so an
        unknown or closed order is reported as such whether or not anyone is
        logged on (spec 3.2).
        """
        order = self._require_open(order_id)
        if qty is not None:
            self._validate_qty(order, qty)
        if check_price:
            self._manual_price(order, price)
        return order

    def _validate_qty(self, order: OrderState, qty) -> int:
        quantity = _as_int(qty)
        if quantity is None:
            raise OrderActionError("invalid_request",
                                   f"qty is not a whole number: {qty}")
        if quantity <= 0:
            raise OrderActionError("invalid_request",
                                   f"qty must be > 0, got {quantity}")
        if quantity > order.leaves_qty:
            raise OrderActionError(
                "invalid_request",
                f"qty {quantity} exceeds LeavesQty {order.leaves_qty}",
            )
        return quantity

    def manual_fill(self, order_id: str, qty, price=None) -> list:
        """Fill *qty* shares by hand, at *price* or the order's own price."""
        order = self._require_open(order_id)
        quantity = self._validate_qty(order, qty)
        fill_price = self._manual_price(order, price)
        return [
            Evidence("manual fill",
                     f"{order.order_id} {quantity} @ {fmt_price(fill_price)} "
                     f"via control API")
        ] + self._apply_fill(order, quantity, price=fill_price)

    def manual_fill_rest(self, order_id: str, price=None) -> list:
        """Fill everything still outstanding."""
        order = self._require_open(order_id)
        return self.manual_fill(order_id, order.leaves_qty, price)

    def manual_cancel(self, order_id: str, text: str | None = None) -> list:
        """Cancel the order unsolicited, as a venue would."""
        order = self._require_open(order_id)
        actions = [
            Evidence("manual cancel", f"{order.order_id} via control API")
        ]
        actions.extend(
            self._unsolicited_cancel(order, text or "Canceled via control API")
        )
        return actions

    def hold(self, order_id: str) -> list:
        """Drop the schedule but leave the order open for manual control."""
        order = self._require_open(order_id)
        pending = len(order.scheduled)
        actions = [
            Evidence(
                "order held",
                f"{order.order_id} held via control API; {pending} scheduled "
                f"event(s) dropped, order stays open",
            )
        ]
        actions.extend(self._drop_scheduled(order, "order held via control API"))
        self._persist(order)
        return actions

    # --------------------------------------------  lifecycle actions

    def done_for_day(self, order_id: str) -> list:
        """150=3 39=3: the venue ends the order's day; nothing is left open."""
        order = self._require_open(order_id)
        return [Evidence("done for day", f"{order.order_id} via control API")] \
            + self._close_order(order, STATUS_DONE_FOR_DAY,
                                ReportKind.DONE_FOR_DAY,
                                "Done for day via control API",
                                "order done for day via control API")

    def expire(self, order_id: str) -> list:
        """150=C 39=C: expire any open order by hand."""
        order = self._require_open(order_id)
        return [Evidence("manual expiry", f"{order.order_id} via control API")] \
            + self._close_order(order, STATUS_EXPIRED, ReportKind.EXPIRED,
                                "Expired via control API",
                                "order expired via control API")

    def lock(self, order_id: str) -> list:
        """Put an open order in "fill in progress": F/G get 102=0 until unlock
        or its next fill or terminal event."""
        order = self._require_open(order_id)
        if order.locked:
            raise OrderActionError("conflict",
                                   f"Order {order_id} is already locked")
        order.locked = True
        self._persist(order)
        return [Evidence(
            "order locked",
            f"{order.order_id} locked via control API: cancel/replace now "
            f"get Too late to cancel until unlock, a fill or a terminal event")]

    def unlock(self, order_id: str) -> list:
        order = self._require_open(order_id)
        if not order.locked:
            raise OrderActionError("conflict",
                                   f"Order {order_id} is not locked")
        order.locked = False
        self._persist(order)
        return [Evidence("order unlocked",
                         f"{order.order_id} unlocked via control API")]

    # --------------------------------------------- F / G shared lookup

    def _lookup_for_cancel(self, msg, response_to: str):
        """Return (order, reject_actions).  Exactly one is not None/empty."""
        cl_ord_id = msg.get(TAG_CL_ORD_ID)
        orig_cl_ord_id = msg.get(TAG_ORIG_CL_ORD_ID)

        if cl_ord_id in self._used_cl_ord_ids:
            return None, self._cancel_reject_actions(
                None, cl_ord_id, orig_cl_ord_id, STATUS_REJECTED, response_to,
                Reason.DUPLICATE_CL_ORD_ID, "Duplicate ClOrdID",
            )
        self._used_cl_ord_ids.add(cl_ord_id)

        order = self._find_by_cl_ord_id(orig_cl_ord_id)
        if order is None:
            return None, self._cancel_reject_actions(
                None, cl_ord_id, orig_cl_ord_id, STATUS_REJECTED, response_to,
                Reason.CXL_UNKNOWN_ORDER, f"Unknown order {orig_cl_ord_id}",
            )

        symbol = msg.get(TAG_SYMBOL)
        side = msg.get(TAG_SIDE)
        if symbol is not None and symbol != order.symbol:
            return None, self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status, response_to,
                Reason.CXL_BROKER_OPTION,
                f"Symbol {symbol} does not match order symbol {order.symbol}",
            )
        if side is not None and side != order.side:
            return None, self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status, response_to,
                Reason.CXL_BROKER_OPTION,
                f"Side {side} does not match order side {order.side}",
            )

        if order.closed:
            return None, self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status, response_to,
                Reason.CXL_TOO_LATE,
                f"Order is {STATUS_NAMES.get(order.ord_status, order.ord_status)}",
            )
        if order.locked:
            return None, self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status, response_to,
                Reason.CXL_TOO_LATE,
                "Too late to cancel: a fill is in progress",
            )

        reject_code, text = self._business_failure(
            msg, require_order_fields=(response_to == ResponseTo.REPLACE)
        )
        if reject_code is not None:
            return None, self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status, response_to,
                Reason.CXL_BROKER_OPTION, text,
            )
        return order, []

    def _cancel_reject_actions(self, order, cl_ord_id, orig_cl_ord_id,
                               ord_status, response_to, reason: Reason,
                               text) -> list:
        order_id = order.order_id if order is not None else "NONE"
        send = self._cancel_reject(cl_ord_id, orig_cl_ord_id, ord_status,
                                   response_to, reason, text, order_id=order_id)
        code = self.profile.cancel_reject_code_for(reason)
        line = (f"{'CXLREJ':<5} {order_id} 11={cl_ord_id} 41={orig_cl_ord_id} "
                f"102={code} -> {text}")
        events = [Evidence("cancel reject", f"{text} (102={code})",
                           order=order.snapshot() if order is not None else None),
                  Evidence("order lifecycle", line)]
        return [send] + events

    # ------------------------------------------ F: OrderCancelRequest

    def _on_cancel_request(self, msg) -> list:
        order, rejects = self._lookup_for_cancel(msg, ResponseTo.CANCEL)
        if order is None:
            return rejects

        cl_ord_id = msg.get(TAG_CL_ORD_ID)
        orig_cl_ord_id = order.cl_ord_id
        actions: list = []

        if self.config.send_pending_acks:
            order.ord_status = STATUS_PENDING_CANCEL
            previous_cl_ord_id, order.cl_ord_id = order.cl_ord_id, cl_ord_id
            actions.append(
                self._execution_report(
                    order, ReportKind.PENDING_CANCEL,
                    orig_cl_ord_id=orig_cl_ord_id,
                    ord_status=STATUS_PENDING_CANCEL,
                )
            )
            actions.extend(self._order_events(order, self._order_line(order)))
            order.cl_ord_id = previous_cl_ord_id

        order.cl_ord_id = cl_ord_id
        order.cl_ord_id_chain.append(cl_ord_id)
        order.ord_status = STATUS_CANCELED
        order.leaves_qty = 0
        order.closed = True
        actions.append(
            self._execution_report(order, ReportKind.CANCELED,
                                   orig_cl_ord_id=orig_cl_ord_id)
        )
        actions.extend(self._order_events(order, self._order_line(order)))
        actions.extend(self._drop_scheduled(order, "order canceled by request"))
        return actions

    # ----------------------------- G: OrderCancelReplaceRequest

    def _on_replace_request(self, msg, market_price=None) -> list:
        order, rejects = self._lookup_for_cancel(msg, ResponseTo.REPLACE)
        if order is None:
            return rejects

        cl_ord_id = msg.get(TAG_CL_ORD_ID)
        orig_cl_ord_id = order.cl_ord_id
        new_ord_type = msg.get(TAG_ORD_TYPE)
        new_price_raw = msg.get(TAG_PRICE)

        if new_ord_type is not None and new_ord_type != order.ord_type:
            return self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status,
                ResponseTo.REPLACE, Reason.CXL_BROKER_OPTION,
                f"OrdType {new_ord_type} does not match order OrdType "
                f"{order.ord_type}",
            )
        if order.ord_type == ORD_TYPE_MARKET and new_price_raw is not None:
            return self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status,
                ResponseTo.REPLACE, Reason.CXL_BROKER_OPTION,
                "Price must be absent when replacing a market order",
            )

        new_qty_decimal = _as_decimal(msg.get(TAG_ORDER_QTY))
        new_qty = int(new_qty_decimal)
        if new_qty <= order.cum_qty:
            return self._cancel_reject_actions(
                order, cl_ord_id, orig_cl_ord_id, order.ord_status,
                ResponseTo.REPLACE, Reason.CXL_BROKER_OPTION,
                "OrderQty must exceed CumQty",
            )

        actions: list = []

        # The collar applies to a replace that moves a limit price, too.
        if order.ord_type == ORD_TYPE_LIMIT and new_price_raw is not None:
            new_price = _as_decimal(new_price_raw)
            rule = (self.rules.match(order.symbol)
                    if self.rules is not None else None)
            outcome, detail, quote, pct = evaluate_band(
                self.price_band, rule, order.side, new_price, market_price
            )
            actions.extend(self._band_actions(cl_ord_id, order.side,
                                              order.ord_type, new_price,
                                              outcome, detail, quote, pct))
            if outcome == BAND_REJECT:
                # The order is left exactly as it was.  A cancel reject has no
                # code for "no reference", so broker option carries both.
                return actions + self._cancel_reject_actions(
                    order, cl_ord_id, orig_cl_ord_id, order.ord_status,
                    ResponseTo.REPLACE, Reason.CXL_BROKER_OPTION, detail,
                )

        if self.config.send_pending_acks:
            previous_cl_ord_id, order.cl_ord_id = order.cl_ord_id, cl_ord_id
            actions.append(
                self._execution_report(
                    order, ReportKind.PENDING_REPLACE,
                    orig_cl_ord_id=orig_cl_ord_id,
                    ord_status=STATUS_PENDING_REPLACE,
                )
            )
            order.ord_status = STATUS_PENDING_REPLACE
            actions.extend(self._order_events(order, self._order_line(order)))
            order.cl_ord_id = previous_cl_ord_id

        order.order_qty = new_qty
        order.leaves_qty = new_qty - order.cum_qty
        if order.ord_type == ORD_TYPE_LIMIT and new_price_raw is not None:
            order.price = _as_decimal(new_price_raw)
            order.fill_price = order.price
        order.cl_ord_id = cl_ord_id
        order.cl_ord_id_chain.append(cl_ord_id)

        if self.config.replace_ack_ordstatus == "replaced":
            order.ord_status = STATUS_REPLACED
        else:
            order.ord_status = order.working_status

        actions.append(
            self._execution_report(order, ReportKind.REPLACED,
                                   orig_cl_ord_id=orig_cl_ord_id)
        )
        actions.extend(self._order_events(order, self._order_line(order)))
        return actions

    # --------------------------------------------------  persistence

    def _persist(self, order: OrderState) -> None:
        if self.order_store is None:
            return
        if order.closed:
            self.order_store.close_order(order.order_id)
        else:
            self.order_store.upsert(self.serialize(order))

    def serialize(self, order: OrderState) -> dict:
        """Everything needed to bring an open order back after a restart.

        Scheduled events are stored as delays from now, so a reload
        reschedules them relative to the moment it happens.
        """
        now = self.clock.now()
        return {
            "order_id": order.order_id,
            "cl_ord_id": order.cl_ord_id,
            "cl_ord_id_chain": list(order.cl_ord_id_chain),
            "symbol": order.symbol,
            "side": order.side,
            "ord_type": order.ord_type,
            "order_qty": order.order_qty,
            "price": None if order.price is None else str(order.price),
            "account": order.account,
            "cum_qty": order.cum_qty,
            "leaves_qty": order.leaves_qty,
            "notional": str(order.notional),
            "ord_status": order.ord_status,
            "fill_price": (None if order.fill_price is None
                           else str(order.fill_price)),
            "price_source": order.price_source,
            "rule_name": order.rule_name,
            "time_in_force": order.time_in_force,
            "expire_at": _iso(order.expire_at),
            "locked": order.locked,
            "trade_date": order.trade_date,
            "saved_at": _iso(now),
            "scheduled": [
                {"kind": event.kind, "shares": event.shares,
                 "delay_ms": max(0, int((event.due - now).total_seconds()
                                        * 1000))}
                for event in sorted(order.scheduled,
                                    key=lambda e: (e.due, e.sequence))
            ],
        }

    def restore(self, records) -> list:
        """Reload open orders saved by an earlier run .

        Returns evidence describing what came back.  Scheduled events resume
        relative to now; a GTD order keeps its absolute expiry.
        """
        now = self.clock.now()
        actions: list = []
        for record in records or ():
            try:
                order = OrderState(
                    order_id=record["order_id"],
                    cl_ord_id=record["cl_ord_id"],
                    symbol=record["symbol"],
                    side=record["side"],
                    ord_type=record["ord_type"],
                    order_qty=int(record["order_qty"]),
                    price=(None if record.get("price") is None
                           else Decimal(record["price"])),
                    account=record.get("account"),
                    cl_ord_id_chain=list(record.get("cl_ord_id_chain") or
                                         [record["cl_ord_id"]]),
                    cum_qty=int(record.get("cum_qty", 0)),
                    leaves_qty=int(record.get("leaves_qty", 0)),
                    notional=Decimal(record.get("notional", "0")),
                    ord_status=record.get("ord_status", STATUS_NEW),
                    fill_price=(None if record.get("fill_price") is None
                                else Decimal(record["fill_price"])),
                    price_source=record.get("price_source", ""),
                    rule_name=record.get("rule_name", ""),
                    time_in_force=record.get("time_in_force",
                                             TIME_IN_FORCE_DAY),
                    expire_at=_from_iso(record.get("expire_at")),
                    locked=bool(record.get("locked", False)),
                    trade_date=record.get("trade_date", ""),
                    restored=True,
                )
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                actions.append(Evidence(
                    "order not restored",
                    f"{record.get('order_id', '?')}: unreadable record "
                    f"({type(exc).__name__}: {exc})"))
                continue
            for item in record.get("scheduled") or ():
                self._event_seq += 1
                if item.get("kind") == EVENT_EXPIRE and order.expire_at:
                    due = order.expire_at
                else:
                    due = now + timedelta(milliseconds=int(
                        item.get("delay_ms", 0)))
                order.scheduled.append(ScheduledEvent(
                    due=due, kind=item.get("kind", "fill"),
                    shares=int(item.get("shares", 0)),
                    sequence=self._event_seq))
            self._orders[order.order_id] = order
            self._used_cl_ord_ids.update(order.cl_ord_id_chain)
            actions.append(Evidence(
                "order restored",
                f"{order.describe()} {STATUS_NAMES.get(order.ord_status, order.ord_status)} "
                f"cum={order.cum_qty} leaves={order.leaves_qty} "
                f"tif={TIF_LABELS.get(order.time_in_force, order.time_in_force)}"
                f" chain={','.join(order.cl_ord_id_chain)} "
                f"{len(order.scheduled)} event(s) rescheduled"))
        return actions

    def on_session_logon(self) -> list:
        """Run when the session logs on .

        Day orders reloaded from an earlier UTC date expire now, and reloaded
        market orders still waiting for a price ask for it again.
        """
        today = self.clock.now().astimezone(timezone.utc).strftime("%Y%m%d")
        actions: list = []
        for order in list(self._orders.values()):
            if not order.restored or order.closed:
                continue
            if order.time_in_force == TIME_IN_FORCE_DAY and order.trade_date \
                    and order.trade_date < today:
                actions.extend(self._close_order(
                    order, STATUS_EXPIRED, ReportKind.EXPIRED,
                    f"Day order from {order.trade_date} expired after restart",
                    "day order from an earlier date expired on logon"))
                continue
            if order.price_pending:
                actions.append(RequestPrice(order.order_id, order.symbol))
        return actions

    # ------------------------------------- H: OrderStatusRequest 

    def _find_for_status(self, msg):
        order_id = msg.get(TAG_ORDER_ID)
        if order_id and order_id in self._orders:
            return self._orders[order_id]
        cl_ord_id = msg.get(TAG_CL_ORD_ID)
        order = self._find_by_cl_ord_id(cl_ord_id)
        if order is not None:
            return order
        for candidate in self._orders.values():
            if cl_ord_id in candidate.cl_ord_id_chain:
                return candidate
        return None

    def _on_status_request(self, msg) -> list:
        """Answer with an ER that says where the order stands, changing
        nothing: 4.2 20=3 with ExecType = OrdStatus, 4.4 150=I."""
        order = self._find_for_status(msg)
        req_id = msg.get(TAG_ORD_STATUS_REQ_ID)
        if order is None:
            report = ExecReport(
                kind=ReportKind.STATUS, order_id="NONE",
                cl_ord_id=msg.get(TAG_CL_ORD_ID) or "",
                exec_id=self._next_exec_id(),
                symbol=msg.get(TAG_SYMBOL) or "", side=msg.get(TAG_SIDE) or "",
                order_qty="0", ord_type=msg.get(TAG_ORD_TYPE),
                ord_status=STATUS_REJECTED, leaves_qty="0", cum_qty="0",
                avg_px=fmt_avg(ZERO), transact_time=format_time(self.clock.now()),
                text="Unknown order", ord_status_req_id=req_id,
            )
            line = (f"{'STATUS':<5} 11={msg.get(TAG_CL_ORD_ID)} 37="
                    f"{msg.get(TAG_ORDER_ID) or '-'} -> unknown order")
            return [AppSend(MSG_EXECUTION_REPORT,
                            self.profile.render_exec_report(report)),
                    Evidence("order status request", "unknown order"),
                    Evidence("order lifecycle", line)]
        report = ExecReport(
            kind=ReportKind.STATUS, order_id=order.order_id,
            cl_ord_id=order.cl_ord_id, exec_id=self._next_exec_id(),
            account=order.account or None, symbol=order.symbol,
            side=order.side, order_qty=str(order.order_qty),
            ord_type=order.ord_type, ord_status=order.ord_status,
            price=fmt_price(order.price) if order.price is not None else None,
            leaves_qty=str(order.leaves_qty), cum_qty=str(order.cum_qty),
            avg_px=fmt_avg(order.avg_px),
            transact_time=format_time(self.clock.now()),
            ord_status_req_id=req_id,
        )
        status = STATUS_NAMES.get(order.ord_status, order.ord_status)
        line = (f"{'STATUS':<5} {order.order_id} 11={order.cl_ord_id} -> "
                f"{status} cum={order.cum_qty} leaves={order.leaves_qty}")
        return [AppSend(MSG_EXECUTION_REPORT,
                        self.profile.render_exec_report(report)),
                Evidence("order status request",
                         f"{order.order_id} {status}", order=order.snapshot()),
                Evidence("order lifecycle", line)]

