"""Cook 4 carry-overs fixed in Cook 5 (spec 3, tests 9.8-9.11, 9.13)."""

import asyncio
import socket
import sys
import time
from decimal import Decimal

import httpx
import pytest

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import FakeClock, SystemClock
from orderecho_Config import PriceBandConfig, PricingConfig
from orderecho_ControlApi import ControlApiServer
from orderecho_FixVersion import FIX_4_2, FIX_4_4
from orderecho_Pricing import (
    SOURCE_LIVE_YFINANCE,
    PriceQuote,
    StaticPriceSource,
    YFinancePriceSource,
    warm_up,
)
from orderecho_Transport import Transport
from orderecho_Version import ORDERECHO_BUILD, ORDERECHO_VERSION
from test_Pricing import FakeLog, START, make_noisy_yfinance


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# 8 -------------------------------------------------------------------------

def test_warm_symbols_are_all_fetched_in_the_background(monkeypatch):
    make_noisy_yfinance(monkeypatch, 100.0)
    log = FakeLog()
    source = YFinancePriceSource(StaticPriceSource({}, "100"),
                                 FakeClock(START), engine_log=log)

    asked = []
    started = __import__("threading").Event()

    def slow(symbol):
        asked.append(symbol)
        started.set()
        time.sleep(0.15)
        return PriceQuote(Decimal("1.00"), SOURCE_LIVE_YFINANCE)

    monkeypatch.setattr(source, "get", slow)

    begin = time.monotonic()
    thread = warm_up(source, ["AAPL", "MSFT", "SPY"], engine_log=log)
    elapsed = time.monotonic() - begin

    assert elapsed < 0.1                  # startup did not wait
    assert started.wait(5.0)
    thread.join(5.0)
    assert asked == ["AAPL", "MSFT", "SPY"]
    assert len(log.infos) == 3


def test_warm_symbols_failure_does_not_stop_the_rest(monkeypatch):
    make_noisy_yfinance(monkeypatch, 100.0)
    log = FakeLog()
    source = YFinancePriceSource(StaticPriceSource({}, "100"),
                                 FakeClock(START), engine_log=log)

    def sometimes(symbol):
        if symbol == "MSFT":
            raise RuntimeError("no route to host")
        return PriceQuote(Decimal("2.00"), SOURCE_LIVE_YFINANCE)

    monkeypatch.setattr(source, "get", sometimes)
    warm_up(source, ["AAPL", "MSFT", "SPY"], engine_log=log).join(5.0)
    assert len(log.infos) == 2
    assert any("MSFT" in line and "no route" in line for line in log.warnings)


def test_a_warmed_symbol_is_a_cache_hit_afterwards(monkeypatch):
    calls = []
    make_noisy_yfinance(monkeypatch, 231.00)
    log = FakeLog()
    clock = FakeClock(START)
    source = YFinancePriceSource(StaticPriceSource({}, "100"), clock,
                                 engine_log=log)
    real_get = source.get

    def counting(symbol):
        calls.append(symbol)
        return real_get(symbol)

    monkeypatch.setattr(source, "get", counting)
    warm_up(source, ["AAPL"], engine_log=log).join(5.0)
    assert calls == ["AAPL"]

    quote = source.get("AAPL")            # served from the warm cache
    assert quote.source == SOURCE_LIVE_YFINANCE
    assert calls == ["AAPL", "AAPL"]      # our wrapper counted, the fetch did not
    assert len(source._cache) == 1


def test_warm_up_is_skipped_in_static_mode():
    assert warm_up(StaticPriceSource({}, "100"), ["AAPL"]) is None


# 9 -------------------------------------------------------------------------

async def start_engine(tmp_path, **kwargs):
    config = isolated_config(tmp_path, fix_port=free_port(), **kwargs)
    transport = Transport(config, clock=SystemClock())
    await transport.start(config.session.port)
    client = FixTestClient(fix_version=config.session.fix_version)
    await client.connect("127.0.0.1", transport.port)
    await client.logon(30)
    assert (await client.recv()).msg_type == "A"
    return transport, client


async def send_limit(client, cl_ord_id, price, symbol="ZWZZT",
                     version=FIX_4_2):
    fields = [(11, cl_ord_id), (55, symbol), (54, "1"), (38, "100"),
              (40, "2"), (44, price), (60, "20260101-00:00:00.000")]
    if version == FIX_4_2:
        fields.insert(1, (21, "1"))
    await client.send("D", fields)


def no_reference_pricing():
    """A pricing config whose lookups never produce a usable quote."""
    return PricingConfig(mode="static", static={}, static_default=Decimal("1"),
                         warm_symbols=[])


@pytest.mark.parametrize("version,expected_code", [(FIX_4_2, "0"),
                                                   (FIX_4_4, "99")])
async def test_on_no_reference_reject(tmp_path, monkeypatch, version,
                                      expected_code):
    import orderecho_Transport

    async def no_quote(source, symbol, timeout_sec):
        return None

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", no_quote)
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive",
                           on_no_reference="reject")
    transport, client = await start_engine(tmp_path, fix_version=version,
                                           band=band)
    try:
        await send_limit(client, "NR1", "100.00", version=version)
        report = await client.recv_until("8")
        assert report.get(150) == "8"
        assert report.get(39) == "8"
        assert report.get(103) == expected_code
        assert report.get(58) == "No reference price available"
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


async def test_on_no_reference_skip_logs_a_warning(tmp_path, monkeypatch):
    import orderecho_Transport

    async def no_quote(source, symbol, timeout_sec):
        return None

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", no_quote)
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive",
                           on_no_reference="skip")
    transport, client = await start_engine(tmp_path, band=band)
    try:
        await send_limit(client, "NR2", "99999.00")
        report = await client.recv_until("8")
        assert report.get(150) == "0"          # acked, band not applied

        with open(transport.engine_log.path, encoding="utf-8") as handle:
            engine = handle.read()
        assert "WARNING" in engine
        warning_lines = [line for line in engine.splitlines()
                         if "WARNING" in line and "BAND " in line]
        assert warning_lines, engine
        assert "-> SKIPPED (no reference price available)" in warning_lines[0]
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


# 10 ------------------------------------------------------------------------

async def test_band_is_checked_before_the_rule(tmp_path):
    """A reject-rule symbol out of band gets the band reason (spec 3.2)."""
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive")
    rules = [
        {"name": "always-reject", "match": {"symbol": "ZVZZT"},
         "behavior": "reject", "reject_code": 1, "text": "Unknown symbol"},
        {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
    ]
    pricing = PricingConfig(mode="static", static={"ZVZZT": Decimal("100.00")},
                            static_default=Decimal("100.00"), warm_symbols=[])
    transport, client = await start_engine(tmp_path, band=band, rules=rules,
                                           pricing=pricing)
    try:
        await send_limit(client, "BR1", "500.00", symbol="ZVZZT")
        report = await client.recv_until("8")
        assert report.get(150) == "8"
        # The band's reason, not the rule's.
        assert report.get(103) == "3"
        assert report.get(58) == (
            "Limit 500.00 outside 10% band of ref 100.00 (static:config)"
        )
        assert "Unknown symbol" != report.get(58)

        with open(transport.engine_log.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        band_at = next(i for i, line in enumerate(lines) if "BAND  BR1" in line)
        rule_at = [i for i, line in enumerate(lines)
                   if "rule matched" in line and "ZVZZT" in line]
        # The band decision is logged, and the rule never gets a say.
        assert band_at >= 0
        assert not rule_at or rule_at[0] > band_at
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


async def test_an_in_band_limit_still_gets_the_rule(tmp_path):
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive")
    rules = [
        {"name": "always-reject", "match": {"symbol": "ZVZZT"},
         "behavior": "reject", "reject_code": 1, "text": "Unknown symbol"},
        {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
    ]
    pricing = PricingConfig(mode="static", static={"ZVZZT": Decimal("100.00")},
                            static_default=Decimal("100.00"), warm_symbols=[])
    transport, client = await start_engine(tmp_path, band=band, rules=rules,
                                           pricing=pricing)
    try:
        await send_limit(client, "BR2", "101.00", symbol="ZVZZT")
        report = await client.recv_until("8")
        assert report.get(150) == "8"
        assert report.get(103) == "1"                 # the rule's own code
        assert report.get(58) == "Unknown symbol"
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


# 11 ------------------------------------------------------------------------

async def test_health_and_status_report_version_build_and_fix_version(tmp_path):
    config = isolated_config(tmp_path, fix_version=FIX_4_4,
                             fix_port=free_port(), api_port=free_port(),
                             control_api_enabled=True)
    transport = Transport(config, clock=SystemClock())
    await transport.start(config.session.port)
    api = ControlApiServer(config, transport)
    await api.start()
    http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{api.bound_port}",
                             timeout=10.0)
    try:
        health = (await http.get("/health")).json()
        assert health == {"ok": True, "version": ORDERECHO_VERSION,
                          "build": ORDERECHO_BUILD, "sessions": 1}
        assert health["version"] == "0.8.0"
        assert health["build"] == "cook8"

        status = (await http.get("/status")).json()
        assert status["fix_version"] == FIX_4_4
    finally:
        await http.aclose()
        await api.stop()
        await transport.stop()
        transport.close_logs()


def test_only_one_module_names_the_build():
    import pathlib
    offenders = []
    for path in pathlib.Path(".").glob("orderecho_*.py"):
        if path.name == "orderecho_Version.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("cook3", "cook4", "cook5", "cook6", "cook7",
                       "cook8", "Cook 4", "Cook 5", "Cook 6", "Cook 7",
                       "Cook 8"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


# 13 ------------------------------------------------------------------------

def test_bulk_orders_get_unique_cl_ord_ids():
    from orderecho_DemoClient import DemoClient

    client = DemoClient("127.0.0.1", 1, "AGENT", "ORDERECHO")
    ids = [client.next_cl_ord_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(value.startswith("DEMO-") for value in ids)
    suffixes = [int(value.rsplit("-", 1)[1]) for value in ids]
    assert suffixes == list(range(1, 201))


def test_cancel_and_replace_ids_are_also_unique():
    from orderecho_DemoClient import DemoClient

    client = DemoClient("127.0.0.1", 1, "AGENT", "ORDERECHO")
    values = [client.next_cl_ord_id(), client.next_cl_ord_id("-C"),
              client.next_cl_ord_id("-R")]
    assert len(set(values)) == 3
    assert values[1].endswith("-C") and values[2].endswith("-R")


def test_possdup_messages_are_marked():
    from orderecho_Codec import Codec
    from orderecho_DemoClient import describe

    codec = Codec(FIX_4_2)
    raw = codec.encode(
        "8", [(37, "O-1"), (150, "F"), (39, "2"), (55, "AAPL"),
              (14, "100"), (151, "0"), (6, "10.0000")],
        sender_comp_id="ORDERECHO", target_comp_id="AGENT", seq_num=3,
        sending_time=START, poss_dup=True, orig_sending_time=START,
    )
    msg = codec.decode(raw)[0]
    line = describe(msg)
    assert "(PossDup)" in line
    assert "exec=TRADE" in line               # 4.4 naming for 150=F

    plain = codec.decode(codec.encode(
        "8", [(37, "O-1"), (150, "0"), (39, "0")],
        sender_comp_id="ORDERECHO", target_comp_id="AGENT", seq_num=4,
        sending_time=START))[0]
    assert "(PossDup)" not in describe(plain)


def test_status_lists_an_order_the_moment_it_is_sent():
    import io

    from orderecho_DemoClient import DemoClient

    out = io.StringIO()
    client = DemoClient("127.0.0.1", 1, "AGENT", "ORDERECHO", out=out)
    client.my_orders["DEMO-1-1"] = {
        "cl_ord_id": "DEMO-1-1", "order_id": None, "symbol": "AAPL",
        "side": "1", "ord_status": None, "cum_qty": None,
        "leaves_qty": "500", "avg_px": None,
    }
    client.print_status()
    printed = out.getvalue()
    assert "DEMO-1-1" in printed
    assert "SENT" in printed
    assert "AAPL" in printed
