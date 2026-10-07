"""Lifecycle states, account validation and OrderStatusRequest (Cook 9 spec 4,
5): the pure order book first, then the control API and FIX end to end."""

import socket
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import FakeClock, SystemClock
from orderecho_Codec import Codec
from orderecho_Config import OrdersConfig
from orderecho_ControlApi import ControlApiServer
from orderecho_FixVersion import FIX_4_2, FIX_4_4, profile_for
from orderecho_OrderBook import OrderActionError, OrderBook
from orderecho_Pricing import PriceQuote
from orderecho_Rules import load_rules
from orderecho_Session import AppSend
from orderecho_Transport import Transport

START = datetime(2026, 10, 1, 14, 0, 0, tzinfo=timezone.utc)
QUOTE = PriceQuote(Decimal("227.50"), "static:config")
RULES = [
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "full", "match": {"first_letter": "A-D"}, "behavior": "full_fill"},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]
VERSIONS = [FIX_4_2, FIX_4_4]


def make_book(version=FIX_4_2, **orders):
    clock = FakeClock(START)
    book = OrderBook(OrdersConfig(**orders), load_rules(RULES, 500), clock,
                     "LC", profile=profile_for(version))
    return book, clock


_seq = [1]


def msg(version, msg_type, fields):
    _seq[0] += 1
    codec = Codec(version)
    raw = codec.encode(msg_type, fields, sender_comp_id="AGENT",
                       target_comp_id="ORDERECHO", seq_num=_seq[0],
                       sending_time=START)
    return codec.decode(raw)[0]


def new_order(version, cl_ord_id, symbol="ZWZZT", account=None, qty="100"):
    fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
              (60, "20261001-14:00:00.000"), (38, qty), (40, "2"),
              (44, "10.00")]
    if account is not None:
        fields.insert(0, (1, account))
    return msg(version, "D", fields)


def cancel(version, cl_ord_id, orig):
    return msg(version, "F", [(11, cl_ord_id), (41, orig), (55, "ZWZZT"),
                              (54, "1"), (60, "20261001-14:00:00.000")])


def status_request(version, cl_ord_id, symbol="ZWZZT", extra=()):
    return msg(version, "H", [(11, cl_ord_id), (55, symbol), (54, "1")]
               + list(extra))


def sent(actions, msg_type=None):
    return [(a.msg_type, dict(a.body_fields)) for a in actions
            if isinstance(a, AppSend)
            and (msg_type is None or a.msg_type == msg_type)]


# ------------------------------------------------------- Pending New

@pytest.mark.parametrize("version", VERSIONS)
def test_pending_new_comes_before_the_ack(version):
    book, _clock = make_book(version, send_pending_new=True)
    out = sent(book.on_app_message(new_order(version, "P1"), QUOTE))
    assert [(f[150], f[39], f[151]) for _t, f in out] == [
        ("A", "A", "100"), ("0", "0", "100")]
    assert out[0][1][37] == out[1][1][37]          # same order
    assert out[0][1][17] != out[1][1][17]          # its own ExecID


def test_pending_new_is_off_by_default():
    book, _clock = make_book()
    out = sent(book.on_app_message(new_order(FIX_4_2, "P2"), QUOTE))
    assert [f[150] for _t, f in out] == ["0"]


def test_a_rejected_order_gets_no_pending_new():
    book, _clock = make_book(send_pending_new=True,
                             valid_accounts=["CERT1"])
    out = sent(book.on_app_message(new_order(FIX_4_2, "P3", account="NOPE"),
                                   QUOTE))
    assert [f[150] for _t, f in out] == ["8"]


# ------------------------------------------- Done For Day, Expire

@pytest.mark.parametrize("version", VERSIONS)
def test_done_for_day_closes_the_order(version):
    book, _clock = make_book(version)
    order_id = sent(book.on_app_message(new_order(version, "D1"), QUOTE))[0][1][37]
    out = sent(book.done_for_day(order_id))
    assert [(f[150], f[39], f[151], f[58]) for _t, f in out] == [
        ("3", "3", "0", "Done for day via control API")]
    assert book.get_order(order_id).closed
    with pytest.raises(OrderActionError) as raised:
        book.done_for_day(order_id)
    assert raised.value.code == "order_closed"


@pytest.mark.parametrize("version", VERSIONS)
def test_expire_closes_any_open_order(version):
    book, _clock = make_book(version)
    order_id = sent(book.on_app_message(new_order(version, "X1"), QUOTE))[0][1][37]
    out = sent(book.expire(order_id))
    assert [(f[150], f[39], f[151]) for _t, f in out] == [("C", "C", "0")]
    # A cancel after expiry is too late.
    rejected = sent(book.on_app_message(cancel(version, "X1-C", "X1"), QUOTE))
    assert rejected[0][0] == "9" and rejected[0][1][102] == "0"


# ------------------------------------------------------- lock / unlock

@pytest.mark.parametrize("version", VERSIONS)
def test_lock_makes_a_cancel_too_late_until_unlock(version):
    book, _clock = make_book(version)
    order_id = sent(book.on_app_message(new_order(version, "L1"), QUOTE))[0][1][37]
    book.lock(order_id)
    out = sent(book.on_app_message(cancel(version, "L1-C1", "L1"), QUOTE))
    assert out[0][0] == "9"
    fields = out[0][1]
    assert (fields[102], fields[434], fields[39]) == ("0", "1", "0")
    assert fields[58] == "Too late to cancel: a fill is in progress"
    assert not book.get_order(order_id).closed

    book.unlock(order_id)
    out = sent(book.on_app_message(cancel(version, "L1-C2", "L1"), QUOTE))
    assert [(f[150], f[39]) for _t, f in out] == [("4", "4")]


def test_a_fill_clears_the_lock():
    book, _clock = make_book()
    order_id = sent(book.on_app_message(new_order(FIX_4_2, "L2"), QUOTE))[0][1][37]
    book.lock(order_id)
    book.manual_fill(order_id, 10)
    assert not book.get_order(order_id).locked
    out = sent(book.on_app_message(cancel(FIX_4_2, "L2-C", "L2"), QUOTE))
    assert out[-1][1][150] == "4"


def test_lock_and_unlock_refuse_the_wrong_state():
    book, _clock = make_book()
    order_id = sent(book.on_app_message(new_order(FIX_4_2, "L3"), QUOTE))[0][1][37]
    with pytest.raises(OrderActionError) as raised:
        book.unlock(order_id)
    assert raised.value.code == "conflict"
    book.lock(order_id)
    with pytest.raises(OrderActionError):
        book.lock(order_id)


# --------------------------------------------------- account validation

@pytest.mark.parametrize("version,code", [(FIX_4_2, "0"), (FIX_4_4, "15")])
def test_an_unlisted_account_is_rejected(version, code):
    book, _clock = make_book(version, valid_accounts=["CERT1", "CERT2"])
    out = sent(book.on_app_message(new_order(version, "A1", account="NOPE"),
                                   QUOTE))
    assert [(f[150], f[39], f[103], f[58]) for _t, f in out] == [
        ("8", "8", code, "Unknown account NOPE")]


@pytest.mark.parametrize("version", VERSIONS)
def test_a_missing_account_is_allowed(version):
    """Cook 9.1 (Q4): only an Account that is present and unlisted is
    rejected."""
    book, _clock = make_book(version, valid_accounts=["CERT1", "CERT2"])
    out = sent(book.on_app_message(new_order(version, "A0", account=None),
                                   QUOTE))
    assert [(f[150], f[39]) for _t, f in out] == [("0", "0")]
    assert 1 not in out[0][1]


@pytest.mark.parametrize("version", VERSIONS)
def test_a_listed_account_is_accepted_and_echoed(version):
    book, _clock = make_book(version, valid_accounts=["CERT1", "CERT2"])
    out = sent(book.on_app_message(new_order(version, "A2", account="CERT2"),
                                   QUOTE))
    assert [(f[150], f[1]) for _t, f in out] == [("0", "CERT2")]


def test_no_list_means_no_account_check():
    book, _clock = make_book()
    out = sent(book.on_app_message(new_order(FIX_4_2, "A3", account="ANY"),
                                   QUOTE))
    assert out[0][1][150] == "0"


# ------------------------------------------------- OrderStatusRequest

@pytest.mark.parametrize("version", VERSIONS)
def test_status_of_a_known_order(version):
    book, _clock = make_book(version, status_requests=True)
    book.on_app_message(new_order(version, "S1"), QUOTE)
    order_id = book.orders()[0].order_id
    book.manual_fill(order_id, 40, "10.00")
    out = sent(book.on_app_message(
        status_request(version, "S1", extra=[(790, "REQ-1")]), QUOTE))
    assert len(out) == 1
    fields = out[0][1]
    assert (fields[37], fields[11], fields[39], fields[14], fields[151],
            fields[32]) == (order_id, "S1", "1", "40", "60", "0")
    if version == FIX_4_2:
        assert (fields[20], fields[150]) == ("3", "1")   # Status; = OrdStatus
        assert 790 not in fields
    else:
        assert fields[150] == "I" and 20 not in fields
        assert fields[790] == "REQ-1"
    # Asking changes nothing.
    assert book.get_order(order_id).cum_qty == 40


@pytest.mark.parametrize("version", VERSIONS)
def test_status_of_an_unknown_order(version):
    book, _clock = make_book(version, status_requests=True)
    out = sent(book.on_app_message(status_request(version, "NOPE"), QUOTE))
    fields = out[0][1]
    assert (fields[37], fields[11], fields[39], fields[58]) == (
        "NONE", "NOPE", "8", "Unknown order")
    assert fields[150] == ("8" if version == FIX_4_2 else "I")


def test_status_by_an_earlier_cl_ord_id_of_a_replaced_order():
    book, _clock = make_book(status_requests=True)
    book.on_app_message(new_order(FIX_4_2, "S2"), QUOTE)
    book.on_app_message(msg(FIX_4_2, "G", [
        (11, "S2-R"), (41, "S2"), (21, "1"), (55, "ZWZZT"), (54, "1"),
        (60, "20261001-14:00:00.000"), (38, "200"), (40, "2"),
        (44, "10.00")]), QUOTE)
    out = sent(book.on_app_message(status_request(FIX_4_2, "S2"), QUOTE))
    assert (out[0][1][11], out[0][1][38]) == ("S2-R", "200")


# ---------------------------------------------- end to end over FIX

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def engine(tmp_path):
    async def start(version=FIX_4_2, **orders):
        config = isolated_config(
            tmp_path, fix_version=version, fix_port=free_port(),
            api_port=free_port(), rules=RULES, control_api_enabled=True,
            orders=OrdersConfig(default_delay_ms=50, **orders))
        transport = Transport(config, clock=SystemClock())
        await transport.start()
        api = ControlApiServer(config, transport)
        await api.start()
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{api.bound_port}",
                                 timeout=10.0)
        started.append((transport, api, http))
        return transport, http

    started = []
    yield start
    for transport, api, http in started:
        await http.aclose()
        await api.stop()
        await transport.stop()
        transport.close_logs()


async def connect(transport, version=FIX_4_2) -> FixTestClient:
    fix = FixTestClient(fix_version=version)
    await fix.connect("127.0.0.1", transport.port)
    await fix.logon(30)
    assert (await fix.recv()).msg_type == "A"
    return fix


async def place(fix, cl_ord_id):
    await fix.send("D", [(11, cl_ord_id), (21, "1"), (55, "ZWZZT"), (54, "1"),
                         (38, "100"), (40, "2"), (44, "10.00"),
                         (60, "20261001-14:00:00.000")])
    ack = await fix.recv_until("8")
    return ack.get(37)


async def test_lock_cancel_unlock_cancel_through_the_api(engine):
    transport, http = await engine()
    fix = await connect(transport)
    order_id = await place(fix, "API-L1")

    locked = await http.post(f"/orders/{order_id}/lock")
    assert locked.status_code == 200 and locked.json()["locked"] is True
    await fix.send("F", [(11, "API-L1-C1"), (41, "API-L1"), (55, "ZWZZT"),
                         (54, "1"), (60, "20261001-14:00:00.000")])
    reject = await fix.recv_until("9")
    assert (reject.get(102), reject.get(434)) == ("0", "1")

    unlocked = await http.post(f"/orders/{order_id}/unlock")
    assert unlocked.status_code == 200 and unlocked.json()["locked"] is False
    assert (await http.post(f"/orders/{order_id}/unlock")).status_code == 409
    await fix.send("F", [(11, "API-L1-C2"), (41, "API-L1"), (55, "ZWZZT"),
                         (54, "1"), (60, "20261001-14:00:00.000")])
    canceled = await fix.recv_until("8")
    assert (canceled.get(150), canceled.get(39)) == ("4", "4")
    await fix.close()


async def test_done_for_day_and_expire_through_the_api(engine):
    transport, http = await engine()
    fix = await connect(transport)
    dfd_id = await place(fix, "API-D1")
    exp_id = await place(fix, "API-X1")

    response = await http.post(f"/orders/{dfd_id}/done-for-day")
    assert response.status_code == 200
    assert response.json()["order"]["ord_status"] == "3"
    report = await fix.recv_until("8")
    assert (report.get(150), report.get(39), report.get(151)) == ("3", "3", "0")

    response = await http.post(f"/orders/{exp_id}/expire")
    assert response.json()["order"]["ord_status"] == "C"
    report = await fix.recv_until("8")
    assert (report.get(150), report.get(39)) == ("C", "C")

    # Order checks before session checks: closed is 409, unknown 404.
    assert (await http.post(f"/orders/{dfd_id}/expire")).status_code == 409
    assert (await http.post("/orders/O-NOPE/done-for-day")).status_code == 404
    await fix.close()


async def test_status_request_is_answered_only_when_switched_on(engine):
    transport, _http = await engine(status_requests=True)
    fix = await connect(transport)
    order_id = await place(fix, "API-S1")
    await fix.send("H", [(11, "API-S1"), (55, "ZWZZT"), (54, "1")])
    answer = await fix.recv_until("8")
    assert (answer.get(20), answer.get(150), answer.get(39),
            answer.get(37)) == ("3", "0", "0", order_id)
    await fix.close()


async def test_status_request_is_a_business_reject_by_default(engine):
    transport, _http = await engine()
    fix = await connect(transport)
    await place(fix, "API-S2")
    await fix.send("H", [(11, "API-S2"), (55, "ZWZZT"), (54, "1")])
    reject = await fix.recv_until("j")
    assert (reject.get(372), reject.get(380)) == ("H", "3")
    await fix.close()
