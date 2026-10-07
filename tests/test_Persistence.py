"""Open orders across restarts (Cook 9 spec 5): the store, the book's
restore, POST /admin/restart, and a real process restart."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import yaml

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import FakeClock, SystemClock
from orderecho_Codec import Codec
from orderecho_Config import OrdersConfig
from orderecho_ControlApi import ControlApiServer
from orderecho_FixVersion import FIX_4_2, profile_for
from orderecho_OrderBook import OrderBook
from orderecho_OrderStore import OrderStore
from orderecho_Pricing import PriceQuote
from orderecho_Rules import load_rules
from orderecho_Session import AppSend, RequestPrice
from orderecho_Transport import Transport

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = datetime(2026, 10, 1, 14, 0, 0, tzinfo=timezone.utc)
QUOTE = PriceQuote(Decimal("227.50"), "static:config")
RULES = [
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave"},
    {"name": "default", "match": {"any": True}, "behavior": "full_fill"},
]
ALL_TIFS = ["day", "gtc", "ioc", "fok", "gtx", "gtd"]


# ------------------------------------------------------------- the store

def test_store_keeps_the_latest_open_record_and_compacts(tmp_path):
    store = OrderStore(str(tmp_path / "orders"), "s1", compact_every=0)
    assert not (tmp_path / "orders").exists()        # nothing until a save
    store.upsert({"order_id": "O-1", "n": 1})
    store.upsert({"order_id": "O-2", "n": 1})
    store.upsert({"order_id": "O-1", "n": 2})
    store.close_order("O-2")
    store.close_order("O-9")                         # never open: no line
    lines = (tmp_path / "orders" / "s1.jsonl").read_text().splitlines()
    assert len(lines) == 4

    again = OrderStore(str(tmp_path / "orders"), "s1")
    assert again.load() == [{"order_id": "O-1", "n": 2}]
    # load() compacted the file down to the one open order.
    assert len((tmp_path / "orders" / "s1.jsonl").read_text().splitlines()) == 1


def test_store_survives_a_torn_last_line(tmp_path):
    store = OrderStore(str(tmp_path), "s1")
    store.upsert({"order_id": "O-1"})
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write('{"v":1,"op":"upsert","order":{"order_id":"O-')
    assert [r["order_id"] for r in OrderStore(str(tmp_path), "s1").load()] == ["O-1"]


def test_store_compacts_every_n_appends(tmp_path):
    store = OrderStore(str(tmp_path), "s1", compact_every=3)
    for n in range(7):
        store.upsert({"order_id": "O-1", "n": n})
    assert len(open(store.path).read().splitlines()) <= 3


# ----------------------------------------------------- book: save/restore

_seq = [1]


def new_order(cl_ord_id, symbol, tif=None, qty="1000"):
    _seq[0] += 1
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
              (60, "20261001-14:00:00.000"), (38, qty), (40, "2"),
              (44, "10.00")]
    if tif:
        fields.append((59, tif))
    codec = Codec(FIX_4_2)
    raw = codec.encode("D", fields, sender_comp_id="AGENT",
                       target_comp_id="ORDERECHO", seq_num=_seq[0],
                       sending_time=START)
    return codec.decode(raw)[0]


def book_on(store_dir, clock, **orders):
    return OrderBook(
        OrdersConfig(persist=True, time_in_force=ALL_TIFS, **orders),
        load_rules(RULES, 1000), clock, "P", profile=profile_for(FIX_4_2),
        order_store=OrderStore(store_dir, "s1"))


def reports(actions):
    return [dict(a.body_fields) for a in actions
            if isinstance(a, AppSend) and a.msg_type == "8"]


def test_open_orders_come_back_with_chains_and_resumed_schedules(tmp_path):
    clock = FakeClock(START)
    book = book_on(str(tmp_path), clock)
    book.on_app_message(new_order("G1", "ZWZZT", tif="1"), QUOTE)   # GTC hold
    book.on_app_message(new_order("E1", "EFG"), QUOTE)              # partials
    book.on_app_message(new_order("A1", "AAPL"), QUOTE)
    clock.advance(1.0)
    book.on_timer()                       # E1 40% fill, A1 filled (closed)
    gtc = book.orders()[0]
    book.on_app_message(_replace("G1-R", "G1"), QUOTE)
    assert gtc.cl_ord_id_chain == ["G1", "G1-R"]

    # "Restart" an hour later: a new book, the same store.
    clock.advance(3600)
    reborn = book_on(str(tmp_path), clock)
    evidence = reborn.restore(OrderStore(str(tmp_path), "s1").load())
    restored = {order.cl_ord_id: order for order in reborn.orders()}
    assert set(restored) == {"G1-R", "E1"}           # A1 was closed
    assert [e.event for e in evidence] == ["order restored"] * 2
    assert restored["G1-R"].cl_ord_id_chain == ["G1", "G1-R"]
    assert restored["G1-R"].order_id == gtc.order_id
    assert restored["G1-R"].time_in_force == "1"
    assert restored["E1"].cum_qty == 400 and restored["E1"].leaves_qty == 600

    # E1's second fill was due 1s after the first: it is rescheduled 1s
    # after the reload, not lost and not fired at once.
    assert reports(reborn.on_timer()) == []
    clock.advance(1.0)
    fills = reports(reborn.on_timer())
    assert [(f[32], f[14]) for f in fills] == [("100", "500")]

    # A ClOrdID used before the restart is still a duplicate after it.
    dup = reports(reborn.on_app_message(new_order("G1", "ZWZZT"), QUOTE))
    assert dup[0][103] == "6"


def _replace(cl_ord_id, orig):
    _seq[0] += 1
    codec = Codec(FIX_4_2)
    raw = codec.encode("G", [(11, cl_ord_id), (41, orig), (21, "1"),
                             (55, "ZWZZT"), (54, "1"),
                             (60, "20261001-14:00:00.000"), (38, "800"),
                             (40, "2"), (44, "10.50")],
                       sender_comp_id="AGENT", target_comp_id="ORDERECHO",
                       seq_num=_seq[0], sending_time=START)
    return codec.decode(raw)[0]


def test_day_orders_from_an_earlier_date_expire_on_the_next_logon(tmp_path):
    clock = FakeClock(START)
    book = book_on(str(tmp_path), clock)
    book.on_app_message(new_order("D1", "ZWZZT"), QUOTE)           # Day
    book.on_app_message(new_order("G1", "ZWZZT", tif="1"), QUOTE)  # GTC
    clock.advance(timedelta(days=1).total_seconds())
    reborn = book_on(str(tmp_path), clock)
    reborn.restore(OrderStore(str(tmp_path), "s1").load())
    assert all(not order.closed for order in reborn.orders())      # not yet
    out = reports(reborn.on_session_logon())
    assert [(f[11], f[150], f[39], f[151]) for f in out] == [
        ("D1", "C", "C", "0")]
    assert "expired after restart" in out[0][58]
    # Only once, and the GTC order lives on.
    assert reports(reborn.on_session_logon()) == []
    assert [o.cl_ord_id for o in reborn.orders() if not o.closed] == ["G1"]


def test_a_restored_market_order_asks_for_its_price_again(tmp_path):
    clock = FakeClock(START)
    book = book_on(str(tmp_path), clock)
    _seq[0] += 1
    codec = Codec(FIX_4_2)
    raw = codec.encode("D", [(11, "M1"), (21, "1"), (55, "ZWZZT"), (54, "1"),
                             (60, "20261001-14:00:00.000"), (38, "10"),
                             (40, "1"), (59, "1")],
                       sender_comp_id="AGENT", target_comp_id="ORDERECHO",
                       seq_num=_seq[0], sending_time=START)
    book.on_app_message(codec.decode(raw)[0], None)
    reborn = book_on(str(tmp_path), clock)
    reborn.restore(OrderStore(str(tmp_path), "s1").load())
    asks = [a for a in reborn.on_session_logon() if isinstance(a, RequestPrice)]
    assert [a.symbol for a in asks] == ["ZWZZT"]


def test_nothing_is_written_when_persist_is_off(tmp_path):
    clock = FakeClock(START)
    book = OrderBook(OrdersConfig(), load_rules(RULES, 1000), clock, "P")
    book.on_app_message(new_order("N1", "ZWZZT"), QUOTE)
    assert book.order_store is None
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------- POST /admin/restart

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def answer_logout(fix):
    logout = await fix.recv_until("5", timeout=10)
    assert logout is not None
    await fix.send("5", [(58, "bye")])
    return logout


async def test_admin_restart_keeps_open_orders_and_their_schedules(tmp_path):
    rules = [
        {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
        {"name": "partial", "match": {"first_letter": "E-G"},
         "behavior": "partial_fill", "fills": ["40%", "10%"],
         "then": "leave", "delay_ms": 1500},
        {"name": "default", "match": {"any": True}, "behavior": "full_fill"},
    ]
    config = isolated_config(
        tmp_path, fix_port=free_port(), api_port=free_port(), rules=rules,
        control_api_enabled=True,
        orders=OrdersConfig(default_delay_ms=50, persist=True,
                            time_in_force=ALL_TIFS))
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    api = ControlApiServer(config, transport)
    await api.start()
    http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{api.bound_port}",
                             timeout=20.0)
    port = transport.port
    try:
        fix = FixTestClient()
        await fix.connect("127.0.0.1", port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        await fix.send("D", [(11, "GTC-1"), (21, "1"), (55, "ZWZZT"),
                             (54, "1"), (38, "100"), (40, "2"),
                             (44, "10.00"), (59, "1"),
                             (60, "20261001-14:00:00.000")])
        gtc_id = (await fix.recv_until("8")).get(37)
        await fix.send("D", [(11, "EFG-1"), (21, "1"), (55, "EFG"), (54, "1"),
                             (38, "1000"), (40, "2"), (44, "10.00"),
                             (60, "20261001-14:00:00.000")])
        efg_id = (await fix.recv_until("8")).get(37)

        restart = asyncio.create_task(http.post("/admin/restart"))
        await answer_logout(fix)
        response = await restart
        assert response.status_code == 200
        body = response.json()
        assert body["restarted"] is True and body["ports"] == [port]
        assert body["open_orders"] == {"ORDERECHO-AGENT": 2}
        await fix.close()

        # The same port again, and the orders are still there.
        fix = FixTestClient()
        await fix.connect("127.0.0.1", port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        orders = (await http.get("/orders?status=open")).json()["orders"]
        assert {o["order_id"] for o in orders} == {gtc_id, efg_id}

        # EFG's scheduled fills resume after the restart.
        fill = await fix.recv_until("8", timeout=5)
        assert (fill.get(37), fill.get(32), fill.get(39)) == (efg_id, "400", "1")

        # The GTC order is live: a cancel by its original ClOrdID works.
        await fix.send("F", [(11, "GTC-1-C"), (41, "GTC-1"), (55, "ZWZZT"),
                             (54, "1"), (60, "20261001-14:00:00.000")])
        canceled = await fix.recv_until("8")
        while canceled.get(37) != gtc_id:
            canceled = await fix.recv_until("8")
        assert (canceled.get(150), canceled.get(39), canceled.get(41)) == (
            "4", "4", "GTC-1")
        await fix.close()
    finally:
        await http.aclose()
        await api.stop()
        await transport.stop()
        transport.close_logs()


async def test_admin_restart_with_nobody_connected(tmp_path):
    config = isolated_config(tmp_path, fix_port=free_port(),
                             api_port=free_port(), rules=RULES,
                             control_api_enabled=True)
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    serving = asyncio.create_task(transport.serve_forever())
    api = ControlApiServer(config, transport)
    await api.start()
    try:
        async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{api.bound_port}",
                timeout=10.0) as http:
            response = await http.post("/admin/restart")
            assert response.status_code == 200
            assert response.json()["open_orders"] == {"ORDERECHO-AGENT": 0}
            # serve_forever is still serving through the restart.
            assert not serving.done()
            assert (await http.get("/health")).status_code == 200
        fix = FixTestClient()
        await fix.connect("127.0.0.1", transport.port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        await fix.close()
    finally:
        await api.stop()
        await transport.stop()
        await asyncio.wait_for(serving, 5)
        transport.close_logs()


# --------------------------------------------- a real process restart

def write_engine_config(tmp_path, fix_port, api_port) -> str:
    raw = {
        "session": {"fix_version": "FIX.4.2", "sender_comp_id": "ORDERECHO",
                    "target_comp_id": "AGENT", "host": "127.0.0.1",
                    "port": fix_port, "heartbeat_grace_pct": 20,
                    "logout_timeout_sec": 5},
        "storage": {k: str(tmp_path / "data" / k.split("_")[0]) for k in
                    ("seqnum_dir", "evidence_dir", "msgstore_dir",
                     "orders_dir")},
        "logging": {"log_dir": str(tmp_path / "logs"), "fix_delimiter": "|",
                    "engine_level": "INFO", "console": False},
        "control_api": {"enabled": True, "host": "127.0.0.1",
                        "port": api_port},
        "orders": {"default_delay_ms": 50, "persist": True,
                   "status_requests": True, "time_in_force": ALL_TIFS},
        "pricing": {"mode": "static", "static": {"default": 100}},
        "rules": RULES,
    }
    path = tmp_path / "engine.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


async def start_engine(config_path, api_port):
    process = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "orderecho_Main.py"),
         "--config", config_path],
        cwd=os.path.dirname(config_path), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    async with httpx.AsyncClient(timeout=1.0) as http:
        for _ in range(150):
            try:
                if (await http.get(
                        f"http://127.0.0.1:{api_port}/health")).status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    process.kill()
    raise AssertionError("engine did not come up:\n"
                         + process.stdout.read().decode())


async def stop_engine(process):
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


async def test_open_orders_survive_a_process_restart(tmp_path):
    fix_port = free_port()
    api_port = free_port()
    while api_port == fix_port:
        api_port = free_port()
    config_path = write_engine_config(tmp_path, fix_port, api_port)
    engine = await start_engine(config_path, api_port)
    try:
        fix = FixTestClient()
        await fix.connect("127.0.0.1", fix_port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        await fix.send("D", [(11, "PR-GTC"), (21, "1"), (55, "ZWZZT"),
                             (54, "1"), (38, "100"), (40, "2"),
                             (44, "10.00"), (59, "1"),
                             (60, "20261001-14:00:00.000")])
        order_id = (await fix.recv_until("8")).get(37)
        stopping = asyncio.create_task(stop_engine(engine))
        await answer_logout(fix)
        await stopping
        await fix.close()
        assert os.path.exists(tmp_path / "data" / "orders" /
                              "ORDERECHO-AGENT.jsonl")

        engine = await start_engine(config_path, api_port)
        fix = FixTestClient()
        await fix.connect("127.0.0.1", fix_port)
        await fix.logon(30, reset=True)
        assert (await fix.recv()).msg_type == "A"
        await fix.send("H", [(11, "PR-GTC"), (55, "ZWZZT"), (54, "1")])
        status = await fix.recv_until("8")
        assert (status.get(37), status.get(39), status.get(20)) == (
            order_id, "0", "3")
        await fix.send("F", [(11, "PR-GTC-C"), (41, "PR-GTC"), (55, "ZWZZT"),
                             (54, "1"), (60, "20261001-14:00:00.000")])
        canceled = await fix.recv_until("8")
        assert (canceled.get(37), canceled.get(150)) == (order_id, "4")
        await fix.close()
        engine_log = "".join(
            open(os.path.join(tmp_path, "logs", "engine", name)).read()
            for name in os.listdir(tmp_path / "logs" / "engine"))
        assert "Restored 1 open order(s)" in engine_log
    finally:
        await stop_engine(engine)
