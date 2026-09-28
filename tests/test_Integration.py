"""End-to-end over real sockets on localhost."""

import asyncio
import json
import os
import socket

import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import SystemClock
from orderecho_Config import Config, LoggingConfig, SessionConfig, StorageConfig
from orderecho_Codec import FixMsg
from orderecho_Transport import Transport

HEARTBEAT = 30


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, port, console=False) -> Config:
    return Config(
        session=SessionConfig(
            fix_version="FIX.4.2",
            sender_comp_id="ORDERECHO",
            target_comp_id="AGENT",
            host="127.0.0.1",
            port=port,
            heartbeat_grace_pct=20,
            logout_timeout_sec=10,
        ),
        storage=StorageConfig(
            seqnum_dir=str(tmp_path / "data" / "seqnums"),
            evidence_dir=str(tmp_path / "data" / "evidence"),
            msgstore_dir=str(tmp_path / "data" / "msgstore"),
        ),
        logging=LoggingConfig(
            log_dir=str(tmp_path / "logs"),
            fix_delimiter="|",
            engine_level="DEBUG",
            console=console,
        ),
        path="<test>",
    )


@pytest.fixture
async def acceptor(tmp_path):
    config = make_config(tmp_path, free_port())
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    try:
        yield transport
    finally:
        await transport.stop()
        transport.close_logs()


def read_evidence(transport):
    with open(transport.evidence.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_fix_log(transport):
    with open(transport.fix_log.path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


def read_engine_log(transport):
    with open(transport.engine_log.path, encoding="utf-8") as handle:
        return handle.read()


async def test_logon_testrequest_logout_end_to_end(acceptor):
    transport = acceptor
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)

    await client.logon(HEARTBEAT)
    logon_reply = await client.recv()
    assert isinstance(logon_reply, FixMsg)
    assert logon_reply.msg_type == "A"
    assert logon_reply.seq_num == 1
    assert logon_reply.get(49) == "ORDERECHO"
    assert logon_reply.get(56) == "AGENT"
    assert logon_reply.get(98) == "0"
    assert logon_reply.get(108) == str(HEARTBEAT)

    await client.send("1", [(112, "TEST-42")])
    heartbeat = await client.recv()
    assert heartbeat.msg_type == "0"
    assert heartbeat.get(112) == "TEST-42"
    assert heartbeat.seq_num == 2

    await client.send("5", [(58, "done")])
    logout = await client.recv()
    assert logout.msg_type == "5"
    assert logout.seq_num == 3

    assert await client.wait_closed(timeout=5.0)
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    # --- evidence -------------------------------------------------------
    records = read_evidence(transport)
    messages = [r for r in records if r["kind"] in ("in", "out")]
    assert [(r["kind"], r["msg_type"], r["seq"]) for r in messages] == [
        ("in", "A", 1),
        ("out", "A", 1),
        ("in", "1", 2),
        ("out", "0", 2),
        ("in", "5", 3),
        ("out", "5", 3),
    ]
    for record in records:
        assert record["injected"] is False
        assert record["session"] == "ORDERECHO-AGENT"
        assert record["run_id"] == transport.run_id
        assert record["ts"].endswith("Z")
    for record in messages:
        assert "\x01" not in record["raw"]
        assert record["raw"].startswith("8=FIX.4.2|")
        # fields is an ordered list of [tag, value] pairs (spec 3.3).
        assert isinstance(record["fields"], list)
        assert all(isinstance(pair, list) and len(pair) == 2
                   for pair in record["fields"])
        as_dict = dict(record["fields"])
        assert as_dict["35"] == record["msg_type"]
        assert as_dict["34"] == str(record["seq"])
        assert [pair[0] for pair in record["fields"][:3]] == ["8", "9", "35"]

    # --- sequence numbers ----------------------------------------------
    with open(transport.seq_store.path, encoding="utf-8") as handle:
        assert json.load(handle) == {"next_out": 4, "next_in": 4}

    # --- fix log matches the evidence, one line each ---------------------
    fix_lines = read_fix_log(transport)
    assert len(fix_lines) == len(messages)
    for line, record in zip(fix_lines, messages):
        direction = "IN " if record["kind"] == "in" else "OUT"
        assert line.split()[1].strip() == direction.strip()
        assert f"seq={record['seq']}" in line
        assert f"35={record['msg_type']}" in line
        assert line.endswith(record["raw"])

    # --- engine log ------------------------------------------------------
    engine = read_engine_log(transport)
    assert "Connection accepted from" in engine
    assert "logon accepted" in engine
    assert "State AWAITING_LOGON -> ACTIVE" in engine
    assert "Disconnecting: Logout requested by counterparty" in engine
    # A counterparty-initiated logout never passes through LOGOUT_SENT.
    assert "State ACTIVE -> DISCONNECTED" in engine
    assert "LOGOUT_SENT" not in engine
    assert "Connection closed" in engine


async def test_second_connection_is_refused_while_active(acceptor):
    transport = acceptor
    first = FixTestClient()
    await first.connect("127.0.0.1", transport.port)
    await first.logon(HEARTBEAT)
    assert (await first.recv()).msg_type == "A"

    second = FixTestClient()
    await second.connect("127.0.0.1", transport.port)
    assert await second.wait_closed(timeout=5.0)
    assert second.received == []
    await second.close()

    # The first session is untouched: it still answers.
    await first.send("1", [(112, "STILL-HERE")])
    heartbeat = await first.recv()
    assert heartbeat.msg_type == "0"
    assert heartbeat.get(112) == "STILL-HERE"

    await first.close()
    await transport.wait_for_session_end(timeout=5.0)
    assert "Refused second connection" in read_engine_log(transport)


async def test_bad_checksum_frame_is_discarded_and_logged(acceptor):
    transport = acceptor
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    await client.logon(HEARTBEAT)
    assert (await client.recv()).msg_type == "A"

    good = client.codec.encode(
        "1", [(112, "NOPE")],
        sender_comp_id="AGENT", target_comp_id="ORDERECHO",
        seq_num=2, sending_time=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
    )
    await client.send_raw(good[:-4] + b"000\x01")

    # Nothing comes back, and the session stays up.
    assert await client.recv(timeout=0.5) is None
    await client.send("1", [(112, "OK")], seq=2)
    heartbeat = await client.recv()
    assert heartbeat.get(112) == "OK"

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    records = read_evidence(transport)
    discards = [r for r in records if r["kind"] == "discarded"]
    assert len(discards) == 1
    assert "CheckSum" in discards[0]["detail"]
    assert discards[0]["seq"] is None
    assert discards[0]["msg_type"] is None

    disc_lines = [line for line in read_fix_log(transport) if " DISC " in line]
    assert len(disc_lines) == 1
    assert "# Bad CheckSum" in disc_lines[0]


async def test_application_message_gets_business_reject_over_the_wire(acceptor):
    transport = acceptor
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    await client.logon(HEARTBEAT)
    assert (await client.recv()).msg_type == "A"

    await client.send("D", [(11, "ORD-1"), (55, "AAPL"), (54, "1"), (38, "100")])
    reject = await client.recv()
    assert reject.msg_type == "j"
    assert reject.get(45) == "2"
    assert reject.get(372) == "D"
    assert reject.get(380) == "3"
    assert reject.get(58) == "Not supported in this build"

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_wrong_seq_number_triggers_resend_request(acceptor):
    transport = acceptor
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    await client.logon(HEARTBEAT)
    assert (await client.recv()).msg_type == "A"

    await client.send("1", [(112, "GAPPY")], seq=9)
    resend = await client.recv()
    assert resend.msg_type == "2"
    assert resend.get(7) == "2"
    assert resend.get(16) == "0"

    # Fill the gap; the session picks up at 10.
    await client.send("4", [(123, "Y"), (36, "10")], seq=2, poss_dup=True,
                      orig_sending_time="20260101-00:00:00.000")
    await client.send("1", [(112, "BACK")], seq=10)
    heartbeat = await client.recv()
    assert heartbeat.get(112) == "BACK"

    await client.close()
    await transport.wait_for_session_end(timeout=5.0)


async def test_first_message_not_logon_is_disconnected(acceptor):
    transport = acceptor
    client = FixTestClient()
    await client.connect("127.0.0.1", transport.port)
    await client.send("1", [(112, "TOO-EARLY")])
    assert await client.wait_closed(timeout=5.0)
    assert client.received == []
    await client.close()
    await transport.wait_for_session_end(timeout=5.0)
    assert "First message not Logon" in read_engine_log(transport)
