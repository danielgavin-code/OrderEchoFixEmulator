"""Control API, driven against a full engine on random free ports."""

import asyncio
import json
import socket
from decimal import Decimal

import httpx
import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import SystemClock
from orderecho_Codec import DiscardedFrame, FixMsg
from orderecho_Config import (
    Config,
    ConfigError,
    is_loopback,
    ControlApiConfig,
    LoggingConfig,
    OrdersConfig,
    PricingConfig,
    SessionConfig,
    StorageConfig,
)
from orderecho_ControlApi import ControlApiServer
from orderecho_Rules import load_rules
from orderecho_Transport import Transport
from orderecho_Version import ORDERECHO_BUILD, ORDERECHO_VERSION

HEARTBEAT = 30
FAST_MS = 50
SLOW_MS = 1000

RULES = [
    # Holds forever: the control API is the only thing that moves it.
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "a-to-d-full", "match": {"first_letter": "A-D"},
     "behavior": "full_fill", "delay_ms": FAST_MS},
    # Slow on purpose, so a test can call /hold before the first fill fires.
    {"name": "e-to-g-partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave",
     "delay_ms": SLOW_MS},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(tmp_path, fix_port, api_port, api_host="127.0.0.1") -> Config:
    return Config(
        session=SessionConfig(
            fix_version="FIX.4.2", sender_comp_id="ORDERECHO",
            target_comp_id="AGENT", host="127.0.0.1", port=fix_port,
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
        orders=OrdersConfig(default_delay_ms=FAST_MS),
        pricing=PricingConfig(mode="static",
                              static={"AAPL": Decimal("227.50")},
                              static_default=Decimal("100.00")),
        control_api=ControlApiConfig(enabled=True, host=api_host, port=api_port),
        rules=load_rules(RULES, FAST_MS),
        path="<test>",
    )


@pytest.fixture
async def engine(tmp_path):
    """The whole engine: FIX acceptor plus control API, on free ports."""
    config = make_config(tmp_path, free_port(), free_port())
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    api = ControlApiServer(config, transport)
    await api.start()
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api.bound_port}", timeout=10.0
    )
    try:
        yield transport, client
    finally:
        await client.aclose()
        await api.stop()
        await transport.stop()
        transport.close_logs()


async def logged_on(transport, continue_seq: bool = False) -> FixTestClient:
    fix = FixTestClient()
    await fix.connect("127.0.0.1", transport.port)
    if continue_seq:
        # Sequence numbers persist across connections, so a fresh client has
        # to pick up where the previous one left off.
        _next_out, next_in = transport.seq_store.load()
        fix.next_out = next_in
    await fix.logon(HEARTBEAT)
    reply = await fix.recv()
    assert reply.msg_type == "A"
    return fix


async def send_order(fix, cl_ord_id, symbol, qty=1000, ord_type="1", price=None):
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
              (38, str(qty)), (40, ord_type), (60, "20260927-00:00:00.000")]
    if price is not None:
        fields.append((44, price))
    await fix.send("D", fields)


async def next_report(fix, timeout=5.0):
    return await fix.recv_until("8", timeout)


async def order_on(fix, transport, symbol="ZWZZT", cl_ord_id="A1", qty=1000):
    """Place an order and return (order_id, ack)."""
    await send_order(fix, cl_ord_id, symbol, qty)
    ack = await next_report(fix)
    assert ack.get(150) == "0"
    return ack.get(37), ack


def read_fix_log(transport):
    with open(transport.fix_log.path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


def read_evidence(transport):
    with open(transport.evidence.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# 1 -------------------------------------------------------------------------

async def test_health_and_status_before_and_after_logon(engine):
    transport, api = engine

    health = await api.get("/health")
    assert health.status_code == 200
    # Cook 5 §3.3: version and build. Cook 6 §7.4 adds the session count.
    assert health.json() == {"ok": True, "version": ORDERECHO_VERSION,
                             "build": ORDERECHO_BUILD, "sessions": 1}

    before = (await api.get("/status")).json()
    assert before["state"] == "DISCONNECTED"
    assert before["peer"] is None
    assert before["heart_bt_int"] is None
    assert before["next_out"] == 1 and before["next_in"] == 1
    assert before["open_orders"] == 0
    assert before["run_id"] == transport.run_id
    assert before["sender_comp_id"] == "ORDERECHO"
    assert before["target_comp_id"] == "AGENT"
    assert before["evidence_path"] == transport.evidence.path

    fix = await logged_on(transport)
    after = (await api.get("/status")).json()
    assert after["state"] == "ACTIVE"
    assert after["peer"].startswith("127.0.0.1:")
    assert after["heart_bt_int"] == HEARTBEAT
    assert after["next_out"] == 2 and after["next_in"] == 2
    assert after["last_received"].endswith("Z")

    await fix.close()


# 2 -------------------------------------------------------------------------

async def test_manual_fill_then_fill_rest_then_closed(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    response = await api.post(f"/orders/{order_id}/fill", json={"qty": 300})
    assert response.status_code == 200
    payload = response.json()
    assert payload["order"]["cum_qty"] == "300"
    assert payload["order"]["leaves_qty"] == "700"
    assert [sent["msg_type"] for sent in payload["sent"]] == ["8"]

    partial = await next_report(fix)
    assert partial.get(150) == "1"
    assert partial.get(39) == "1"
    assert partial.get(32) == "300"
    assert partial.get(14) == "300"
    assert partial.get(151) == "700"

    rest = await api.post(f"/orders/{order_id}/fill-rest")
    assert rest.status_code == 200
    assert rest.json()["order"]["ord_status"] == "2"

    filled = await next_report(fix)
    assert filled.get(150) == "2"
    assert filled.get(39) == "2"
    assert filled.get(32) == "700"
    assert filled.get(151) == "0"

    closed = await api.post(f"/orders/{order_id}/fill", json={"qty": 1})
    assert closed.status_code == 409
    assert closed.json()["error"] == "order_closed"

    await fix.close()


# 3 -------------------------------------------------------------------------

async def test_manual_fills_at_two_prices_average_correctly(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport, qty=1000)

    assert (await api.post(f"/orders/{order_id}/fill",
                           json={"qty": 400, "price": "10.00"})).status_code == 200
    assert (await api.post(f"/orders/{order_id}/fill",
                           json={"qty": 600, "price": "20.00"})).status_code == 200

    first = await next_report(fix)
    assert first.get(31) == "10.00" and first.get(6) == "10.0000"
    second = await next_report(fix)
    # (400 x 10 + 600 x 20) / 1000 = 16.0000
    assert second.get(31) == "20.00"
    assert second.get(6) == "16.0000"
    assert second.get(39) == "2"

    await fix.close()


# 4 -------------------------------------------------------------------------

async def test_hold_stops_scheduled_fills(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport, symbol="EFG",
                                    cl_ord_id="H1", qty=1000)

    held = await api.post(f"/orders/{order_id}/hold")
    assert held.status_code == 200
    assert held.json()["order"]["ord_status"] == "0"

    order = transport.order_book.get_order(order_id)
    assert order.scheduled == []
    assert not order.closed

    # Well past when the first scheduled fill would have been due.
    await asyncio.sleep(SLOW_MS / 1000.0 * 1.5)
    assert await fix.recv(timeout=0.3) is None

    # Still open, so a manual fill still works.
    assert (await api.post(f"/orders/{order_id}/fill",
                           json={"qty": 10})).status_code == 200
    assert (await next_report(fix)).get(32) == "10"

    await fix.close()


# 5 -------------------------------------------------------------------------

async def test_manual_cancel(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    response = await api.post(f"/orders/{order_id}/cancel",
                              json={"text": "enough of that"})
    assert response.status_code == 200
    assert response.json()["order"]["ord_status"] == "4"

    canceled = await next_report(fix)
    assert canceled.get(150) == "4"
    assert canceled.get(39) == "4"
    assert canceled.get(151) == "0"
    assert canceled.get(41) is None          # unsolicited
    assert canceled.get(58) == "enough of that"

    await fix.close()


async def test_manual_cancel_default_text(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)
    await api.post(f"/orders/{order_id}/cancel")
    assert (await next_report(fix)).get(58) == "Canceled via control API"
    await fix.close()


# 6 -------------------------------------------------------------------------

async def test_order_action_errors(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport, qty=100)

    too_big = await api.post(f"/orders/{order_id}/fill", json={"qty": 101})
    assert too_big.status_code == 400
    assert too_big.json()["error"] == "invalid_request"
    assert "exceeds LeavesQty" in too_big.json()["detail"]

    for bad_qty in (0, -5):
        response = await api.post(f"/orders/{order_id}/fill",
                                  json={"qty": bad_qty})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    unknown = await api.post("/orders/O-nope-9/fill", json={"qty": 1})
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "not_found"

    bad_price = await api.post(f"/orders/{order_id}/fill",
                               json={"qty": 1, "price": "0"})
    assert bad_price.status_code == 400

    # Not even valid JSON for the model.
    malformed = await api.post(f"/orders/{order_id}/fill",
                               json={"qty": "many"})
    assert malformed.status_code == 400
    assert malformed.json()["error"] == "invalid_request"

    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    logged_out = await api.post(f"/orders/{order_id}/fill", json={"qty": 1})
    assert logged_out.status_code == 409
    assert logged_out.json()["error"] == "session_not_active"


# 7 -------------------------------------------------------------------------

async def test_inject_set_tag_on_next_execution_report(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    queued = await api.post("/inject/next",
                            json={"msg_type": "8", "set": {"9999": "FOO"}})
    assert queued.status_code == 200
    assert queued.json()["queued"]["remaining"] == 1
    assert (await api.get("/inject")).json()["pending"][0]["msg_type"] == "8"

    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    mutated = await next_report(fix)
    assert isinstance(mutated, FixMsg)           # framing still valid
    assert mutated.get(9999) == "FOO"
    assert mutated.get(32) == "100"

    # The mutation was one-shot: the next report is clean.
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    clean = await next_report(fix)
    assert clean.get(9999) is None
    assert (await api.get("/inject")).json()["pending"] == []

    out_records = [r for r in read_evidence(transport)
                   if r["kind"] == "out" and r["msg_type"] == "8"]
    assert out_records[-2]["injected"] is True
    assert "9999=FOO" in out_records[-2]["detail"]
    assert out_records[-1]["injected"] is False

    injected_lines = [line for line in read_fix_log(transport)
                      if "# injected:" in line]
    assert len(injected_lines) == 1
    assert "9999=FOO" in injected_lines[0]

    await fix.close()


async def test_inject_applies_to_any_msg_type_when_unset(engine):
    transport, api = engine
    fix = await logged_on(transport)

    await api.post("/inject/next", json={"set": {"58": "meddled"}})
    await api.post("/session/test-request")
    test_request = await fix.recv_until("1")
    assert test_request.get(58) == "meddled"

    await fix.close()


async def test_inject_count_applies_more_than_once(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    await api.post("/inject/next",
                   json={"msg_type": "8", "set": {"9999": "X"}, "count": 2})
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    assert (await next_report(fix)).get(9999) == "X"
    assert (await next_report(fix)).get(9999) == "X"
    assert (await api.get("/inject")).json()["pending"] == []

    await fix.close()


async def test_injections_can_be_listed_and_cleared(engine):
    transport, api = engine
    fix = await logged_on(transport)

    await api.post("/inject/next", json={"msg_type": "8", "set": {"58": "a"}})
    await api.post("/inject/next", json={"msg_type": "0", "remove": ["58"]})
    listed = (await api.get("/inject")).json()["pending"]
    assert len(listed) == 2
    assert listed[0]["set"] == {"58": "a"}
    assert listed[1]["remove"] == [58]

    cleared = await api.delete("/inject")
    assert cleared.json() == {"cleared": 2}
    assert (await api.get("/inject")).json()["pending"] == []

    await fix.close()


# 8 -------------------------------------------------------------------------

async def test_inject_corrupt_checksum_is_discarded_by_the_client(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    await api.post("/inject/next",
                   json={"msg_type": "8", "corrupt_checksum": True})
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})

    item = await fix.recv()
    assert isinstance(item, DiscardedFrame)
    assert "CheckSum" in item.reason

    await fix.close()


# 9 -------------------------------------------------------------------------

async def test_inject_remove_tag(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)

    await api.post("/inject/next", json={"msg_type": "8", "remove": ["60"]})
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})

    stripped = await next_report(fix)
    assert stripped.get(60) is None
    assert stripped.get(32) == "100"          # everything else intact

    await fix.close()


# 10 ------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"set": {"10": "999"}},
    {"set": {"9": "50"}},
    {"set": {"8": "FIX.4.4"}},
    {"remove": ["10"]},
    {"remove": ["9"]},
])
async def test_framing_tags_cannot_be_injected(engine, payload):
    transport, api = engine
    fix = await logged_on(transport)

    response = await api.post("/inject/next", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "framing" in response.json()["detail"]

    await fix.close()


# 11 ------------------------------------------------------------------------

async def test_inject_seq_gap_then_resend_request_is_gap_filled(engine):
    transport, api = engine
    fix = await logged_on(transport)
    assert transport.session.next_out == 2

    gap = await api.post("/inject/seq-gap", json={"skip": 3})
    assert gap.status_code == 200
    assert gap.json() == {"skipped": 3, "next_out": 5}

    # The next real message lands on 5, so the client sees 2, 3, 4 missing.
    await api.post("/session/test-request")
    test_request = await fix.recv_until("1")
    assert test_request.seq_num == 5

    await fix.send("2", [(7, "2"), (16, "0")])
    gap_fill = await fix.recv_until("4")
    assert gap_fill.seq_num == 2                 # starts at BeginSeqNo
    assert gap_fill.get(123) == "Y"
    assert gap_fill.get(43) == "Y"
    assert gap_fill.get(122) is not None
    assert gap_fill.get(36) == "6"               # our next outbound

    await fix.close()


async def test_inject_seq_gap_rejects_bad_skip(engine):
    transport, api = engine
    fix = await logged_on(transport)
    response = await api.post("/inject/seq-gap", json={"skip": 0})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    await fix.close()


# 12 ------------------------------------------------------------------------

async def test_inject_duplicate_last_with_poss_dup(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, ack = await order_on(fix, transport)
    original_seq = ack.seq_num
    original_sending_time = ack.get(52)

    response = await api.post("/inject/duplicate-last",
                              json={"poss_dup": True})
    assert response.status_code == 200
    assert response.json()["sent"][0]["injected"] is True

    duplicate = await next_report(fix)
    assert duplicate.seq_num == original_seq
    assert duplicate.get(43) == "Y"
    assert duplicate.get(122) == original_sending_time
    assert duplicate.get(37) == order_id
    assert transport.session.next_out == original_seq + 1   # nothing consumed

    await fix.close()


async def test_inject_duplicate_last_without_poss_dup(engine):
    transport, api = engine
    fix = await logged_on(transport)
    _order_id, ack = await order_on(fix, transport)

    await api.post("/inject/duplicate-last")
    duplicate = await next_report(fix)
    assert duplicate.seq_num == ack.seq_num
    assert duplicate.get(43) is None
    assert duplicate.get(122) is None

    await fix.close()


# 13 ------------------------------------------------------------------------

async def test_session_test_request(engine):
    transport, api = engine
    fix = await logged_on(transport)

    response = await api.post("/session/test-request")
    assert response.status_code == 200
    test_req_id = response.json()["test_req_id"]
    assert test_req_id == "TEST-1"

    arrived = await fix.recv_until("1")
    assert arrived.get(112) == "TEST-1"
    assert transport.session.pending_test_req_id == "TEST-1"

    await fix.close()


async def test_session_logout(engine):
    transport, api = engine
    fix = await logged_on(transport)

    response = await api.post("/session/logout", json={"text": "time to go"})
    assert response.status_code == 200
    assert response.json()["state"] == "LOGOUT_SENT"

    logout = await fix.recv_until("5")
    assert logout.get(58) == "time to go"

    await fix.close()


async def test_session_disconnect_drops_the_socket_without_logout(engine):
    transport, api = engine
    fix = await logged_on(transport)

    response = await api.post("/session/disconnect")
    assert response.status_code == 200

    assert await fix.wait_closed(timeout=5.0)
    assert not any(isinstance(m, FixMsg) and m.msg_type == "5"
                   for m in fix.received)
    await transport.wait_for_session_end(timeout=5.0)

    injected = [r for r in read_evidence(transport)
                if r["kind"] == "event" and r["injected"]]
    assert any("injected disconnect" in r["detail"] for r in injected)

    await fix.close()


async def test_session_endpoints_need_an_active_session(engine):
    _transport, api = engine
    for path in ("/session/test-request", "/session/logout",
                 "/session/disconnect"):
        response = await api.post(path)
        assert response.status_code == 409
        assert response.json()["error"] == "session_not_active"


# 14 ------------------------------------------------------------------------

async def test_reset_seqnums_only_while_disconnected(engine):
    transport, api = engine
    fix = await logged_on(transport)
    await order_on(fix, transport)

    refused = await api.post("/session/reset-seqnums")
    assert refused.status_code == 409
    assert refused.json()["error"] == "conflict"

    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    allowed = await api.post("/session/reset-seqnums")
    assert allowed.status_code == 200
    # Spec 3.4: the response says what it archived (a path, or null).
    body = allowed.json()
    assert body["next_out"] == 1 and body["next_in"] == 1
    assert "archived_store" in body
    assert body["archived_store"] is None or "msgstore" in body["archived_store"]

    with open(transport.seq_store.path, encoding="utf-8") as handle:
        assert json.load(handle) == {"next_out": 1, "next_in": 1}


# 15 ------------------------------------------------------------------------

async def test_messages_ring_buffer(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)
    await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    await next_report(fix)

    everything = (await api.get("/messages?limit=100")).json()["messages"]
    kinds = [row["kind"] for row in everything]
    types = [row["msg_type"] for row in everything]
    assert types == ["A", "A", "D", "8", "8"]
    assert kinds == ["in", "out", "in", "out", "out"]
    assert all(row["raw"].startswith("8=FIX.4.2|") for row in everything)
    assert all(row["ts"].endswith("Z") for row in everything)
    assert all(row["injected"] is False for row in everything)

    inbound = (await api.get("/messages?direction=in")).json()["messages"]
    assert [row["msg_type"] for row in inbound] == ["A", "D"]
    outbound = (await api.get("/messages?direction=out")).json()["messages"]
    assert [row["msg_type"] for row in outbound] == ["A", "8", "8"]

    # limit keeps the newest, in order.
    last_two = (await api.get("/messages?limit=2")).json()["messages"]
    assert [row["msg_type"] for row in last_two] == ["8", "8"]
    assert last_two == everything[-2:]

    bad = await api.get("/messages?direction=sideways")
    assert bad.status_code == 400

    await fix.close()


async def test_orders_endpoints(engine):
    transport, api = engine
    fix = await logged_on(transport)
    open_id, _ack = await order_on(fix, transport, cl_ord_id="O1")
    closed_id, _ack2 = await order_on(fix, transport, cl_ord_id="O2")
    await api.post(f"/orders/{closed_id}/cancel")
    await next_report(fix)

    every = (await api.get("/orders")).json()["orders"]
    assert {o["order_id"] for o in every} == {open_id, closed_id}

    still_open = (await api.get("/orders?status=open")).json()["orders"]
    assert [o["order_id"] for o in still_open] == [open_id]

    done = (await api.get("/orders?status=closed")).json()["orders"]
    assert [o["order_id"] for o in done] == [closed_id]

    detail = (await api.get(f"/orders/{open_id}")).json()
    assert detail["order"]["order_id"] == open_id
    assert detail["cl_ord_id_chain"] == ["O1"]
    assert len(detail["execution_reports"]) == 1
    assert detail["execution_reports"][0]["msg_type"] == "8"

    missing = await api.get("/orders/O-nope-1")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    await fix.close()


# 16 ------------------------------------------------------------------------

async def test_price_endpoint_in_static_mode(engine):
    _transport, api = engine
    listed = (await api.get("/price/AAPL")).json()
    assert listed == {"symbol": "AAPL", "price": "227.50",
                      "source": "static:config"}

    default = (await api.get("/price/WHATEVER")).json()
    assert default["price"] == "100.00"
    assert default["source"] == "static:config"


async def test_rules_endpoint_lists_rules_in_match_order(engine):
    _transport, api = engine
    rules = (await api.get("/rules")).json()["rules"]
    assert [rule["name"] for rule in rules] == [
        "hold", "a-to-d-full", "e-to-g-partial", "default"
    ]
    assert rules[0]["match"] == {"symbol": "ZWZZT"}
    assert rules[1]["match"] == {"first_letter": "A-D"}
    assert rules[2]["fills"] == ["40%", "10%"]
    assert rules[2]["then"] == "leave"
    assert rules[2]["delay_ms"] == SLOW_MS
    assert rules[3]["match"] == {"any": True}


# 17 ------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost",
                                  "LOCALHOST", " 127.0.0.1 "])
def test_loopback_hosts_are_accepted(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "10.0.0.1",
                                  "example.com", "", None, "::"])
def test_non_loopback_hosts_are_rejected(host):
    assert is_loopback(host) is False


def test_config_file_with_a_non_loopback_control_api_host_fails(tmp_path):
    from orderecho_Config import load_config
    from test_DemoClient import CONFIG_TEMPLATE

    body = CONFIG_TEMPLATE.format(port=9999, tmp=tmp_path) + """
control_api:
  enabled: true
  host: 0.0.0.0
  port: 8090
"""
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(str(path))
    assert "loopback" in str(caught.value)
    assert "0.0.0.0" in str(caught.value)


async def test_control_api_server_rejects_non_loopback_at_construction(tmp_path):
    """Even if a Config is built by hand, the server refuses to bind."""
    config = make_config(tmp_path, free_port(), free_port())
    config.control_api.host = "192.168.1.50"
    transport = Transport(config, clock=SystemClock())
    try:
        with pytest.raises(ConfigError, match="loopback"):
            ControlApiServer(config, transport)
    finally:
        transport.close_logs()


# engine log ----------------------------------------------------------------

async def test_api_calls_are_written_to_the_engine_log(engine):
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)
    await api.get("/health")
    await api.post(f"/orders/{order_id}/fill", json={"qty": 300})
    await api.post(f"/orders/{order_id}/fill", json={"qty": 99999})   # a 400

    with open(transport.engine_log.path, encoding="utf-8") as handle:
        text = handle.read()
    assert f"API   POST /orders/{order_id}/fill qty=300 -> 200" in text
    assert "API   GET /health -> 200" in text
    assert f"API   POST /orders/{order_id}/fill qty=99999 -> 400" in text
    # The label column lines up with the order lifecycle lines.
    assert "ORDER O-" in text

    await fix.close()


# ===========================================================================
# Cook 4 fixes (spec 3) and instant acks (spec 4)
# ===========================================================================

async def test_pending_injections_are_dropped_on_disconnect(engine):
    """Spec 3.1: mischief never leaks into the next session."""
    transport, api = engine
    fix = await logged_on(transport)

    await api.post("/inject/next",
                   json={"msg_type": "8", "set": {"9999": "LEAK"}})
    assert len((await api.get("/inject")).json()["pending"]) == 1

    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    assert (await api.get("/inject")).json()["pending"] == []

    dropped = [r for r in read_evidence(transport)
               if r["kind"] == "event"
               and "pending injections dropped on disconnect" in r["detail"]]
    assert len(dropped) == 1
    assert "9999=LEAK" in dropped[0]["detail"]
    with open(transport.engine_log.path, encoding="utf-8") as handle:
        assert "pending injections dropped on disconnect" in handle.read()

    # The next session's first report is clean.
    again = await logged_on(transport, continue_seq=True)
    order_id, _ack = await order_on(again, transport, cl_ord_id="A2")
    await api.post(f"/orders/{order_id}/fill", json={"qty": 10})
    report = await next_report(again)
    assert report.get(9999) is None
    await again.close()


async def test_nothing_is_reported_when_no_injections_were_pending(engine):
    transport, _api = engine
    fix = await logged_on(transport)
    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)
    assert not any("pending injections dropped" in r["detail"]
                   for r in read_evidence(transport)
                   if r["kind"] == "event" and r["detail"])


async def test_order_facts_are_reported_even_when_logged_out(engine):
    """Spec 3.2: unknown is 404 and closed is 409, session or no session."""
    transport, api = engine
    fix = await logged_on(transport)
    order_id, _ack = await order_on(fix, transport)
    await api.post(f"/orders/{order_id}/cancel")
    await next_report(fix)

    open_id, _ack2 = await order_on(fix, transport, cl_ord_id="A3")

    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    closed = await api.post(f"/orders/{order_id}/fill", json={"qty": 1})
    assert closed.status_code == 409
    assert closed.json()["error"] == "order_closed"

    unknown = await api.post("/orders/O-nope-9/fill", json={"qty": 1})
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "not_found"

    bad_body = await api.post(f"/orders/{open_id}/fill", json={"qty": 0})
    assert bad_body.status_code == 400
    assert bad_body.json()["error"] == "invalid_request"

    # A sane request on a live order is where the session finally matters.
    no_session = await api.post(f"/orders/{open_id}/fill", json={"qty": 1})
    assert no_session.status_code == 409
    assert no_session.json()["error"] == "session_not_active"


async def test_manual_fill_while_the_price_is_pending(engine, monkeypatch):
    """Spec 4/10.6: no price while pending is a 409; an explicit price is fine."""
    import orderecho_Transport
    from orderecho_Pricing import PriceQuote

    transport, api = engine
    resolved = asyncio.Event()

    async def slow_resolve(source, symbol, timeout_sec):
        await resolved.wait()
        return PriceQuote(Decimal("227.50"), "live:yfinance")

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", slow_resolve)

    fix = await logged_on(transport)
    await send_order(fix, "PP1", "ZWZZT", 1000)
    ack = await next_report(fix)
    order_id = ack.get(37)
    assert ack.get(150) == "0"

    pending = await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    assert pending.status_code == 409
    assert pending.json()["error"] == "price_pending"
    assert "explicit price" in pending.json()["detail"]

    priced = await api.post(f"/orders/{order_id}/fill",
                            json={"qty": 100, "price": "42.00"})
    assert priced.status_code == 200
    filled = await next_report(fix)
    assert filled.get(32) == "100"
    assert filled.get(31) == "42.00"

    # Once the quote lands, a bare fill works again.
    resolved.set()
    await asyncio.sleep(0.3)
    later = await api.post(f"/orders/{order_id}/fill", json={"qty": 100})
    assert later.status_code == 200
    assert (await next_report(fix)).get(31) == "227.50"

    await fix.close()


async def test_status_shows_a_pending_price(engine, monkeypatch):
    import orderecho_Transport
    from orderecho_Pricing import PriceQuote

    transport, api = engine
    gate = asyncio.Event()

    async def slow_resolve(source, symbol, timeout_sec):
        await gate.wait()
        return PriceQuote(Decimal("1.00"), "live:yfinance")

    monkeypatch.setattr(orderecho_Transport, "resolve_quote", slow_resolve)

    fix = await logged_on(transport)
    await send_order(fix, "PP2", "ZWZZT", 10)
    await next_report(fix)

    orders = (await api.get("/orders?status=open")).json()["orders"]
    assert orders[0]["price_source"] == "pending"

    gate.set()
    await asyncio.sleep(0.3)
    orders = (await api.get("/orders?status=open")).json()["orders"]
    assert orders[0]["price_source"] == "live:yfinance"

    await fix.close()


async def test_rules_endpoint_reports_the_price_band(engine):
    _transport, api = engine
    body = (await api.get("/rules")).json()
    assert "price_band" in body
    assert body["price_band"]["enabled"] is False
    assert body["price_band"]["mode"] == "aggressive"
    assert "price_band_pct" in body["rules"][0]
