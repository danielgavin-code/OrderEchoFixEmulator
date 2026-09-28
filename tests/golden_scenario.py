"""A deterministic scripted scenario through the pure order book.

This exists to protect the Cook 5 refactor: its output is captured from the
Cook 4 code as golden fixtures, and the FIX 4.2 profile must reproduce them
byte for byte afterwards.  It therefore touches only the order book's public
API and renders exactly what goes on the wire.

Evidence actions are deliberately *not* captured: they are narrative, and Cook
5 §3.2 reorders some of them on purpose.  What must not move is the FIX.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from orderecho_Clock import FakeClock
from orderecho_Codec import Codec
from orderecho_FixVersion import FIX_4_2, profile_for
from orderecho_Config import OrdersConfig, PriceBandConfig
from orderecho_OrderBook import OrderBook
from orderecho_Pricing import PriceQuote
from orderecho_Rules import load_rules
from orderecho_Session import AppSend, RequestPrice, SessionReject

START = datetime(2026, 1, 2, 9, 30, 0, tzinfo=timezone.utc)
RUN_ID = "GOLDEN"
DELAY_MS = 1000
DELAY_SEC = DELAY_MS / 1000.0

REF = PriceQuote(Decimal("227.50"), "static:config")

#: ref 227.50, 10% band -> 204.75 .. 250.25
IN_BAND = "230.00"
OUT_OF_BAND = "500.00"

RULES = [
    {"name": "nasdaq-test-reject", "match": {"symbol": "ZVZZT"},
     "behavior": "reject", "reject_code": 1, "text": "Unknown symbol"},
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "full", "match": {"first_letter": "A-D"}, "behavior": "full_fill",
     "delay_ms": DELAY_MS},
    {"name": "partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "fill_rest",
     "delay_ms": DELAY_MS},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]

TRANSACT_TIME_TAG = 60
NORMALIZED = "<TRANSACT_TIME>"


# ------------------------------------------------------------------ helpers


class Scenario:
    """Drives an order book and records everything it puts on the wire."""

    def __init__(self, band_enabled=True, version=FIX_4_2, **orders_kwargs):
        self.clock = FakeClock(START)
        self.version = version
        self.profile = profile_for(version)
        band = PriceBandConfig(enabled=band_enabled, pct=10.0,
                               mode="aggressive")
        self.book = OrderBook(
            OrdersConfig(**orders_kwargs), load_rules(RULES, DELAY_MS),
            self.clock, RUN_ID, price_band=band, profile=self.profile,
        )
        self.codec = Codec(version)
        self.seq = 1
        self.lines: list[str] = []

    # -- building inbound messages -----------------------------------------

    def _decode(self, msg_type, fields):
        self.seq += 1
        raw = self.codec.encode(
            msg_type, fields, sender_comp_id="AGENT",
            target_comp_id="ORDERECHO", seq_num=self.seq, sending_time=START,
        )
        return self.codec.decode(raw)[0]

    def new_order(self, cl_ord_id, symbol="AAPL", side="1", qty="1000",
                  ord_type="1", price=None, handl_inst="1", extra=(),
                  omit=(), transact_time="20260102-09:30:00.000"):
        fields = [(11, cl_ord_id), (21, handl_inst), (55, symbol), (54, side),
                  (60, transact_time), (38, qty), (40, ord_type)]
        if price is not None:
            fields.append((44, price))
        fields.extend(extra)
        return self._decode("D", [(t, v) for t, v in fields if t not in omit])

    def cancel(self, cl_ord_id, orig, symbol="AAPL", side="1", omit=()):
        fields = [(11, cl_ord_id), (41, orig), (55, symbol), (54, side),
                  (60, "20260102-09:30:00.000")]
        return self._decode("F", [(t, v) for t, v in fields if t not in omit])

    def replace(self, cl_ord_id, orig, symbol="AAPL", side="1", qty="2000",
                ord_type="2", price=IN_BAND, omit=()):
        fields = [(11, cl_ord_id), (41, orig), (21, "1"), (55, symbol),
                  (54, side), (60, "20260102-09:30:00.000"), (38, qty),
                  (40, ord_type)]
        if price is not None:
            fields.append((44, price))
        return self._decode("G", [(t, v) for t, v in fields if t not in omit])

    # -- recording ---------------------------------------------------------

    def note(self, text: str) -> None:
        self.lines.append(f"# {text}")

    def record(self, actions) -> None:
        for action in actions or ():
            if isinstance(action, AppSend):
                body = "|".join(
                    f"{int(tag)}="
                    f"{NORMALIZED if int(tag) == TRANSACT_TIME_TAG else value}"
                    for tag, value in action.body_fields
                )
                self.lines.append(f"SEND {action.msg_type} {body}")
            elif isinstance(action, SessionReject):
                self.lines.append(
                    f"REJECT ref_seq={action.ref_seq} "
                    f"ref_tag={action.ref_tag} "
                    f"reason={action.reason_code} text={action.text}"
                )
            elif isinstance(action, RequestPrice):
                self.lines.append(
                    f"PRICE_REQUEST {action.order_id} {action.symbol}"
                )

    # -- driving -----------------------------------------------------------

    def send(self, msg, quote=REF) -> None:
        self.record(self.book.on_app_message(msg, quote))

    def tick(self, seconds=DELAY_SEC) -> None:
        self.clock.advance(seconds)
        self.record(self.book.on_timer())

    def price(self, order_id, quote=REF) -> None:
        self.record(self.book.on_price(order_id, quote))

    def manual(self, call) -> None:
        try:
            self.record(call())
        except Exception as exc:
            self.lines.append(f"ERROR {type(exc).__name__} {exc}")

    def first_order_id(self, n=1) -> str:
        return f"O-{RUN_ID}-{n}"

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------- scenarios


def _scenario_acks(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("limit order: priced at the ack")
    s.send(s.new_order("A1", symbol="EFG", ord_type="2", price="10.25"))
    s.note("market order: acked pending, priced by on_price, then filled")
    s.send(s.new_order("A2", symbol="AAPL"))
    s.price(s.first_order_id(2))
    s.tick()
    s.note("ack_only symbol: nothing is scheduled")
    s.send(s.new_order("A3", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.tick(DELAY_SEC * 3)
    s.note("order carrying an Account")
    s.send(s.new_order("A4", symbol="ZWZZT", ord_type="2", price=IN_BAND,
                       extra=[(1, "ACCT-7")]))
    return s.text()


def _scenario_partial_fills(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("40% and 10% partials, then fill_rest")
    s.send(s.new_order("P1", symbol="EFG", ord_type="2", price="10.00"))
    s.tick()
    s.tick()
    s.tick()
    return s.text()


def _scenario_cancel(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("cancel request against a working order")
    s.send(s.new_order("C1", symbol="EFG", ord_type="2", price="10.00"))
    s.send(s.cancel("C2", "C1", symbol="EFG"))
    s.note("unsolicited cancel by rule")
    s2 = Scenario(version=version, )
    s2.book.rules = load_rules(
        [{"name": "cancel-after", "match": {"any": True},
          "behavior": "cancel_after_ack", "delay_ms": DELAY_MS}], DELAY_MS
    )
    s2.send(s2.new_order("C3", symbol="HJK", ord_type="2", price=IN_BAND))
    s2.tick()
    s.lines.extend(s2.lines)
    s.note("manual cancel through the control API path")
    s.send(s.new_order("C4", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.manual(lambda: s.book.manual_cancel(s.first_order_id(3), "by hand"))
    return s.text()


def _scenario_replace(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("replace raising quantity")
    s.send(s.new_order("R1", symbol="EFG", ord_type="2", price="10.00"))
    s.send(s.replace("R2", "R1", symbol="EFG", qty="2000", price="10.00"))
    s.note("replace changing the limit price, after a partial")
    s.tick()
    s.send(s.replace("R3", "R2", symbol="EFG", qty="2000", price=IN_BAND))
    s.note("replace_ack_ordstatus: replaced")
    s2 = Scenario(version=version, replace_ack_ordstatus="replaced")
    s2.send(s2.new_order("R4", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s2.send(s2.replace("R5", "R4", symbol="ZWZZT", qty="2000", price=IN_BAND))
    s.lines.extend(s2.lines)
    return s.text()


def _scenario_pending_acks(version=FIX_4_2) -> str:
    s = Scenario(version=version, send_pending_acks=True)
    s.note("pending cancel then cancel")
    s.send(s.new_order("N1", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.send(s.cancel("N2", "N1", symbol="ZWZZT"))
    s.note("pending replace then replace")
    s.send(s.new_order("N3", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.send(s.replace("N4", "N3", symbol="ZWZZT", qty="2000", price=IN_BAND))
    return s.text()


def _scenario_rejects_business(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("quantity zero")
    s.send(s.new_order("B1", qty="0"))
    s.note("quantity negative")
    s.send(s.new_order("B2", qty="-5"))
    s.note("quantity fractional")
    s.send(s.new_order("B3", qty="10.5"))
    s.note("side valid in FIX but unsupported")
    s.send(s.new_order("B4", side="3"))
    s.note("ordtype valid in FIX but unsupported")
    s.send(s.new_order("B5", ord_type="3"))
    s.note("price not positive")
    s.send(s.new_order("B6", ord_type="2", price="0"))
    s.note("TimeInForce unsupported")
    s.send(s.new_order("B7", extra=[(59, "1")]))
    s.note("duplicate ClOrdID")
    s.send(s.new_order("B8", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.send(s.new_order("B8", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    return s.text()


def _scenario_rejects_rule(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("rule reject with its own code and text")
    s.send(s.new_order("U1", symbol="ZVZZT", ord_type="2", price=IN_BAND))
    s.note("and as a market order")
    s.send(s.new_order("U2", symbol="ZVZZT"))
    return s.text()


def _scenario_rejects_band(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("buy limit above the band")
    s.send(s.new_order("D1", symbol="ZWZZT", ord_type="2",
                       price=OUT_OF_BAND))
    s.note("sell limit below the band")
    s.send(s.new_order("D2", symbol="ZWZZT", side="2", ord_type="2",
                       price="100.00"))
    s.note("exactly at the edge is accepted")
    s.send(s.new_order("D3", symbol="ZWZZT", ord_type="2", price="250.25"))
    s.note("no reference at all: skipped")
    s.send(s.new_order("D4", symbol="ZWZZT", ord_type="2",
                       price=OUT_OF_BAND), quote=None)
    s.note("band disabled entirely")
    s2 = Scenario(version=version, band_enabled=False)
    s2.send(s2.new_order("D5", symbol="ZWZZT", ord_type="2",
                         price=OUT_OF_BAND))
    s.lines.extend(s2.lines)
    return s.text()


def _scenario_cancel_rejects(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    s.note("cancel an unknown order")
    s.send(s.cancel("K1", "NOSUCH"))
    s.note("working order for the rest of these")
    s.send(s.new_order("K2", symbol="ZWZZT", ord_type="2", price=IN_BAND))
    s.note("duplicate ClOrdID on the cancel")
    s.send(s.cancel("K2", "K2", symbol="ZWZZT"))
    s.note("symbol mismatch")
    s.send(s.cancel("K3", "K2", symbol="WRONG"))
    s.note("side mismatch")
    s.send(s.cancel("K4", "K2", symbol="ZWZZT", side="2"))
    s.note("replace below CumQty")
    s.send(s.replace("K5", "K2", symbol="ZWZZT", qty="0", price=IN_BAND))
    s.note("replace changing side")
    s.send(s.replace("K6", "K2", symbol="ZWZZT", side="2", qty="2000",
                     price=IN_BAND))
    s.note("replace moving the price out of band")
    s.send(s.replace("K7", "K2", symbol="ZWZZT", qty="2000",
                     price=OUT_OF_BAND))
    s.note("cancel a closed order")
    s.send(s.cancel("K8", "K2", symbol="ZWZZT"))
    s.send(s.cancel("K9", "K8", symbol="ZWZZT"))
    return s.text()


def _scenario_session_rejects(version=FIX_4_2) -> str:
    s = Scenario(version=version, )
    for tag in (11, 21, 55, 54, 60, 38, 40):
        s.note(f"missing required tag {tag}")
        s.send(s.new_order(f"S{tag}", omit=(tag,)))
    s.note("limit order without a price")
    s.send(s.new_order("S44", ord_type="2", price=None))
    s.note("quantity not a number")
    s.send(s.new_order("SQ", qty="many"))
    s.note("price not a number")
    s.send(s.new_order("SP", ord_type="2", price="cheap"))
    s.note("TransactTime unparseable")
    s.send(s.new_order("ST", transact_time="yesterday"))
    s.note("side not a FIX value")
    s.send(s.new_order("SS", side="X"))
    s.note("ordtype not a FIX value")
    s.send(s.new_order("SO", ord_type="Z"))
    s.note("handlinst not a FIX value")
    s.send(s.new_order("SH", handl_inst="9"))
    s.note("cancel missing its OrigClOrdID")
    s.send(s.cancel("SC", "X", omit=(41,)))
    return s.text()


SCENARIOS = {
    "acks": _scenario_acks,
    "partial_fills": _scenario_partial_fills,
    "cancel": _scenario_cancel,
    "replace": _scenario_replace,
    "pending_acks": _scenario_pending_acks,
    "rejects_business": _scenario_rejects_business,
    "rejects_rule": _scenario_rejects_rule,
    "rejects_band": _scenario_rejects_band,
    "cancel_rejects": _scenario_cancel_rejects,
    "session_rejects": _scenario_session_rejects,
}


def run_all(version: str = FIX_4_2) -> dict:
    """Every scenario, rendered for one FIX version."""
    return {name: build(version) for name, build in SCENARIOS.items()}
