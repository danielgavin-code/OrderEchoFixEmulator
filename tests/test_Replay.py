"""Real message replay on ResendRequest (spec 6)."""

import asyncio
import json
import os
import socket
from decimal import Decimal

import httpx
import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import SystemClock
from orderecho_Codec import FixMsg
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
from orderecho_ControlApi import ControlApiServer
from orderecho_MessageStore import MessageStore
from orderecho_Rules import load_rules
from orderecho_Transport import Transport

HEARTBEAT = 30
DELAY_MS = 50

# Two equal partial fills, so an order produces an ack plus two ERs.
RULES = [
    {"name": "halves", "match": {"symbol": "EFG"}, "behavior": "partial_fill",
     "fills": ["50%", "50%"], "then": "leave", "delay_ms": DELAY_MS},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, fix_port, api_port, resend_mode="replay") -> Config:
    return Config(
        session=SessionConfig(
            fix_version="FIX.4.2", sender_comp_id="ORDERECHO",
            target_comp_id="AGENT", host="127.0.0.1", port=fix_port,
            heartbeat_grace_pct=20, logout_timeout_sec=10,
            resend_mode=resend_mode,
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
        pricing=PricingConfig(mode="static",
                              static={"AAPL": Decimal("227.50")},
                              static_default=Decimal("100.00")),
        control_api=ControlApiConfig(enabled=True, host="127.0.0.1",
                                     port=api_port),
        price_band=PriceBandConfig(enabled=False),
        rules=load_rules(RULES, DELAY_MS),
        path="<test>",
    )


class Engine:
    """A running acceptor plus its control API, restartable in place."""

    def __init__(self, config):
        self.config = config
        self.transport = None
        self.api = None
        self.http = None

    async def start(self):
        self.transport = Transport(self.config, clock=SystemClock())
        await self.transport.start(self.config.session.port)
        self.api = ControlApiServer(self.config, self.transport)
        await self.api.start()
        self.http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.api.bound_port}", timeout=10.0
        )
        return self.transport

    async def stop(self):
        if self.http is not None:
            await self.http.aclose()
            self.http = None
        if self.api is not None:
            await self.api.stop()
            self.api = None
        if self.transport is not None:
            await self.transport.stop()
            self.transport.close_logs()
            self.transport = None


@pytest.fixture
async def engine(tmp_path):
    config = make_config(tmp_path, free_port(), free_port())
    running = Engine(config)
    await running.start()
    try:
        yield running
    finally:
        await running.stop()


async def logged_on(transport, reset=False,
                    continue_seq=False) -> FixTestClient:
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    if continue_seq:
        # The emulator persists sequence numbers, so a fresh client has to
        # pick up where the last one left off rather than start at 1.
        _next_out, next_in = transport.seq_store.load()
        client.next_out = next_in
    await client.logon(HEARTBEAT, reset=reset)
    reply = await client.recv()
    assert reply.msg_type == "A"
    return client


async def send_order(client, cl_ord_id, symbol="EFG", qty=100, ord_type="2",
                     price="10.00"):
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
              (38, str(qty)), (40, ord_type), (60, "20260927-12:00:00.000")]
    if price is not None:
        fields.append((44, price))
    await client.send("D", fields)


async def collect(client, count, msg_type="8", timeout=5.0):
    out = []
    while len(out) < count:
        msg = await client.recv(timeout)
        if msg is None:
            break
        if isinstance(msg, FixMsg) and msg.msg_type == msg_type:
            out.append(msg)
    return out


async def drain(client, seconds=0.4):
    """Read whatever arrives in the next *seconds*."""
    seen = []
    try:
        while True:
            msg = await asyncio.wait_for(client.recv(seconds), seconds + 0.2)
            if msg is None:
                break
            seen.append(msg)
    except asyncio.TimeoutError:
        pass
    return seen


def comparable(msg: FixMsg) -> list:
    """Fields that a replay must preserve exactly."""
    skip = {8, 9, 10, 43, 52, 122}
    return [(tag, value) for tag, value in msg.pairs if tag not in skip]


def read_engine_log(transport):
    with open(transport.engine_log.path, encoding="utf-8") as handle:
        return handle.read()


def read_evidence(transport):
    with open(transport.evidence.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# 15 ------------------------------------------------------------------------

async def test_execution_reports_are_replayed_verbatim(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R1")
    originals = await collect(client, 3)
    assert [msg.get(150) for msg in originals] == ["0", "1", "2"]
    first, last = originals[0].seq_num, originals[-1].seq_num
    assert (first, last) == (2, 4)

    await client.send("2", [(7, str(first)), (16, str(last))])
    replays = await collect(client, 3)

    assert [msg.seq_num for msg in replays] == [2, 3, 4]
    for original, replayed in zip(originals, replays):
        assert replayed.get(43) == "Y"
        assert replayed.get(122) == original.get(52)
        assert replayed.get(52) != original.get(52)      # resent now
        assert comparable(replayed) == comparable(original)
        # Framing was recomputed, so the client accepted it as a valid frame.
        assert isinstance(replayed, FixMsg)

    # None of it consumed a new sequence number.
    assert transport.session.next_out == last + 1

    engine_log = read_engine_log(transport)
    assert "RESEND 2..4 -> replayed 3, gap-filled 0 run(s)" in engine_log

    await client.close()


# 16 ------------------------------------------------------------------------

async def test_admin_runs_collapse_into_single_gap_fills(engine):
    transport, api = engine.transport, engine.http
    client = await logged_on(transport)

    # seq 2..4: the ack and two fills.
    await send_order(client, "R2")
    await collect(client, 3)
    # seq 5, 6: two admin messages.
    await api.post("/session/test-request")
    await client.recv_until("1")
    await api.post("/session/test-request")
    await client.recv_until("1")
    # seq 7..9: another order.
    await send_order(client, "R3")
    await collect(client, 3)

    await client.send("2", [(7, "2"), (16, "0")])
    replayed = await drain(client, 0.5)
    kinds = [(msg.msg_type, msg.seq_num) for msg in replayed
             if isinstance(msg, FixMsg)]

    # ERs come back as themselves; the admin run 5..6 collapses to one gap fill.
    assert ("8", 2) in kinds and ("8", 3) in kinds and ("8", 4) in kinds
    assert ("8", 7) in kinds and ("8", 8) in kinds and ("8", 9) in kinds
    gap_fills = [msg for msg in replayed
                 if isinstance(msg, FixMsg) and msg.msg_type == "4"]
    assert len(gap_fills) == 1
    assert gap_fills[0].seq_num == 5
    assert gap_fills[0].get(36) == "7"       # first seq after the run
    assert gap_fills[0].get(123) == "Y"
    assert gap_fills[0].get(43) == "Y"

    assert "replayed 6, gap-filled 1 run(s)" in read_engine_log(transport)
    await client.close()


# 17 ------------------------------------------------------------------------

async def test_bounded_end_seq_no_is_respected(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R4")
    await collect(client, 3)

    await client.send("2", [(7, "2"), (16, "3")])
    replays = await collect(client, 2)
    assert [msg.seq_num for msg in replays] == [2, 3]
    assert await client.recv(timeout=0.3) is None    # 4 was out of range

    await client.close()


async def test_zero_end_seq_no_goes_to_the_last_message_sent(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R5")
    await collect(client, 3)
    last = transport.session.next_out - 1

    await client.send("2", [(7, "2"), (16, "0")])
    replays = await collect(client, 3)
    assert [msg.seq_num for msg in replays] == [2, 3, last]

    await client.close()


async def test_begin_beyond_what_we_sent_replays_nothing(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R6")
    await collect(client, 3)

    await client.send("2", [(7, "99"), (16, "0")])
    assert await client.recv(timeout=0.5) is None
    assert "resend request for future seqnums ignored" in \
        read_engine_log(transport)

    await client.close()


# 18 ------------------------------------------------------------------------

async def test_injected_sequence_gap_is_gap_filled_between_replays(engine):
    transport, api = engine.transport, engine.http
    client = await logged_on(transport)

    await send_order(client, "R7")
    await collect(client, 3)                       # seq 2, 3, 4
    await api.post("/inject/seq-gap", json={"skip": 3})   # 5, 6, 7 vanish
    await send_order(client, "R8")
    await collect(client, 3)                       # seq 8, 9, 10

    await client.send("2", [(7, "2"), (16, "0")])
    replayed = await drain(client, 0.6)
    seqs = [(msg.msg_type, msg.seq_num) for msg in replayed
            if isinstance(msg, FixMsg)]

    assert ("8", 2) in seqs and ("8", 4) in seqs
    assert ("8", 8) in seqs and ("8", 10) in seqs
    gap_fills = [msg for msg in replayed
                 if isinstance(msg, FixMsg) and msg.msg_type == "4"]
    assert len(gap_fills) == 1
    assert gap_fills[0].seq_num == 5          # the first missing seq
    assert gap_fills[0].get(36) == "8"        # first seq that exists again

    await client.close()


# 19 ------------------------------------------------------------------------

async def test_replay_of_an_injected_message_keeps_the_mutation(engine):
    transport, api = engine.transport, engine.http
    client = await logged_on(transport)

    await api.post("/inject/next",
                   json={"msg_type": "8", "set": {"9999": "FOO"}})
    await send_order(client, "R9")
    originals = await collect(client, 3)
    assert originals[0].get(9999) == "FOO"
    assert originals[1].get(9999) is None

    await client.send("2", [(7, "2"), (16, "2")])
    replay = (await collect(client, 1))[0]
    assert replay.seq_num == 2
    assert replay.get(9999) == "FOO"          # the mutation travelled with it
    assert replay.get(43) == "Y"

    records = [r for r in read_evidence(transport)
               if r["kind"] == "out" and r["seq"] == 2]
    assert records[-1]["injected"] is True
    assert records[-1]["detail"] == "replay of injected seq 2"

    await client.close()


async def test_replay_of_a_clean_message_is_not_marked_injected(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R10")
    await collect(client, 3)

    await client.send("2", [(7, "2"), (16, "2")])
    await collect(client, 1)

    records = [r for r in read_evidence(transport)
               if r["kind"] == "out" and r["seq"] == 2]
    assert records[-1]["injected"] is False
    assert records[-1]["detail"] == "replay of seq 2"

    await client.close()


# 20 ------------------------------------------------------------------------

async def test_replay_survives_an_engine_restart(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R11")
    originals = await collect(client, 3)
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    # Restart the whole engine; the store is on disk.
    await engine.stop()
    transport = await engine.start()
    assert len(transport.message_store) >= 3

    again = await logged_on(transport, continue_seq=True)
    await again.send("2", [(7, "2"), (16, "4")])
    replays = await collect(again, 3)

    assert [msg.seq_num for msg in replays] == [2, 3, 4]
    for original, replayed in zip(originals, replays):
        assert comparable(replayed) == comparable(original)
        assert replayed.get(122) == original.get(52)

    await again.close()


# 21 ------------------------------------------------------------------------

async def test_logon_with_reset_archives_the_store(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R12")
    await collect(client, 3)
    assert len(transport.message_store) >= 3
    store_path = transport.message_store.path
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    again = await logged_on(transport, reset=True)
    assert len(transport.message_store) == 1      # just the new Logon reply

    directory = os.path.dirname(store_path)
    archived = [name for name in os.listdir(directory)
                if name.startswith(os.path.basename(store_path) + ".")]
    assert len(archived) == 1
    with open(os.path.join(directory, archived[0]), encoding="utf-8") as handle:
        kept = [json.loads(line) for line in handle if line.strip()]
    assert any(record["msg_type"] == "8" for record in kept)

    assert "store archived" in read_engine_log(transport)
    await again.close()


async def test_reset_seqnums_via_the_api_archives_the_store(engine):
    transport, api = engine.transport, engine.http
    client = await logged_on(transport)
    await send_order(client, "R13")
    await collect(client, 3)
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    response = await api.post("/session/reset-seqnums")
    assert response.status_code == 200
    assert response.json()["archived_store"] is not None
    assert len(transport.message_store) == 0
    assert "reset via control API" in read_engine_log(transport)


# 22 ------------------------------------------------------------------------

async def test_gapfill_mode_keeps_the_cook_3_behavior(tmp_path):
    config = make_config(tmp_path, free_port(), free_port(),
                         resend_mode="gapfill")
    running = Engine(config)
    transport = await running.start()
    try:
        client = await logged_on(transport)
        await send_order(client, "R14")
        await collect(client, 3)

        await client.send("2", [(7, "2"), (16, "0")])
        gap_fill = await client.recv_until("4")
        assert gap_fill.seq_num == 2
        assert gap_fill.get(123) == "Y"
        assert gap_fill.get(36) == str(transport.session.next_out)
        assert await client.recv(timeout=0.3) is None    # nothing replayed

        await client.close()
    finally:
        await running.stop()


# the store itself ----------------------------------------------------------

async def test_gap_fills_and_replays_are_never_stored(engine):
    transport = engine.transport
    client = await logged_on(transport)
    await send_order(client, "R15")
    await collect(client, 3)
    stored_before = dict(transport.message_store._messages)

    await client.send("2", [(7, "2"), (16, "0")])
    await drain(client, 0.5)

    # Replaying must not overwrite the originals it is replaying.
    assert transport.message_store._messages == stored_before
    await client.close()


def test_store_skips_corrupt_lines(tmp_path):
    directory = str(tmp_path / "msgstore")
    store = MessageStore(directory, "ORDERECHO", "AGENT")
    store.append(1, "8", b"8=FIX.4.2\x0135=8\x0110=001\x01")
    store.close()
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
        handle.write('{"no_seq": true}\n')
    reloaded = MessageStore(directory, "ORDERECHO", "AGENT")
    assert len(reloaded) == 1
    assert reloaded.get(1)["msg_type"] == "8"
