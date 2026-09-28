"""Price band (fat-finger collar).  Static pricing; no network."""

import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import FakeClock, SystemClock
from orderecho_Codec import Codec
from orderecho_Config import (
    Config,
    ControlApiConfig,
    LoggingConfig,
    OrdersConfig,
    PriceBandConfig,
    PricingConfig,
    SessionConfig,
    StorageConfig,
)
from orderecho_OrderBook import (
    BAND_PASS,
    BAND_REJECT,
    BAND_SKIPPED,
    OrderBook,
    evaluate_band,
)
from orderecho_Pricing import (
    SOURCE_STATIC_CONFIG,
    SOURCE_STATIC_FALLBACK,
    PriceQuote,
)
from orderecho_Rules import load_rules
from orderecho_Session import AppSend, Evidence
from orderecho_Transport import Transport

START = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "20260927-120000"
REF = PriceQuote(Decimal("227.50"), SOURCE_STATIC_CONFIG)
FALLBACK_REF = PriceQuote(Decimal("227.50"), SOURCE_STATIC_FALLBACK)

# ref 227.50 with a 10% band: 204.75 .. 250.25
EDGE_HIGH = "250.25"
OVER_HIGH = "250.26"
EDGE_LOW = "204.75"
UNDER_LOW = "204.74"

RULES = [{"name": "default", "match": {"any": True}, "behavior": "ack_only"}]


def make_book(rules_raw=None, **band_kwargs):
    band = PriceBandConfig(enabled=True, pct=10.0, mode="aggressive",
                           **band_kwargs)
    clock = FakeClock(START)
    rules = load_rules(rules_raw or RULES, 50)
    return OrderBook(OrdersConfig(), rules, clock, RUN_ID, price_band=band), band


_codec = Codec("FIX.4.2")
_seq = [1]


def order(cl_ord_id="B1", symbol="AAPL", side="1", qty="100",
          ord_type="2", price="200.00"):
    _seq[0] += 1
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, side),
              (60, "20260927-12:00:00.000"), (38, qty), (40, ord_type)]
    if price is not None:
        fields.append((44, price))
    raw = _codec.encode("D", fields, sender_comp_id="AGENT",
                        target_comp_id="ORDERECHO", seq_num=_seq[0],
                        sending_time=START)
    return _codec.decode(raw)[0]


def replace(cl_ord_id, orig, symbol="AAPL", side="1", qty="200",
            price="200.00"):
    _seq[0] += 1
    fields = [(11, cl_ord_id), (41, orig), (21, "1"), (55, symbol), (54, side),
              (60, "20260927-12:00:00.000"), (38, qty), (40, "2")]
    if price is not None:
        fields.append((44, price))
    raw = _codec.encode("G", fields, sender_comp_id="AGENT",
                        target_comp_id="ORDERECHO", seq_num=_seq[0],
                        sending_time=START)
    return _codec.decode(raw)[0]


def reports(actions, msg_type="8"):
    return [a for a in actions
            if isinstance(a, AppSend) and a.msg_type == msg_type]


def events(actions):
    return [a for a in actions if isinstance(a, Evidence)]


def field(send, tag):
    for pair_tag, value in send.body_fields:
        if int(pair_tag) == tag:
            return value
    return None


def band_event(actions):
    for event in events(actions):
        if event.event.startswith("price band "):
            return event
    return None


def band_line(actions):
    # A skipped band emits the same line under a warning-level event name.
    for event in events(actions):
        if event.event in ("price band", "price band warning"):
            return event.detail
    return None


# 7 -------------------------------------------------------------------------

def test_buy_limit_inside_the_band_is_accepted():
    book, _band = make_book()
    actions = book.on_app_message(order(price="230.00"), REF)
    assert field(reports(actions)[0], 150) == "0"
    assert band_event(actions).event == f"price band {BAND_PASS}"
    assert "-> PASS" in band_line(actions)


def test_buy_limit_exactly_at_the_edge_is_accepted():
    book, _band = make_book()
    actions = book.on_app_message(order(price=EDGE_HIGH), REF)
    assert field(reports(actions)[0], 150) == "0"
    assert band_event(actions).event == f"price band {BAND_PASS}"


def test_buy_limit_above_the_band_is_rejected():
    book, _band = make_book()
    actions = book.on_app_message(order(price=OVER_HIGH), REF)
    out = reports(actions)
    assert len(out) == 1
    assert field(out[0], 150) == "8"
    assert field(out[0], 39) == "8"
    assert field(out[0], 103) == "3"
    assert field(out[0], 58) == (
        "Limit 250.26 outside 10% band of ref 227.50 (static:config)"
    )
    assert field(out[0], 151) == "0"

    line = band_line(actions)
    assert line.startswith("BAND  B1 BUY LMT 250.26 ref=227.50 "
                           "(static:config) band=±10%")
    assert line.endswith("-> REJECT")

    order_state = book.get_order(field(out[0], 37))
    assert order_state.closed
    assert order_state.ord_status == "8"


def test_a_rejected_order_is_never_scheduled():
    book, clock = make_book()[0], None
    book_clock = book.clock
    actions = book.on_app_message(order(price=OVER_HIGH), REF)
    assert not book.has_pending_events()
    book_clock.advance(10)
    assert book.on_timer() == []
    assert actions is not None


# 8 -------------------------------------------------------------------------

def test_sell_limit_below_the_band_is_rejected():
    book, _band = make_book()
    actions = book.on_app_message(order(side="2", price=UNDER_LOW), REF)
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 103) == "3"
    assert "204.74" in field(out[0], 58)


def test_sell_limit_exactly_at_the_low_edge_is_accepted():
    book, _band = make_book()
    actions = book.on_app_message(order(side="2", price=EDGE_LOW), REF)
    assert field(reports(actions)[0], 150) == "0"


@pytest.mark.parametrize("side", ["2", "5", "6"])
def test_selling_far_above_the_market_is_fine_when_aggressive_only(side):
    """Offering to sell high is not a fat finger; it just will not trade."""
    book, _band = make_book()
    actions = book.on_app_message(order(side=side, price="10000.00"), REF)
    assert field(reports(actions)[0], 150) == "0"


def test_selling_far_above_the_market_is_rejected_in_both_mode():
    book, band = make_book()
    band.mode = "both"
    actions = book.on_app_message(order(side="2", price="10000.00"), REF)
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 103) == "3"


def test_buying_far_below_the_market_is_fine_when_aggressive_only():
    book, _band = make_book()
    actions = book.on_app_message(order(price="0.01"), REF)
    assert field(reports(actions)[0], 150) == "0"


def test_buying_far_below_the_market_is_rejected_in_both_mode():
    book, band = make_book()
    band.mode = "both"
    actions = book.on_app_message(order(price="0.01"), REF)
    assert field(reports(actions)[0], 150) == "8"


# 9 -------------------------------------------------------------------------

def test_market_orders_are_never_checked():
    book, _band = make_book()
    actions = book.on_app_message(order(ord_type="1", price=None), REF)
    assert field(reports(actions)[0], 150) == "0"
    assert band_event(actions) is None
    assert band_line(actions) is None


# 10 ------------------------------------------------------------------------

def test_replace_moving_the_price_outside_the_band_is_rejected():
    book, _band = make_book()
    book.on_app_message(order(cl_ord_id="R1", price="230.00"), REF)
    before = book.get_order(f"O-{RUN_ID}-1")
    assert before.price == Decimal("230.00")

    actions = book.on_app_message(
        replace("R2", "R1", price=OVER_HIGH), REF
    )
    out = reports(actions, "9")
    assert len(out) == 1
    assert field(out[0], 434) == "2"
    assert field(out[0], 102) == "2"
    assert field(out[0], 58) == (
        "Limit 250.26 outside 10% band of ref 227.50 (static:config)"
    )

    # The order is exactly as it was.
    assert before.price == Decimal("230.00")
    assert before.order_qty == 100
    assert before.cl_ord_id == "R1"
    assert not before.closed


def test_replace_inside_the_band_is_accepted():
    book, _band = make_book()
    book.on_app_message(order(cl_ord_id="R3", price="230.00"), REF)
    actions = book.on_app_message(replace("R4", "R3", price="240.00"), REF)
    out = reports(actions)
    assert field(out[0], 150) == "5"
    assert field(out[0], 44) == "240.00"
    assert book.get_order(f"O-{RUN_ID}-1").cl_ord_id == "R4"


# 11 ------------------------------------------------------------------------

def test_fallback_reference_is_skipped_by_default():
    book, _band = make_book()
    actions = book.on_app_message(order(price="9999.00"), FALLBACK_REF)
    assert field(reports(actions)[0], 150) == "0"
    event = band_event(actions)
    assert event.event == f"price band {BAND_SKIPPED}"
    assert "fallback" in event.detail
    assert "-> SKIPPED" in band_line(actions)


def test_fallback_reference_is_enforced_when_configured():
    book, _band = make_book(enforce_on_fallback=True)
    actions = book.on_app_message(order(price="9999.00"), FALLBACK_REF)
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 103) == "3"
    assert "static:fallback" in field(out[0], 58)


def test_no_reference_at_all_is_skipped():
    book, _band = make_book()
    actions = book.on_app_message(order(price="9999.00"), None)
    assert field(reports(actions)[0], 150) == "0"
    assert "no reference price available" in band_event(actions).detail


# 13 ------------------------------------------------------------------------

def test_rule_override_tightens_the_band():
    rules = [{"name": "tight", "match": {"symbol": "AAPL"},
              "behavior": "ack_only", "price_band_pct": 2},
             {"name": "default", "match": {"any": True},
              "behavior": "ack_only"}]
    book, _band = make_book(rules)

    # 2% of 227.50 is 4.55, so the edge is 232.05.
    assert field(reports(book.on_app_message(
        order(cl_ord_id="T1", price="232.05"), REF))[0], 150) == "0"

    actions = book.on_app_message(order(cl_ord_id="T2", price="232.06"), REF)
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 58) == (
        "Limit 232.06 outside 2% band of ref 227.50 (static:config)"
    )

    # A symbol on the default rule still gets the global 10%.
    assert field(reports(book.on_app_message(
        order(cl_ord_id="T3", symbol="ZZZZ", price="240.00"), REF))[0], 150) == "0"


def test_rule_override_off_disables_the_band():
    rules = [{"name": "wild", "match": {"symbol": "AAPL"},
              "behavior": "ack_only", "price_band_pct": "off"},
             {"name": "default", "match": {"any": True},
              "behavior": "ack_only"}]
    book, _band = make_book(rules)
    actions = book.on_app_message(order(price="99999.00"), REF)
    assert field(reports(actions)[0], 150) == "0"
    assert "price_band_pct: off" in band_event(actions).detail


def test_disabling_the_band_globally_skips_everything():
    book, band = make_book()
    band.enabled = False
    actions = book.on_app_message(order(price="99999.00"), REF)
    assert field(reports(actions)[0], 150) == "0"
    assert band_event(actions).detail == "price band disabled"


def test_no_band_config_at_all_means_no_check():
    clock = FakeClock(START)
    book = OrderBook(OrdersConfig(), load_rules(RULES, 50), clock, RUN_ID)
    actions = book.on_app_message(order(price="99999.00"), REF)
    assert field(reports(actions)[0], 150) == "0"


# evaluate_band directly ----------------------------------------------------

@pytest.mark.parametrize("side,price,expected", [
    ("1", "250.25", BAND_PASS),
    ("1", "250.26", BAND_REJECT),
    ("2", "204.75", BAND_PASS),
    ("2", "204.74", BAND_REJECT),
    ("5", "204.74", BAND_REJECT),
    ("6", "204.74", BAND_REJECT),
])
def test_evaluate_band_edges(side, price, expected):
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive")
    outcome, _detail, _quote, pct = evaluate_band(
        band, None, side, Decimal(price), REF
    )
    assert outcome == expected
    assert pct == 10


def test_evaluate_band_zero_pct_only_allows_the_reference():
    band = PriceBandConfig(enabled=True, pct=0, mode="both")
    assert evaluate_band(band, None, "1", Decimal("227.50"), REF)[0] == BAND_PASS
    assert evaluate_band(band, None, "1", Decimal("227.51"), REF)[0] == BAND_REJECT


# --- over the wire (12, 14) ------------------------------------------------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, port, band: PriceBandConfig, rules=None) -> Config:
    return Config(
        session=SessionConfig(
            fix_version="FIX.4.2", sender_comp_id="ORDERECHO",
            target_comp_id="AGENT", host="127.0.0.1", port=port,
            heartbeat_grace_pct=20, logout_timeout_sec=10,
        ),
        storage=StorageConfig(
            seqnum_dir=str(tmp_path / "data" / "seqnums"),
            evidence_dir=str(tmp_path / "data" / "evidence"),
            msgstore_dir=str(tmp_path / "data" / "msgstore"),
        ),
        logging=LoggingConfig(
            log_dir=str(tmp_path / "logs"), fix_delimiter="|",
            engine_level="DEBUG", console=False,
        ),
        orders=OrdersConfig(default_delay_ms=50),
        pricing=PricingConfig(mode="static",
                              static={"AAPL": Decimal("227.50")},
                              static_default=Decimal("100.00")),
        control_api=ControlApiConfig(enabled=False),
        price_band=band,
        rules=load_rules(rules or [{"name": "default", "match": {"any": True},
                                    "behavior": "ack_only"}], 50),
        path="<test>",
    )


async def wire_up(tmp_path, band):
    transport = Transport(make_config(tmp_path, free_port(), band),
                          clock=SystemClock())
    await transport.start()
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    await client.logon(30)
    assert (await client.recv()).msg_type == "A"
    return transport, client


async def send_limit(client, cl_ord_id, price, symbol="AAPL", side="1"):
    await client.send("D", [(11, cl_ord_id), (21, "1"), (55, symbol),
                            (54, side), (38, "100"), (40, "2"), (44, price),
                            (60, "20260927-12:00:00.000")])


# 14 ------------------------------------------------------------------------

async def test_band_reject_over_the_wire(tmp_path):
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive")
    transport, client = await wire_up(tmp_path, band)
    try:
        await send_limit(client, "W1", "500.00")
        report = await client.recv_until("8")
        assert report.get(150) == "8"
        assert report.get(39) == "8"
        assert report.get(103) == "3"
        assert report.get(58) == (
            "Limit 500.00 outside 10% band of ref 227.50 (static:config)"
        )

        await send_limit(client, "W2", "230.00")
        accepted = await client.recv_until("8")
        assert accepted.get(150) == "0"

        with open(transport.engine_log.path, encoding="utf-8") as handle:
            engine = handle.read()
        assert "BAND  W1 BUY LMT 500.00 ref=227.50 (static:config)" in engine
        assert "-> REJECT" in engine
        assert "BAND  W2 BUY LMT 230.00" in engine
        assert "-> PASS" in engine
    finally:
        await client.close()
        await transport.stop()
        transport.close_logs()


# 12 ------------------------------------------------------------------------

async def test_slow_reference_lookup_is_skipped_and_the_order_is_acked(
        tmp_path, monkeypatch):
    """A reference we cannot get in time must not hold up the ack."""
    import orderecho_Pricing

    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive",
                           lookup_timeout_ms=50)
    transport, client = await wire_up(tmp_path, band)

    async def slow_resolve(source, symbol, timeout_sec):
        import asyncio
        await asyncio.sleep(timeout_sec + 0.2)
        return None

    monkeypatch.setattr(orderecho_Pricing, "resolve_quote", slow_resolve)
    monkeypatch.setattr("orderecho_Transport.resolve_quote", slow_resolve)

    try:
        await send_limit(client, "S1", "99999.00")
        report = await client.recv_until("8", timeout=5.0)
        assert report.get(150) == "0"          # acked, not rejected

        with open(transport.engine_log.path, encoding="utf-8") as handle:
            engine = handle.read()
        assert "-> SKIPPED (no reference price available)" in engine
    finally:
        await client.close()
        await transport.stop()
        transport.close_logs()
