"""Order chains and the ten consistency checks (spec 5)."""

import socket
from datetime import datetime, timezone

import pytest

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import SystemClock
from orderecho_Codec import Codec
from orderecho_LogParse import SOH, LogParser
from orderecho_Timeline import (
    FAIL,
    PASS,
    WARN,
    build_chain,
    check_avg_px,
    check_cum_qty_never_decreases,
    check_exec_ids_unique,
    check_fill_quantities_sum,
    check_nothing_after_terminal,
    check_order_id_constant,
    check_requests_answered,
    check_terminal_quantities,
    check_version_rules,
    check_working_quantities,
)
from orderecho_Transport import Transport

WHEN = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------- building fake logs


def line(msg_type, seq, direction="OUT", version="FIX.4.2", fields=None):
    """One OrderEcho-format log line carrying a well-framed message.

    The CompIDs follow the direction, as in a real log: OUT is ours
    (49=ORDERECHO), IN is the counterparty's (49=AGENT).
    """
    body = [(int(tag), str(value)) for tag, value in (fields or {}).items()
            if value is not None]
    sender, target = (("AGENT", "ORDERECHO") if direction == "IN"
                      else ("ORDERECHO", "AGENT"))
    raw = Codec(version).encode(
        msg_type, body, sender_comp_id=sender, target_comp_id=target,
        seq_num=seq, sending_time=WHEN,
    ).decode("latin-1").replace(SOH, "|")
    stamp = f"20260927-09:00:{seq:02d}.000"
    seq_col = f"seq={seq}"
    return f"{stamp} {direction:<4} {seq_col:<8} 35={msg_type:<3} {raw}"


def report(seq, exec_type, ord_status, order_qty="1000", cum="0", leaves="1000",
           avg="0.0000", last_qty="0", last_px="0.00", cl_ord_id="C1",
           exec_id=None, order_id="O-1", version="FIX.4.2", extra=None):
    fields = {
        37: order_id, 11: cl_ord_id, 17: exec_id or f"E-{seq}",
        150: exec_type, 39: ord_status, 55: "AAPL", 54: "1",
        38: order_qty, 40: "2", 44: "10.00", 32: last_qty, 31: last_px,
        151: leaves, 14: cum, 6: avg, 60: "20260927-09:00:00.000",
    }
    if version == "FIX.4.2":
        fields[20] = "0"
    fields.update(extra or {})
    return line("8", seq, version=version, fields=fields)


def parse(lines):
    parser = LogParser()
    return list(parser.parse_lines(lines, source="t.log", session="s"))


def chain_of(lines, cl_ord_id="C1", order_id=None):
    return build_chain(parse(lines), cl_ord_id=cl_ord_id, order_id=order_id)


def status_of(chain, name):
    for check in chain.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name}")


# --- 3: chains -------------------------------------------------------------

REPLACE_CHAIN = [
    line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "1000",
                          40: "2", 44: "10.00", 21: "1",
                          60: "20260927-09:00:00.000"}),
    report(2, "0", "0", cl_ord_id="C1"),
    line("G", 3, "IN", fields={11: "C2", 41: "C1", 55: "AAPL", 54: "1", 38: "2000",
                          40: "2", 44: "10.00", 21: "1",
                          60: "20260927-09:00:00.000"}),
    report(4, "5", "0", cl_ord_id="C2", order_qty="2000", leaves="2000",
           extra={41: "C1"}),
    line("G", 5, "IN", fields={11: "C3", 41: "C2", 55: "AAPL", 54: "1", 38: "3000",
                          40: "2", 44: "10.00", 21: "1",
                          60: "20260927-09:00:00.000"}),
    report(6, "5", "0", cl_ord_id="C3", order_qty="3000", leaves="3000",
           extra={41: "C2"}),
    line("F", 7, "IN", fields={11: "C4", 41: "C3", 55: "AAPL", 54: "1",
                          60: "20260927-09:00:00.000"}),
    report(8, "4", "4", cl_ord_id="C4", order_qty="3000", leaves="0",
           extra={41: "C3"}),
]

UNRELATED = [
    line("D", 20, "IN", fields={11: "OTHER", 55: "MSFT", 54: "1", 38: "50",
                           40: "1", 21: "1", 60: "20260927-09:00:00.000"}),
    report(21, "0", "0", cl_ord_id="OTHER", order_id="O-9", order_qty="50",
           leaves="50"),
]


@pytest.mark.parametrize("seed", ["C1", "C2", "C3", "C4"])
def test_the_whole_chain_is_found_from_any_of_its_cl_ord_ids(seed):
    chain = chain_of(REPLACE_CHAIN + UNRELATED, cl_ord_id=seed)
    assert chain.ids == {"C1", "C2", "C3", "C4"}
    assert len(chain.steps) == 8
    assert "OTHER" not in chain.ids
    assert all(step.message.get(55) == "AAPL" for step in chain.steps)


def test_the_chain_can_be_found_from_its_order_id():
    chain = chain_of(REPLACE_CHAIN + UNRELATED, cl_ord_id=None,
                     order_id="O-1")
    assert chain.ids == {"C1", "C2", "C3", "C4"}
    assert chain.order_ids == {"O-1"}


def test_unrelated_orders_are_left_out():
    chain = chain_of(REPLACE_CHAIN + UNRELATED, cl_ord_id="C1")
    assert not any(step.message.get(55) == "MSFT" for step in chain.steps)


def test_a_cancel_reject_joins_the_chain():
    lines = REPLACE_CHAIN + [
        line("9", 9, fields={37: "O-1", 11: "C5", 41: "C4", 39: "4", 434: "1",
                        102: "0", 58: "Order is CANCELED"}),
    ]
    chain = chain_of(lines, cl_ord_id="C1")
    assert "C5" in chain.ids
    assert any(step.message.msg_type == "9" for step in chain.steps)


def test_an_unknown_order_id_does_not_glue_chains_together():
    """A cancel reject saying 37=NONE must not join every order."""
    lines = REPLACE_CHAIN + UNRELATED + [
        line("9", 30, fields={37: "NONE", 11: "Z1", 41: "NOSUCH", 39: "8",
                         434: "1", 102: "1", 58: "Unknown order"}),
    ]
    chain = chain_of(lines, cl_ord_id="C1")
    assert "Z1" not in chain.ids
    assert "NONE" not in chain.order_ids


def test_an_empty_chain_is_not_an_error():
    chain = chain_of(REPLACE_CHAIN, cl_ord_id="NOTHERE")
    assert chain.steps == []
    assert chain.verdict == PASS


# --- 4: a passing chain per scenario, from real emulator output -------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


REAL_RULES = [
    {"name": "reject-me", "match": {"symbol": "ZVZZT"}, "behavior": "reject",
     "reject_code": 1, "text": "Unknown symbol"},
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "partials", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "60%"], "then": "leave",
     "delay_ms": 30},
    {"name": "full", "match": {"any": True}, "behavior": "full_fill",
     "delay_ms": 30},
]


async def emulator_log(tmp_path):
    """Drive a live session and hand back its FIX log, as really written."""
    config = isolated_config(tmp_path, fix_port=free_port(), rules=REAL_RULES,
                             delay_ms=30)
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    client = FixTestClient()
    try:
        await client.connect("127.0.0.1", transport.port)
        await client.logon(30)
        assert (await client.recv()).msg_type == "A"

        async def order(cl_ord_id, symbol, qty="1000", ord_type="2",
                        price="10.00"):
            fields = [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
                      (38, qty), (40, ord_type),
                      (60, "20260927-09:00:00.000")]
            if price is not None and ord_type == "2":
                fields.append((44, price))
            await client.send("D", fields)

        await order("R-FILL", "AAPL")                 # full fill
        await client.recv_until("8")
        await client.recv_until("8")

        await order("R-PART", "EFG")                  # two partials
        for _ in range(3):
            await client.recv_until("8")

        await order("R-HOLD", "ZWZZT")                # replace then cancel
        await client.recv_until("8")
        await client.send("G", [(11, "R-HOLD-2"), (41, "R-HOLD"), (21, "1"),
                                (55, "ZWZZT"), (54, "1"), (38, "2000"),
                                (40, "2"), (44, "10.00"),
                                (60, "20260927-09:00:00.000")])
        await client.recv_until("8")
        await client.send("F", [(11, "R-HOLD-3"), (41, "R-HOLD-2"),
                                (55, "ZWZZT"), (54, "1"),
                                (60, "20260927-09:00:00.000")])
        await client.recv_until("8")

        await order("R-REJ", "ZVZZT")                 # rule reject
        await client.recv_until("8")

        path = transport.fix_log.path
    finally:
        await client.close()
        await transport.wait_for_session_end(timeout=5.0)
        await transport.stop()
        transport.close_logs()
    return path


@pytest.fixture(scope="module")
def real_messages(tmp_path_factory):
    import asyncio
    tmp_path = tmp_path_factory.mktemp("reallog")
    path = asyncio.run(emulator_log(tmp_path))
    parser = LogParser()
    return list(parser.parse_file(path))


@pytest.mark.parametrize("cl_ord_id,expect_steps", [
    ("R-FILL", 3),      # D, ack, fill
    ("R-PART", 4),      # D, ack, two partials
    ("R-REJ", 2),       # D, reject
])
def test_real_emulator_chains_pass_every_check(real_messages, cl_ord_id,
                                               expect_steps):
    chain = build_chain(real_messages, cl_ord_id=cl_ord_id)
    assert len(chain.steps) == expect_steps, [
        (s.message.msg_type, s.message.get(150)) for s in chain.steps]
    assert chain.verdict == PASS, [
        (c.name, c.status, c.explanation) for c in chain.checks
        if c.status != PASS]
    assert chain.exit_code == 0


def test_a_real_replace_and_cancel_chain_passes(real_messages):
    chain = build_chain(real_messages, cl_ord_id="R-HOLD")
    assert chain.ids == {"R-HOLD", "R-HOLD-2", "R-HOLD-3"}
    statuses = [step.message.get(39) for step in chain.steps
                if step.message.msg_type == "8"]
    assert statuses[-1] == "4"                      # ends canceled
    assert chain.verdict == PASS, [
        (c.name, c.explanation) for c in chain.checks if c.status != PASS]


def test_every_check_ran_on_a_real_chain(real_messages):
    chain = build_chain(real_messages, cl_ord_id="R-FILL")
    assert len(chain.checks) == 11
    assert {check.name for check in chain.checks} == {
        "cum_qty_monotonic", "working_quantities", "terminal_quantities",
        "fill_quantities_sum", "avg_px", "exec_ids_unique",
        "order_id_constant", "nothing_after_terminal", "version_rules",
        "requests_answered", "framing_intact",
    }
    assert all(check.rule for check in chain.checks)


# --- 4: one hand-crafted failure per check ---------------------------------

def test_check_1_cum_qty_going_backwards():
    chain = chain_of([
        report(1, "1", "1", cum="500", leaves="500", last_qty="500",
               last_px="10.00", avg="10.0000"),
        report(2, "1", "1", cum="400", leaves="600", last_qty="100",
               last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "cum_qty_monotonic")
    assert check.status == FAIL
    assert "500 -> 400" in check.explanation
    assert chain.verdict == FAIL and chain.exit_code == 2


def test_check_2_working_quantities_that_do_not_add_up():
    chain = chain_of([report(1, "0", "0", order_qty="1000", cum="0",
                             leaves="900")])
    check = status_of(chain, "working_quantities")
    assert check.status == FAIL
    assert "OrderQty is 1000" in check.explanation


def test_check_3_terminal_state_still_leaving_quantity_open():
    chain = chain_of([report(1, "4", "4", cum="0", leaves="250")])
    check = status_of(chain, "terminal_quantities")
    assert check.status == FAIL
    assert "LeavesQty is 250" in check.explanation


def test_check_3_filled_but_not_for_the_whole_order():
    chain = chain_of([report(1, "2", "2", order_qty="1000", cum="900",
                             leaves="0", last_qty="900", last_px="10.00",
                             avg="10.0000")])
    check = status_of(chain, "terminal_quantities")
    assert check.status == FAIL
    assert "CumQty 900" in check.explanation


def test_check_4_fills_that_do_not_sum_to_cum_qty():
    chain = chain_of([
        report(1, "1", "1", cum="400", leaves="600", last_qty="400",
               last_px="10.00", avg="10.0000"),
        report(2, "2", "2", cum="1000", leaves="0", last_qty="100",
               last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "fill_quantities_sum")
    assert check.status == FAIL
    assert "totalling 500" in check.explanation


def test_check_5_average_price_that_is_not_the_average():
    chain = chain_of([
        report(1, "1", "1", cum="100", leaves="900", last_qty="100",
               last_px="10.00", avg="10.0000"),
        report(2, "1", "1", cum="200", leaves="800", last_qty="100",
               last_px="20.00", avg="10.0000"),
    ])
    check = status_of(chain, "avg_px")
    assert check.status == FAIL
    assert "15" in check.explanation


def test_check_5_accepts_a_correct_weighted_average():
    chain = chain_of([
        report(1, "1", "1", cum="100", leaves="900", last_qty="100",
               last_px="10.00", avg="10.0000"),
        report(2, "1", "1", cum="200", leaves="800", last_qty="100",
               last_px="20.00", avg="15.0000"),
    ])
    assert status_of(chain, "avg_px").status == PASS


def test_check_6_a_repeated_exec_id():
    chain = chain_of([
        report(1, "0", "0", exec_id="E-SAME"),
        report(2, "1", "1", exec_id="E-SAME", cum="100", leaves="900",
               last_qty="100", last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "exec_ids_unique")
    assert check.status == FAIL
    assert "E-SAME" in check.explanation


def test_check_7_two_order_ids_in_one_chain():
    chain = chain_of([
        report(1, "0", "0", order_id="O-1"),
        report(2, "0", "0", order_id="O-2"),
    ])
    check = status_of(chain, "order_id_constant")
    assert check.status == FAIL
    assert "O-1" in check.explanation and "O-2" in check.explanation


def test_check_8_a_report_after_a_terminal_one():
    chain = chain_of([
        report(1, "2", "2", cum="1000", leaves="0", last_qty="1000",
               last_px="10.00", avg="10.0000"),
        report(2, "1", "1", cum="1100", leaves="0", last_qty="100",
               last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "nothing_after_terminal")
    assert check.status == FAIL
    assert "terminal" in check.explanation


def test_check_8_ignores_a_replayed_possdup_report():
    """A resent copy of an earlier report is shown but not counted."""
    lines = [
        report(1, "2", "2", cum="1000", leaves="0", last_qty="1000",
               last_px="10.00", avg="10.0000", exec_id="E-1"),
        report(1, "2", "2", cum="1000", leaves="0", last_qty="1000",
               last_px="10.00", avg="10.0000", exec_id="E-1",
               extra={43: "Y", 122: "20260927-09:00:01.000"}),
    ]
    chain = chain_of(lines)
    assert len(chain.steps) == 2                    # both are shown
    assert chain.steps[1].replay is True
    assert status_of(chain, "nothing_after_terminal").status == PASS
    assert status_of(chain, "exec_ids_unique").status == PASS
    assert status_of(chain, "fill_quantities_sum").status == PASS
    assert chain.verdict == PASS


def test_check_9_a_44_report_using_the_42_fill_exec_type():
    chain = chain_of([
        report(1, "2", "2", version="FIX.4.4", cum="1000", leaves="0",
               last_qty="1000", last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "version_rules")
    assert check.status == FAIL
    assert "150=2" in check.explanation


def test_check_9_a_44_report_carrying_exec_trans_type():
    chain = chain_of([
        report(1, "F", "2", version="FIX.4.4", cum="1000", leaves="0",
               last_qty="1000", last_px="10.00", avg="10.0000",
               extra={20: "0"}),
    ])
    check = status_of(chain, "version_rules")
    assert check.status == FAIL
    assert "tag 20" in check.explanation


def test_check_9_a_42_report_using_the_44_trade_exec_type():
    chain = chain_of([
        report(1, "F", "2", version="FIX.4.2", cum="1000", leaves="0",
               last_qty="1000", last_px="10.00", avg="10.0000"),
    ])
    check = status_of(chain, "version_rules")
    assert check.status == FAIL
    assert "150=F" in check.explanation


def test_check_10_a_request_nobody_answered():
    chain = chain_of([
        line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "1000",
                              40: "2", 44: "10.00", 21: "1",
                              60: "20260927-09:00:00.000"}),
    ])
    check = status_of(chain, "requests_answered")
    assert check.status == WARN
    assert "seq=1" in check.explanation
    assert chain.verdict == WARN and chain.exit_code == 1


def test_check_10_counts_a_session_reject_as_an_answer():
    chain = chain_of([
        line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "1000",
                              40: "2", 44: "10.00", 21: "1",
                              60: "20260927-09:00:00.000"}),
        line("3", 2, fields={45: "1", 371: "38", 373: "1",
                        58: "Required tag missing: 38"}),
    ])
    assert status_of(chain, "requests_answered").status == PASS


# --- the checks never throw ------------------------------------------------

@pytest.mark.parametrize("check", [
    check_cum_qty_never_decreases, check_working_quantities,
    check_terminal_quantities, check_fill_quantities_sum, check_avg_px,
    check_exec_ids_unique, check_order_id_constant,
    check_nothing_after_terminal, check_version_rules,
    check_requests_answered,
])
def test_checks_survive_junk_values(check):
    chain = chain_of([
        report(1, "0", "0", cum="nonsense", leaves="", avg="oops",
               order_qty="lots", last_qty="?", last_px="free"),
    ])
    result = check(chain)
    assert result.status in (PASS, WARN, FAIL)
    assert result.explanation


def test_checks_survive_an_empty_chain():
    from orderecho_Timeline import Chain, run_checks

    results = run_checks(Chain(seed="none"))
    assert len(results) == 11
    assert all(result.status == PASS for result in results)
