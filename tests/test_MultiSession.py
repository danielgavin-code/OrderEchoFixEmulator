"""Several FIX sessions on one engine (spec 4-7)."""

import asyncio
import json
import os
import socket
from decimal import Decimal

import httpx
import pytest

from fix_TestClient import FixTestClient
from isolation import isolated_config, isolated_multi_config
from orderecho_Clock import SystemClock
from orderecho_Codec import FixMsg
from orderecho_Config import ConfigError, PriceBandConfig, PricingConfig, load_config
from orderecho_ControlApi import ControlApiServer
from orderecho_FixVersion import FIX_4_2, FIX_4_4
from orderecho_Transport import Transport

HEARTBEAT = 30
REJECT_ALL = [{"name": "reject-all", "match": {"any": True},
               "behavior": "reject", "reject_code": 0,
               "text": "Strict broker rejects everything"}]
FILL_ALL = [{"name": "default", "match": {"any": True},
             "behavior": "full_fill"}]
HOLD_ALL = [{"name": "default", "match": {"any": True},
             "behavior": "ack_only"}]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def static_pricing():
    return PricingConfig(mode="static", static={"AAPL": Decimal("227.50")},
                         static_default=Decimal("100.00"), warm_symbols=[])


class Engine:
    def __init__(self, config):
        self.config = config
        self.transport = None
        self.api = None
        self.http = None

    async def start(self, with_api=False):
        self.transport = Transport(self.config, clock=SystemClock())
        await self.transport.start()
        if with_api:
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


async def logon(transport, session_id, expect="A") -> FixTestClient:
    spec = transport.config.session_by_id(session_id)
    client = FixTestClient(sender_comp_id=spec.target_comp_id,
                           target_comp_id=spec.sender_comp_id,
                           fix_version=spec.fix_version)
    await client.connect("127.0.0.1", spec.port)
    await client.logon(HEARTBEAT)
    if expect is None:
        return client
    reply = await client.recv()
    assert reply is not None and reply.msg_type == expect, reply
    return client


async def send_order(client, cl_ord_id, symbol="AAPL", qty=100,
                     ord_type="1", price=None):
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
              (38, str(qty)), (40, ord_type), (60, "20260101-00:00:00.000")]
    if price is not None:
        fields.append((44, price))
    await client.send("D", fields)


def read_engine_log(transport):
    with open(transport.engine_log.path, encoding="utf-8") as handle:
        return handle.read()


def read_evidence(transport):
    with open(transport.evidence.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_fix_log(runtime):
    with open(runtime.fix_log.path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
async def two_on_one_port(tmp_path):
    """4.2 and 4.4 with the same CompIDs, sharing a port."""
    port = free_port()
    config = isolated_multi_config(tmp_path, [
        {"id": "agent42", "fix_version": FIX_4_2, "port": port,
         "rules": FILL_ALL},
        {"id": "agent44", "fix_version": FIX_4_4, "port": port,
         "rules": FILL_ALL},
    ], default_session="agent42", pricing=static_pricing())
    engine = Engine(config)
    await engine.start()
    try:
        yield engine
    finally:
        await engine.stop()


# 2 -------------------------------------------------------------------------

async def test_two_sessions_share_a_port_and_are_told_apart_by_version(
        two_on_one_port):
    transport = two_on_one_port.transport
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")

    assert a42.received[0].get(8) == FIX_4_2
    assert a44.received[0].get(8) == FIX_4_4
    assert transport.runtime("agent42").session_active
    assert transport.runtime("agent44").session_active

    await send_order(a42, "A1", "AAPL")
    await send_order(a44, "B1", "AAPL")

    fill42 = await a42.recv_until("8")
    fill42b = await a42.recv_until("8")
    fill44 = await a44.recv_until("8")
    fill44b = await a44.recv_until("8")

    assert fill42.get(8) == FIX_4_2 and fill42b.get(150) == "2"
    assert fill44.get(8) == FIX_4_4 and fill44b.get(150) == "F"
    assert fill42b.get(20) == "0"
    assert fill44b.get(20) is None

    # Each session numbers itself.
    assert transport.runtime("agent42").session.next_out == 4
    assert transport.runtime("agent44").session.next_out == 4
    assert transport.runtime("agent42").seq_store.path != \
        transport.runtime("agent44").seq_store.path

    await a42.close()
    await a44.close()
    await transport.wait_for_session_end(timeout=5.0)

    log42 = read_fix_log(transport.runtime("agent42"))
    log44 = read_fix_log(transport.runtime("agent44"))
    assert "|11=A1|" in log42 and "|11=A1|" not in log44
    assert "|11=B1|" in log44 and "|11=B1|" not in log42
    assert f"8={FIX_4_4}|" not in log42
    assert f"8={FIX_4_2}|" not in log44


async def test_each_session_has_its_own_files(two_on_one_port):
    transport = two_on_one_port.transport
    # The FIX log opens on its first write, so give each session one.
    clients = [await logon(transport, "agent42"),
               await logon(transport, "agent44")]
    for client in clients:
        await client.close()
    await transport.wait_for_session_end(timeout=5.0)

    paths = set()
    for session_id in ("agent42", "agent44"):
        runtime = transport.runtime(session_id)
        assert runtime.seq_store.path.endswith(f"{session_id}.json")
        assert runtime.message_store.path.endswith(f"{session_id}.jsonl")
        assert os.path.basename(runtime.fix_log.path).startswith(
            f"{session_id}_"
        )
        paths.add(runtime.fix_log.path)
    assert len(paths) == 2          # and they are different files


async def test_evidence_records_name_their_session(two_on_one_port):
    transport = two_on_one_port.transport
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")
    await send_order(a42, "E1")
    await a42.recv_until("8")
    await a44.close()
    await a42.close()
    await transport.wait_for_session_end(timeout=5.0)

    sessions = {record["session"] for record in read_evidence(transport)}
    assert {"agent42", "agent44"} <= sessions


# 3 -------------------------------------------------------------------------

async def test_unknown_comp_ids_are_dropped_without_a_reply(two_on_one_port):
    transport = two_on_one_port.transport
    port = transport.config.session_by_id("agent42").port
    stranger = FixTestClient(sender_comp_id="NOBODY",
                             target_comp_id="ORDERECHO")
    await stranger.connect("127.0.0.1", port)
    await stranger.logon(HEARTBEAT)

    assert await stranger.wait_closed(timeout=5.0)
    assert stranger.received == []            # not one byte back
    await stranger.close()

    engine = read_engine_log(transport)
    assert "unknown session:" in engine
    assert "49=NOBODY" in engine
    assert any("unknown session" in (record["detail"] or "")
               for record in read_evidence(transport)
               if record["kind"] == "event")


# 4 -------------------------------------------------------------------------

async def test_wrong_begin_string_lists_every_expected_version(two_on_one_port):
    transport = two_on_one_port.transport
    port = transport.config.session_by_id("agent42").port
    # Our CompIDs, but a dialect neither session speaks.
    odd = FixTestClient(fix_version="FIX.4.3")
    await odd.connect("127.0.0.1", port)
    await odd.logon(HEARTBEAT)

    logout = await odd.recv_until("5")
    assert logout is not None
    assert logout.get(58) == (
        f"Incorrect BeginString, expected {FIX_4_2} or {FIX_4_4}"
    )
    assert await odd.wait_closed(timeout=5.0)
    await odd.close()


async def test_wrong_begin_string_with_one_candidate_names_just_it(tmp_path):
    port = free_port()
    config = isolated_multi_config(tmp_path, [
        {"id": "only42", "fix_version": FIX_4_2, "port": port,
         "rules": FILL_ALL},
    ], pricing=static_pricing())
    engine = Engine(config)
    transport = await engine.start()
    try:
        odd = FixTestClient(fix_version=FIX_4_4)
        await odd.connect("127.0.0.1", port)
        await odd.logon(HEARTBEAT)
        logout = await odd.recv_until("5")
        assert logout.get(58) == f"Incorrect BeginString, expected {FIX_4_2}"
        await odd.close()
    finally:
        await engine.stop()


# 5 -------------------------------------------------------------------------

@pytest.fixture
async def three_sessions(tmp_path):
    shared = free_port()
    own = free_port()
    config = isolated_multi_config(tmp_path, [
        {"id": "agent42", "fix_version": FIX_4_2, "port": shared,
         "rules": FILL_ALL},
        {"id": "agent44", "fix_version": FIX_4_4, "port": shared,
         "rules": HOLD_ALL},
        {"id": "strict-broker", "fix_version": FIX_4_2, "sender": "STRICTBRK",
         "port": own, "rules": REJECT_ALL,
         "band": PriceBandConfig(enabled=True, pct=2, mode="aggressive")},
    ], default_session="agent42", control_api_enabled=True,
        api_port=free_port(), pricing=static_pricing())
    engine = Engine(config)
    await engine.start(with_api=True)
    try:
        yield engine
    finally:
        await engine.stop()


async def test_a_session_on_its_own_port_works_alongside_the_shared_one(
        three_sessions):
    transport = three_sessions.transport
    shared = transport.config.session_by_id("agent42").port
    own = transport.config.session_by_id("strict-broker").port
    assert shared != own
    assert sorted(transport.servers) == sorted({shared, own})

    a42 = await logon(transport, "agent42")
    strict = await logon(transport, "strict-broker")
    assert transport.runtime("agent42").session_active
    assert transport.runtime("strict-broker").session_active

    await a42.close()
    await strict.close()


# 6 -------------------------------------------------------------------------

async def test_rules_are_per_session(three_sessions):
    transport = three_sessions.transport
    a42 = await logon(transport, "agent42")
    strict = await logon(transport, "strict-broker")

    await send_order(a42, "R1", "AAPL")
    await send_order(strict, "R2", "AAPL")

    filled = await a42.recv_until("8")
    assert filled.get(150) == "0"              # acked, then filled
    rejected = await strict.recv_until("8")
    assert rejected.get(150) == "8"
    assert rejected.get(58) == "Strict broker rejects everything"

    await a42.close()
    await strict.close()


async def test_band_override_is_per_session(three_sessions):
    transport = three_sessions.transport
    # strict-broker's band is 2%: 227.50 -> 232.05 is the edge.
    strict_spec = transport.config.session_by_id("strict-broker")
    assert strict_spec.price_band.pct == 2
    assert transport.config.session_by_id("agent42").price_band.enabled is False

    strict = await logon(transport, "strict-broker")
    await send_order(strict, "B1", "AAPL", ord_type="2", price="240.00")
    rejected = await strict.recv_until("8")
    assert rejected.get(103) == "3"
    assert "outside 2% band" in rejected.get(58)

    await send_order(strict, "B2", "AAPL", ord_type="2", price="230.00")
    inside = await strict.recv_until("8")
    # Inside the band, so the rule gets its say instead.
    assert inside.get(58) == "Strict broker rejects everything"
    await strict.close()


# 7 -------------------------------------------------------------------------

async def test_injections_are_per_session(three_sessions):
    transport, api = three_sessions.transport, three_sessions.http
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")

    queued = await api.post("/sessions/agent42/inject/next",
                            json={"msg_type": "8", "set": {"9999": "ONLY42"}})
    assert queued.status_code == 200
    assert (await api.get("/sessions/agent44/inject")).json()["pending"] == []

    await send_order(a44, "I1", "ZWZZT")
    clean = await a44.recv_until("8")
    assert clean.get(9999) is None

    await send_order(a42, "I2", "AAPL")
    mutated = await a42.recv_until("8")
    assert mutated.get(9999) == "ONLY42"

    await a42.close()
    await a44.close()


async def test_disconnecting_one_session_drops_only_its_injections(
        three_sessions):
    transport, api = three_sessions.transport, three_sessions.http
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")

    await api.post("/sessions/agent42/inject/next",
                   json={"msg_type": "8", "set": {"1": "A"}})
    await api.post("/sessions/agent44/inject/next",
                   json={"msg_type": "8", "set": {"1": "B"}})

    await a42.close()
    await transport.runtime("agent42").wait_for_session_end(timeout=5.0)

    assert (await api.get("/sessions/agent42/inject")).json()["pending"] == []
    assert len((await api.get("/sessions/agent44/inject")).json()["pending"]) == 1

    await a44.close()


# 8 -------------------------------------------------------------------------

async def test_order_ids_are_unique_across_sessions(three_sessions):
    transport, api = three_sessions.transport, three_sessions.http
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")

    await send_order(a42, "U1", "ZWZZT")
    await send_order(a44, "U2", "ZWZZT")
    ack42 = await a42.recv_until("8")
    ack44 = await a44.recv_until("8")

    assert ack42.get(37) != ack44.get(37)
    assert ack42.get(17) != ack44.get(17)

    listed = (await api.get("/orders")).json()["orders"]
    by_id = {order["order_id"]: order["session"] for order in listed}
    assert by_id[ack42.get(37)] == "agent42"
    assert by_id[ack44.get(37)] == "agent44"

    await a42.close()
    await a44.close()


async def test_order_routes_find_the_owning_session(three_sessions):
    transport, api = three_sessions.transport, three_sessions.http
    a44 = await logon(transport, "agent44")           # not the default session
    await send_order(a44, "O1", "ZWZZT", qty=1000)
    ack = await a44.recv_until("8")
    order_id = ack.get(37)

    detail = (await api.get(f"/orders/{order_id}")).json()
    assert detail["session"] == "agent44"

    filled = await api.post(f"/orders/{order_id}/fill", json={"qty": 400})
    assert filled.status_code == 200
    assert filled.json()["session"] == "agent44"
    report = await a44.recv_until("8")
    assert report.get(32) == "400"
    assert report.get(150) == "F"                     # rendered as 4.4

    await a44.close()
    await transport.runtime("agent44").wait_for_session_end(timeout=5.0)

    # The owning session is the one that has to be live.
    refused = await api.post(f"/orders/{order_id}/fill", json={"qty": 1})
    assert refused.status_code == 409
    assert refused.json()["error"] == "session_not_active"
    assert "agent44" in refused.json()["detail"]


# 9 -------------------------------------------------------------------------

async def test_sessions_routes(three_sessions):
    transport, api = three_sessions.transport, three_sessions.http

    listed = (await api.get("/sessions")).json()["sessions"]
    assert [entry["id"] for entry in listed] == [
        "agent42", "agent44", "strict-broker"
    ]
    by_id = {entry["id"]: entry for entry in listed}
    assert by_id["agent44"]["fix_version"] == FIX_4_4
    assert by_id["strict-broker"]["sender_comp_id"] == "STRICTBRK"
    assert by_id["strict-broker"]["port"] != by_id["agent42"]["port"]
    assert all(entry["state"] == "DISCONNECTED" for entry in listed)

    status = (await api.get("/sessions/agent44/status")).json()
    assert status["id"] == "agent44"
    assert status["fix_version"] == FIX_4_4

    rules = (await api.get("/sessions/strict-broker/rules")).json()
    assert rules["session"] == "strict-broker"
    assert [rule["name"] for rule in rules["rules"]] == ["reject-all"]
    assert rules["price_band"]["pct"] == 2

    assert (await api.get("/sessions/agent42/orders")).json()["orders"] == []
    assert (await api.get("/sessions/agent42/messages")).json()["messages"] == []

    health = (await api.get("/health")).json()
    assert health["sessions"] == 3


@pytest.mark.parametrize("path,method", [
    ("/sessions/nope/status", "get"),
    ("/sessions/nope/orders", "get"),
    ("/sessions/nope/messages", "get"),
    ("/sessions/nope/rules", "get"),
    ("/sessions/nope/inject", "get"),
    ("/sessions/nope/test-request", "post"),
    ("/sessions/nope/logout", "post"),
    ("/sessions/nope/disconnect", "post"),
    ("/sessions/nope/reset-seqnums", "post"),
])
async def test_unknown_session_id_is_404(three_sessions, path, method):
    api = three_sessions.http
    response = await getattr(api, method)(path)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert "agent42" in response.json()["detail"]


async def test_legacy_routes_use_the_default_session(three_sessions):
    transport, api = three_sessions.transport, three_sessions.http
    status = (await api.get("/status")).json()
    assert status["id"] == "agent42"
    rules = (await api.get("/rules")).json()
    assert rules["session"] == "agent42"


async def test_legacy_routes_are_ambiguous_without_a_default(tmp_path):
    port = free_port()
    config = isolated_multi_config(tmp_path, [
        {"id": "one", "fix_version": FIX_4_2, "port": port, "rules": FILL_ALL},
        {"id": "two", "fix_version": FIX_4_4, "port": port, "rules": FILL_ALL},
    ], control_api_enabled=True, api_port=free_port(),
        pricing=static_pricing())
    config.engine.default_session = None
    engine = Engine(config)
    await engine.start(with_api=True)
    try:
        for path in ("/status", "/rules", "/messages"):
            response = await engine.http.get(path)
            assert response.status_code == 409
            assert response.json()["error"] == "ambiguous_session"
            assert "one" in response.json()["detail"]
            assert "two" in response.json()["detail"]
        posted = await engine.http.post("/session/test-request")
        assert posted.status_code == 409
        assert posted.json()["error"] == "ambiguous_session"
    finally:
        await engine.stop()


async def test_legacy_routes_work_with_a_single_session(tmp_path):
    config = isolated_config(tmp_path, fix_port=free_port(),
                             api_port=free_port(), control_api_enabled=True)
    engine = Engine(config)
    await engine.start(with_api=True)
    try:
        status = (await engine.http.get("/status")).json()
        assert status["id"] == "ORDERECHO-AGENT"
        assert (await engine.http.get("/health")).json()["sessions"] == 1
    finally:
        await engine.stop()


# 10 ------------------------------------------------------------------------

MULTI_HEAD = """
storage:
  seqnum_dir: {tmp}/data/seqnums
  evidence_dir: {tmp}/data/evidence
  msgstore_dir: {tmp}/data/msgstore
logging:
  log_dir: {tmp}/logs
  fix_delimiter: "|"
  engine_level: INFO
  console: false
pricing:
  mode: static
  static: {{ default: 100.00 }}
defaults:
  rules:
    - {{ name: default, match: {{ any: true }}, behavior: ack_only }}
"""


def write_multi(tmp_path, body, name="multi.yaml"):
    path = tmp_path / name
    path.write_text(MULTI_HEAD.format(tmp=tmp_path) + body, encoding="utf-8")
    return str(path)


def test_duplicate_session_id_names_the_session(tmp_path):
    path = write_multi(tmp_path, """
sessions:
  - { id: twice, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
  - { id: twice, fix_version: FIX.4.4, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError, match="twice"):
        load_config(path)


def test_duplicate_triple_on_a_port_names_both_sessions(tmp_path):
    path = write_multi(tmp_path, """
sessions:
  - { id: first, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
  - { id: second, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert "second" in str(caught.value)
    assert "first" in str(caught.value)


def test_a_fix_port_may_not_be_the_control_api_port(tmp_path):
    path = write_multi(tmp_path, """
control_api:
  enabled: true
  host: 127.0.0.1
  port: 8099
sessions:
  - { id: clash, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B, port: 8099 }
""")
    with pytest.raises(ConfigError, match="clash"):
        load_config(path)


def test_default_session_must_exist(tmp_path):
    path = write_multi(tmp_path, """
engine:
  default_session: ghost
sessions:
  - { id: real, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError, match="ghost"):
        load_config(path)


def test_session_id_characters_are_restricted(tmp_path):
    path = write_multi(tmp_path, """
sessions:
  - { id: "bad id!", fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError, match="bad id!"):
        load_config(path)


def test_unknown_fix_version_names_the_session(tmp_path):
    path = write_multi(tmp_path, """
sessions:
  - { id: futuristic, fix_version: FIX.5.0, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError, match="futuristic"):
        load_config(path)


def test_session_and_sessions_are_mutually_exclusive(tmp_path):
    path = write_multi(tmp_path, """
session:
  fix_version: FIX.4.2
  sender_comp_id: A
  target_comp_id: B
  host: 127.0.0.1
  port: 1234
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10
sessions:
  - { id: one, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B }
""")
    with pytest.raises(ConfigError, match="not both"):
        load_config(path)


def test_the_shipped_multi_config_loads():
    config = load_config("config/orderecho_multi.yaml")
    assert [spec.id for spec in config.sessions] == [
        "agent42", "agent44", "strict-broker"
    ]
    assert config.engine.default_session == "agent42"
    # orders and price_band merge over the defaults; rules replace them.
    strict = config.session_by_id("strict-broker")
    assert strict.price_band.pct == 2
    assert strict.price_band.mode == "aggressive"        # from defaults
    assert strict.price_band.on_no_reference == "reject"
    assert strict.orders.default_delay_ms == 500         # from defaults
    assert len(strict.rules) == 1
    assert len(config.session_by_id("agent42").rules) == 8


def test_legacy_configs_still_load_as_one_session():
    for path, version in (("config/orderecho.yaml", FIX_4_2),
                          ("config/orderecho_fix44.yaml", FIX_4_4)):
        config = load_config(path)
        assert len(config.sessions) == 1
        spec = config.sessions[0]
        # The legacy file names must not move.
        assert spec.id == "ORDERECHO-AGENT"
        assert spec.fix_version == version
        assert config.default_session_spec is spec


# 11 ------------------------------------------------------------------------

async def test_shutdown_logs_out_every_active_session(three_sessions):
    transport = three_sessions.transport
    a42 = await logon(transport, "agent42")
    a44 = await logon(transport, "agent44")
    strict = await logon(transport, "strict-broker")

    await transport.shutdown_gracefully("engine going down")

    for client in (a42, a44, strict):
        logout = await client.recv_until("5", timeout=5.0)
        assert logout is not None, "a session was never logged out"
        assert logout.get(58) == "engine going down"
        await client.close()

    engine = read_engine_log(transport)
    assert "Initiating logout on 3 session(s)" in engine


# --- --reset-seqnums covers every session --------------------------------

def test_reset_seqnums_resets_every_session(tmp_path, monkeypatch, capsys):
    import orderecho_Main
    from orderecho_MessageStore import MessageStore
    from orderecho_SeqStore import FileSeqStore

    path = write_multi(tmp_path, """
sessions:
  - { id: one, fix_version: FIX.4.2, sender_comp_id: A, target_comp_id: B, port: 1 }
  - { id: two, fix_version: FIX.4.4, sender_comp_id: A, target_comp_id: B, port: 1 }
""")
    config = load_config(path)
    for spec in config.sessions:
        FileSeqStore(config.storage.seqnum_dir, spec.sender_comp_id,
                     spec.target_comp_id, name=spec.id).save(9, 9)
        store = MessageStore(config.storage.msgstore_dir,
                             spec.sender_comp_id, spec.target_comp_id,
                             name=spec.id)
        store.append(1, "8", b"8=FIX.4.2\x0135=8\x0110=001\x01")
        store.close()

    # Stop before the engine actually starts listening.
    async def no_run(_config):
        return 0

    monkeypatch.setattr(orderecho_Main, "run", no_run)
    assert orderecho_Main.main(["--config", path, "--reset-seqnums"]) == 0

    printed = capsys.readouterr().out
    for spec in config.sessions:
        store = FileSeqStore(config.storage.seqnum_dir, spec.sender_comp_id,
                             spec.target_comp_id, name=spec.id)
        assert store.load() == (1, 1), spec.id
        assert f"{spec.id}.json" in printed
        assert MessageStore(config.storage.msgstore_dir, spec.sender_comp_id,
                            spec.target_comp_id, name=spec.id).stored_version() \
            is None
    assert printed.count("archived") == 2
