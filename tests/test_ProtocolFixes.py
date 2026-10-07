"""Protocol fixes (Cook 9 spec 6): 379 on a BusinessMessageReject, the gap
queue with a closed-range ResendRequest, and the negative price cache."""

import asyncio
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import FakeClock, SystemClock
from orderecho_Codec import Codec
from orderecho_Config import SessionConfig
from orderecho_Pricing import (
    SOURCE_STATIC_FALLBACK,
    StaticPriceSource,
    YFinancePriceSource,
    resolve_quote,
)
from orderecho_SeqStore import MemorySeqStore
from orderecho_Session import GAP_QUEUE_LIMIT, Disconnect, Evidence, Send, Session
from orderecho_Transport import Transport

START = datetime(2026, 10, 1, 14, 0, 0, tzinfo=timezone.utc)
HEARTBEAT = 30


def make_session(next_out=1, next_in=1, **overrides):
    values = dict(fix_version="FIX.4.2", sender_comp_id="ORDERECHO",
                  target_comp_id="AGENT", host="127.0.0.1", port=9878,
                  heartbeat_grace_pct=20, logout_timeout_sec=10)
    values.update(overrides)
    clock = FakeClock(START)
    session = Session(SessionConfig(**values), MemorySeqStore(next_out, next_in),
                      clock)
    return session


def inbound(msg_type, body=None, seq=1, poss_dup=False):
    codec = Codec("FIX.4.2")
    raw = codec.encode(msg_type, body or [], sender_comp_id="AGENT",
                       target_comp_id="ORDERECHO", seq_num=seq,
                       sending_time=START, poss_dup=poss_dup,
                       orig_sending_time=START if poss_dup else None)
    return codec.decode(raw)[0]


def logon(session, seq=1):
    session.on_connect()
    return session.on_message(inbound("A", [(98, "0"), (108, str(HEARTBEAT))],
                                      seq=seq))


def sends(actions):
    return [a for a in actions if isinstance(a, Send)]


def field(send, tag):
    for pair_tag, value in send.body_fields:
        if int(pair_tag) == tag:
            return value
    return None


def events(actions):
    return [a.event for a in actions if isinstance(a, Evidence)]


# --------------------------------------------------- 6.1: 379 on a 35=j

def test_business_reject_carries_379_when_the_message_had_an_id():
    session = make_session(business_reject_ref_id=True)
    logon(session)
    out = sends(session.on_message(
        inbound("ZZ", [(11, "ORD-7"), (58, "provoke")], seq=2)))
    assert out[0].msg_type == "j"
    assert [(t, v) for t, v in out[0].body_fields if t in (45, 372, 379, 380)] \
        == [(45, "2"), (372, "ZZ"), (379, "ORD-7"), (380, "3")]


@pytest.mark.parametrize("body,expected", [
    ([(117, "Q-1")], "Q-1"),          # QuoteID
    ([(262, "MD-1")], "MD-1"),        # MDReqID
    ([(37, "O-1"), (11, "C-1")], "C-1"),   # ClOrdID first, as FIX lists it
])
def test_379_uses_the_business_id_the_message_carries(body, expected):
    session = make_session(business_reject_ref_id=True)
    logon(session)
    out = sends(session.on_message(inbound("ZZ", body, seq=2)))
    assert field(out[0], 379) == expected


def test_no_379_when_there_is_no_business_id():
    """The agent's case 7.8 sends 35=ZZ with only a Text: per FIX, 379 is
    absent then, because there is nothing for it to refer to."""
    session = make_session(business_reject_ref_id=True)
    logon(session)
    out = sends(session.on_message(
        inbound("ZZ", [(58, "OrderEcho cert: provoke a BusinessMessageReject")],
                seq=2)))
    assert field(out[0], 379) is None
    assert [field(out[0], t) for t in (45, 372, 380, 58)] == [
        "2", "ZZ", "3", "Not supported in this build"]


def test_379_is_off_by_default():
    session = make_session()
    logon(session)
    out = sends(session.on_message(inbound("ZZ", [(11, "ORD-8")], seq=2)))
    assert field(out[0], 379) is None


# ------------------------------------- 6.2: gap queue, closed-range resend

def test_a_gap_asks_for_exactly_the_missing_range_and_holds_the_message():
    session = make_session(gap_queue=True)
    logon(session)
    out = session.on_message(inbound("0", seq=5))
    resend = sends(out)
    assert [s.msg_type for s in resend] == ["2"]
    assert (field(resend[0], 7), field(resend[0], 16)) == ("2", "4")
    assert set(session.held) == {5}
    assert session.expected_in == 2
    # A later message is held too, without a second ResendRequest.
    out = session.on_message(inbound("0", seq=6))
    assert sends(out) == [] and set(session.held) == {5, 6}


def test_a_test_request_behind_a_gap_is_answered_after_the_fill():
    session = make_session(gap_queue=True)
    logon(session)
    held = session.on_message(inbound("1", [(112, "TR-1")], seq=4))
    assert [s.msg_type for s in sends(held)] == ["2"]      # no Heartbeat yet
    # The resend arrives: 2 and 3 replayed (PossDup).
    first = session.on_message(inbound("0", seq=2, poss_dup=True))
    assert sends(first) == []
    second = session.on_message(inbound("0", seq=3, poss_dup=True))
    heartbeat = [s for s in sends(second) if s.msg_type == "0"]
    assert [field(s, 112) for s in heartbeat] == ["TR-1"]
    assert "held message processed" in events(second)
    assert session.expected_in == 5 and session.held == {}
    assert not session.resend_outstanding


def test_a_gap_fill_releases_held_messages_in_order():
    session = make_session(gap_queue=True)
    logon(session)
    session.on_message(inbound("1", [(112, "A")], seq=6))
    session.on_message(inbound("1", [(112, "B")], seq=5))
    out = session.on_message(inbound("4", [(123, "Y"), (36, "5")], seq=2,
                                     poss_dup=True))
    assert [field(s, 112) for s in sends(out)] == ["B", "A"]
    assert session.expected_in == 7


def test_a_second_hole_gets_its_own_resend_request():
    session = make_session(gap_queue=True)
    logon(session)
    session.on_message(inbound("0", seq=4))                # asks 2..3
    session.on_message(inbound("0", seq=8))                # held behind it
    out = session.on_message(inbound("4", [(123, "Y"), (36, "5")], seq=2,
                                     poss_dup=True))
    resend = [s for s in sends(out) if s.msg_type == "2"]
    assert [(field(s, 7), field(s, 16)) for s in resend] == [("5", "7")]
    assert session.expected_in == 5 and set(session.held) == {8}


def test_a_logon_that_reveals_a_gap_is_consumed_after_the_fill():
    session = make_session(next_in=3, gap_queue=True)
    out = logon(session, seq=7)
    assert [s.msg_type for s in sends(out)] == ["A", "2"]
    resend = sends(out)[1]
    assert (field(resend, 7), field(resend, 16)) == ("3", "6")
    out = session.on_message(inbound("4", [(123, "Y"), (36, "7")], seq=3,
                                     poss_dup=True))
    assert "held logon consumed" in events(out)
    assert session.expected_in == 8


def test_overflowing_the_gap_queue_logs_out():
    session = make_session(gap_queue=True)
    logon(session)
    for seq in range(5, 5 + GAP_QUEUE_LIMIT):
        assert not [a for a in session.on_message(inbound("0", seq=seq))
                    if isinstance(a, Disconnect)]
    out = session.on_message(inbound("0", seq=5 + GAP_QUEUE_LIMIT))
    logout = [s for s in sends(out) if s.msg_type == "5"]
    assert "Gap queue overflow" in field(logout[0], 58)
    assert [a for a in out if isinstance(a, Disconnect)]


def test_without_the_gap_queue_nothing_changes():
    session = make_session()
    logon(session)
    out = sends(session.on_message(inbound("0", seq=5)))
    assert (field(out[0], 7), field(out[0], 16)) == ("2", "0")
    assert session.held == {}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_gap_queue_end_to_end(tmp_path):
    config = isolated_config(tmp_path, fix_port=free_port())
    config.session.gap_queue = True
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    try:
        fix = FixTestClient()
        await fix.connect("127.0.0.1", transport.port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        # Skip seq 2 and 3: a TestRequest arrives as seq 4.
        await fix.send("1", [(112, "BEHIND")], seq=4)
        resend = await fix.recv_until("2")
        assert (resend.get(7), resend.get(16)) == ("2", "3")
        await fix.send("4", [(123, "Y"), (36, "4")], seq=2, poss_dup=True,
                       orig_sending_time=START)
        heartbeat = await fix.recv_until("0")
        assert heartbeat.get(112) == "BEHIND"
        await fix.close()
    finally:
        await transport.stop()
        transport.close_logs()


# ----------------------------------------------- 6.3: negative price cache

class _FlakyYFinance:
    calls = []

    class Ticker:
        def __init__(self, symbol):
            _FlakyYFinance.calls.append(symbol)
            raise RuntimeError("no data")


def make_live(monkeypatch, negative_cache_seconds):
    import sys
    _FlakyYFinance.calls = []
    monkeypatch.setitem(sys.modules, "yfinance", _FlakyYFinance)
    clock = FakeClock(START)
    source = YFinancePriceSource(StaticPriceSource({}, "100"), clock,
                                 negative_cache_seconds=negative_cache_seconds)
    return source, clock


def test_a_failed_lookup_is_remembered_for_negative_cache_seconds(monkeypatch):
    source, clock = make_live(monkeypatch, 300)
    first = source.get("ZZZZ")
    assert first.source == SOURCE_STATIC_FALLBACK
    assert source.get("ZZZZ") == first
    assert _FlakyYFinance.calls == ["ZZZZ"]            # not tried again
    clock.advance(299)
    source.get("ZZZZ")
    assert _FlakyYFinance.calls == ["ZZZZ"]
    clock.advance(1)
    source.get("ZZZZ")
    assert _FlakyYFinance.calls == ["ZZZZ", "ZZZZ"]    # expired: try again


async def test_only_the_first_order_waits_for_a_timeout(monkeypatch):
    """A lookup that hangs: the first order waits out the timeout and gets the
    fallback; the next one gets the same answer at once."""
    import time
    source, _clock = make_live(monkeypatch, 300)
    hung = []

    def hanging_ticker(symbol):
        hung.append(symbol)
        time.sleep(0.5)
        raise RuntimeError("hung")
    monkeypatch.setattr(_FlakyYFinance, "Ticker", hanging_ticker)

    loop = asyncio.get_running_loop()
    started = loop.time()
    first = await resolve_quote(source, "SLOW", timeout_sec=0.1)
    assert first.source == SOURCE_STATIC_FALLBACK
    assert loop.time() - started >= 0.09
    started = loop.time()
    second = await resolve_quote(source, "SLOW", timeout_sec=0.1)
    assert second == first
    assert loop.time() - started < 0.05                 # answered at once
    assert hung == ["SLOW"]                             # one lookup only


def test_the_engine_turns_the_negative_cache_on_from_config():
    from orderecho_Config import Config, LoggingConfig, PricingConfig, StorageConfig
    from orderecho_Pricing import build_price_source
    config = Config(
        session=SessionConfig(fix_version="FIX.4.2", sender_comp_id="A",
                              target_comp_id="B", host="127.0.0.1", port=1,
                              heartbeat_grace_pct=20, logout_timeout_sec=1),
        storage=StorageConfig(seqnum_dir="x", evidence_dir="y"),
        logging=LoggingConfig(log_dir="z", fix_delimiter="|",
                              engine_level="INFO", console=False),
        pricing=PricingConfig(mode="live"),
    )
    source = build_price_source(config, FakeClock(START))
    assert source.negative_cache_seconds == 300.0
