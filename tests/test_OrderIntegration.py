"""Order flow end to end over real sockets.  Static pricing: no network."""

import asyncio
import json
import socket
from decimal import Decimal

import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import SystemClock
from orderecho_Codec import FixMsg
from orderecho_Config import (
    Config,
    LoggingConfig,
    OrdersConfig,
    PricingConfig,
    SessionConfig,
    StorageConfig,
)
from orderecho_FixVersion import FIX_4_2, FIX_4_4
from orderecho_Rules import load_rules
from orderecho_Transport import Transport

HEARTBEAT = 30
DELAY_MS = 50

RULES = [
    {"name": "nasdaq-test-reject", "match": {"symbol": "ZVZZT"},
     "behavior": "reject", "reject_code": 1, "text": "Unknown symbol"},
    {"name": "a-to-d-full", "match": {"first_letter": "A-D"},
     "behavior": "full_fill"},
    {"name": "e-to-g-partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave"},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, port, fix_version=FIX_4_2) -> Config:
    return Config(
        session=SessionConfig(
            fix_version=fix_version, sender_comp_id="ORDERECHO",
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
        orders=OrdersConfig(default_delay_ms=DELAY_MS),
        pricing=PricingConfig(mode="static", static={"AAPL": Decimal("227.50")},
                              static_default=Decimal("100.00")),
        rules=load_rules(RULES, DELAY_MS),
        path="<test>",
    )


# Spec 9.7: the whole order flow runs on both versions.
@pytest.fixture(params=[FIX_4_2, FIX_4_4], ids=["fix42", "fix44"])
async def acceptor(request, tmp_path):
    transport = Transport(make_config(tmp_path, free_port(), request.param),
                          clock=SystemClock())
    await transport.start()
    try:
        yield transport
    finally:
        await transport.stop()
        transport.close_logs()


def version_of(transport) -> str:
    return transport.config.session.fix_version


def fill_type(transport) -> str:
    """ExecType for a full fill: 4.4 folds both fills into Trade."""
    return "2" if version_of(transport) == FIX_4_2 else "F"


def partial_type(transport) -> str:
    return "1" if version_of(transport) == FIX_4_2 else "F"


async def logged_on(transport) -> FixTestClient:
    client = FixTestClient(fix_version=version_of(transport))
    await client.connect("127.0.0.1", transport.port)
    await client.logon(HEARTBEAT)
    reply = await client.recv()
    assert reply.msg_type == "A"
    return client


async def new_order(client, cl_ord_id, symbol, qty, ord_type="1", price=None,
                    side="1"):
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, side),
              (38, str(qty)), (40, ord_type),
              (60, "20260926-12:00:00.000")]
    if price is not None:
        fields.append((44, price))
    await client.send("D", fields)


async def collect(client, count, timeout=5.0, msg_type="8"):
    """Read until `count` messages of msg_type have arrived."""
    out = []
    while len(out) < count:
        msg = await client.recv(timeout)
        if msg is None:
            break
        if isinstance(msg, FixMsg) and msg.msg_type == msg_type:
            out.append(msg)
    return out


def read_evidence(transport):
    with open(transport.evidence.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_fix_log(transport):
    with open(transport.fix_log.path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


def read_engine_log(transport):
    with open(transport.engine_log.path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------

async def test_market_order_acked_then_fully_filled(acceptor):
    transport = acceptor
    client = await logged_on(transport)

    await new_order(client, "I1", "AAPL", 1000)
    reports = await collect(client, 2)
    ack, fill = reports

    assert ack.get(150) == "0" and ack.get(39) == "0"
    assert ack.get(37).startswith(f"O-{transport.run_id}-")
    assert ack.get(17).startswith(f"E-{transport.run_id}-")
    assert ack.get(11) == "I1"
    assert ack.get(55) == "AAPL" and ack.get(54) == "1"
    assert ack.get(38) == "1000" and ack.get(40) == "1"
    assert ack.get(151) == "1000" and ack.get(14) == "0"
    assert ack.get(32) == "0" and ack.get(31) == "0.00"
    assert ack.get(6) == "0.0000"

    assert fill.get(150) == fill_type(transport) and fill.get(39) == "2"
    assert fill.get(32) == "1000"
    assert fill.get(31) == "227.50"           # the static price for AAPL
    assert fill.get(14) == "1000" and fill.get(151) == "0"
    assert fill.get(6) == "227.5000"
    assert fill.get(37) == ack.get(37)        # same OrderID throughout

    await client.send("5", [(58, "done")])
    logout = await client.recv_until("5")
    assert logout is not None
    assert await client.wait_closed(timeout=5.0)
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    engine = read_engine_log(transport)
    assert "rule matched: AAPL matched rule 'a-to-d-full'" in engine
    assert "price resolved: AAPL 227.50 (static:config)" in engine
    assert "-> NEW" in engine and "-> FILLED" in engine


async def test_limit_order_partials_then_cancel_stops_further_fills(acceptor):
    transport = acceptor
    client = await logged_on(transport)

    await new_order(client, "I2", "EFG", 1000, ord_type="2", price="10.25")
    ack, first, second = await collect(client, 3)

    assert ack.get(150) == "0"
    assert ack.get(44) == "10.25"
    partial = partial_type(transport)
    assert (first.get(150), first.get(32), first.get(14)) == (partial, "400", "400")
    assert (second.get(150), second.get(32), second.get(14)) == (partial, "100", "500")
    assert first.get(39) == "1" and second.get(39) == "1"
    assert second.get(151) == "500"
    assert second.get(6) == "10.2500"

    await client.send("F", [(11, "I2-C"), (41, "I2"), (55, "EFG"), (54, "1"),
                            (60, "20260926-12:00:00.000")])
    canceled = (await collect(client, 1))[0]
    assert canceled.get(150) == "4" and canceled.get(39) == "4"
    assert canceled.get(11) == "I2-C" and canceled.get(41) == "I2"
    assert canceled.get(151) == "0"
    assert canceled.get(14) == "500"

    # Nothing further arrives: the remaining schedule was dropped.
    await asyncio.sleep(DELAY_MS / 1000.0 * 6)
    assert await client.recv(timeout=0.3) is None

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_rule_rejected_symbol_gets_one_reject_report(acceptor):
    transport = acceptor
    client = await logged_on(transport)

    await new_order(client, "I3", "ZVZZT", 500)
    report = (await collect(client, 1))[0]
    assert report.get(150) == "8" and report.get(39) == "8"
    assert report.get(103) == "1"
    assert report.get(58) == "Unknown symbol"
    assert report.get(151) == "0"

    await asyncio.sleep(DELAY_MS / 1000.0 * 4)
    assert await client.recv(timeout=0.3) is None      # no ack, no fills

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_validation_failure_produces_a_session_reject(acceptor):
    transport = acceptor
    client = await logged_on(transport)

    # No OrderQty (38): structural, so a session-level Reject.
    await client.send("D", [(11, "I4"), (21, "1"), (55, "AAPL"), (54, "1"),
                            (40, "1"), (60, "20260926-12:00:00.000")])
    reject = await client.recv_until("3")
    assert reject.get(371) == "38"
    assert reject.get(373) == "1"
    assert "38" in reject.get(58)

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_evidence_and_logs_line_up(acceptor):
    transport = acceptor
    client = await logged_on(transport)

    await new_order(client, "I5", "AAPL", 100)
    await collect(client, 2)
    await client.send("5", [(58, "done")])
    await client.recv_until("5")
    await client.wait_closed(timeout=5.0)
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    records = read_evidence(transport)
    messages = [r for r in records if r["kind"] in ("in", "out")]

    # Every ExecutionReport is followed by an order-snapshot event.
    snapshot_keys = {
        "order_id", "cl_ord_id", "symbol", "side", "order_qty", "cum_qty",
        "leaves_qty", "avg_px", "ord_status", "price_source", "rule_name",
    }
    seen_reports = 0
    for index, record in enumerate(records):
        if record["kind"] == "out" and record["msg_type"] in ("8", "9"):
            seen_reports += 1
            following = records[index + 1]
            assert following["kind"] == "event"
            assert following["order"] is not None
            assert set(following["order"]) == snapshot_keys
            assert all(isinstance(value, str)
                       for value in following["order"].values())
    assert seen_reports == 2                    # the ack and the fill

    # Non-order records carry a null order key.
    for record in messages:
        assert record["order"] is None
        assert record["injected"] is False

    # The FIX log holds exactly the messages, one line each.
    assert len(read_fix_log(transport)) == len(messages)

    with open(transport.seq_store.path, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["next_out"] == len([r for r in messages if r["kind"] == "out"]) + 1


async def test_orders_survive_a_reconnect_and_fire_after_logon(acceptor):
    """Scheduled events are deferred, not lost, while the session is down."""
    transport = acceptor
    client = FixTestClient(fix_version=version_of(transport))
    await client.connect("127.0.0.1", transport.port)
    await client.logon(HEARTBEAT)
    assert (await client.recv()).msg_type == "A"

    # A rule with a long delay, so the fill is still pending when we drop.
    await new_order(client, "I6", "AAPL", 1000)
    ack = (await collect(client, 1))[0]
    order_id = ack.get(37)

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)
    assert transport.order_book.get_order(order_id) is not None

    again = FixTestClient(fix_version=version_of(transport))
    await again.connect("127.0.0.1", transport.port)
    await again.logon(HEARTBEAT, reset=True)
    assert (await again.recv()).msg_type == "A"

    fill = (await collect(again, 1))[0]
    assert fill.get(37) == order_id
    assert fill.get(150) == fill_type(transport)
    await again.close()
    await transport.wait_for_session_end(timeout=5.0)


# ===========================================================================
# Cook 4: instant acks over the wire (spec 4, tests 10.4)
# ===========================================================================

async def test_market_order_is_acked_before_the_price_lookup_finishes(
        acceptor, monkeypatch):
    """The ack must not wait on Yahoo; the fill does."""
    import time as _time

    import orderecho_Transport
    from orderecho_Pricing import PriceQuote

    transport = acceptor
    lookup_delay = 0.6

    async def slow_resolve(source, symbol, timeout_sec):
        await asyncio.sleep(lookup_delay)
        return PriceQuote(Decimal("227.50"), "live:yfinance")

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", slow_resolve)

    client = await logged_on(transport)
    begin = _time.monotonic()
    await new_order(client, "P1", "AAPL", 1000)

    ack = (await collect(client, 1))[0]
    ack_delay = _time.monotonic() - begin
    assert ack.get(150) == "0"
    assert ack.get(39) == "0"
    assert ack_delay < lookup_delay      # acked without waiting for the quote

    fill = (await collect(client, 1, timeout=10.0))[0]
    fill_delay = _time.monotonic() - begin
    assert fill.get(150) == fill_type(transport)
    assert fill.get(31) == "227.50"
    assert fill_delay >= lookup_delay    # but the fill did wait

    engine = read_engine_log(transport)
    assert "price pending: AAPL market order acked before its quote" in engine
    assert "after the ack" in engine     # the delay is recorded

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_limit_orders_do_not_wait_for_any_lookup(acceptor, monkeypatch):
    """A limit order prices itself; with the band off there is nothing to fetch."""
    import orderecho_Transport

    transport = acceptor
    called = []

    async def tracking_resolve(source, symbol, timeout_sec):
        called.append(symbol)
        return None

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", tracking_resolve)

    client = await logged_on(transport)
    await new_order(client, "P2", "EFG", 1000, ord_type="2", price="10.25")
    ack = (await collect(client, 1))[0]
    assert ack.get(150) == "0"
    assert ack.get(44) == "10.25"
    assert called == []                  # price_band is disabled in this config

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)
