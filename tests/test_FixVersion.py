"""FIX 4.4 support and the version profiles (spec 4, 5)."""

import asyncio
import os
import socket
from decimal import Decimal

import pytest

from fix_TestClient import FixTestClient
from golden_scenario import SCENARIOS, Scenario
from isolation import isolated_config
from orderecho_Clock import SystemClock
from orderecho_Codec import Codec, FixMsg
from orderecho_Config import ConfigError, PriceBandConfig, load_config
from orderecho_FixVersion import (
    FIX42,
    FIX44,
    FIX_4_2,
    FIX_4_4,
    CancelReject,
    ExecReport,
    Reason,
    ReportKind,
    ResponseTo,
    UnknownFixVersion,
    profile_for,
)
from orderecho_MessageStore import MessageStore
from orderecho_Transport import Transport

BOTH = [FIX_4_2, FIX_4_4]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fields(text_line: str) -> dict:
    body = text_line.split(" ", 2)[2]
    return dict(pair.split("=", 1) for pair in body.split("|"))


def sends(text: str, msg_type="8") -> list:
    return [line for line in text.splitlines()
            if line.startswith(f"SEND {msg_type} ")]


# --- profile lookup --------------------------------------------------------

def test_profiles_are_found_by_version():
    assert profile_for(FIX_4_2) is FIX42
    assert profile_for(FIX_4_4) is FIX44


def test_unknown_version_is_rejected():
    with pytest.raises(UnknownFixVersion, match="FIX.5.0"):
        profile_for("FIX.5.0")


def test_config_rejects_an_unknown_fix_version(tmp_path):
    from test_DemoClient import CONFIG_TEMPLATE

    body = CONFIG_TEMPLATE.format(port=9999, tmp=tmp_path).replace(
        "fix_version: FIX.4.2", "fix_version: FIX.5.0"
    )
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported FIX version"):
        load_config(str(path))


def test_the_shipped_44_config_loads():
    config = load_config("config/orderecho_fix44.yaml")
    assert config.session.fix_version == FIX_4_4


# 2 -------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_fix44_never_emits_exec_trans_type(name):
    """Tag 20 was removed in 4.4 and must appear nowhere (spec 5.2)."""
    text = SCENARIOS[name](FIX_4_4)
    for line in text.splitlines():
        assert "|20=" not in line, line
        assert not line.startswith("SEND 8 20="), line


def test_fix44_exec_types_for_every_report_kind():
    expected = {
        ReportKind.ACK: "0",
        ReportKind.PARTIAL_FILL: "F",
        ReportKind.FILL: "F",
        ReportKind.CANCELED: "4",
        ReportKind.REPLACED: "5",
        ReportKind.PENDING_CANCEL: "6",
        ReportKind.PENDING_REPLACE: "E",
        ReportKind.REJECTED: "8",
    }
    for kind, value in expected.items():
        assert FIX44.exec_type_for(kind) == value
    # 4.2 keeps the two fill kinds apart.
    assert FIX42.exec_type_for(ReportKind.PARTIAL_FILL) == "1"
    assert FIX42.exec_type_for(ReportKind.FILL) == "2"


def test_fix44_fills_are_trades_distinguished_by_ord_status():
    text = SCENARIOS["partial_fills"](FIX_4_4)
    fills = [fields(line) for line in sends(text)
             if fields(line)["150"] in ("F", "1", "2")]
    assert fills, text
    assert all(fill["150"] == "F" for fill in fills)
    statuses = [fill["39"] for fill in fills]
    assert "1" in statuses and "2" in statuses      # partial and full
    partial = next(f for f in fills if f["39"] == "1")
    full = next(f for f in fills if f["39"] == "2")
    assert partial["151"] != "0"
    assert full["151"] == "0"


def test_fix44_ack_cancel_replace_pending_reject_codes():
    acks = [fields(line) for line in sends(SCENARIOS["acks"](FIX_4_4))]
    assert acks[0]["150"] == "0" and acks[0]["39"] == "0"

    pend = [fields(line) for line in sends(SCENARIOS["pending_acks"](FIX_4_4))]
    exec_types = [f["150"] for f in pend]
    assert "6" in exec_types and "4" in exec_types     # pending cancel, cancel
    assert "E" in exec_types and "5" in exec_types     # pending replace, replace

    rejected = [fields(line) for line in sends(SCENARIOS["rejects_rule"](FIX_4_4))]
    assert all(f["150"] == "8" and f["39"] == "8" for f in rejected)


#: session_rejects is deliberately *not* version-neutral: it omits HandlInst,
#: which 4.2 requires and 4.4 does not (spec 5.3).  The HandlInst tests below
#: cover that difference directly.
VERSION_NEUTRAL_SCENARIOS = sorted(set(SCENARIOS) - {"session_rejects"})


@pytest.mark.parametrize("name", VERSION_NEUTRAL_SCENARIOS)
def test_both_versions_agree_on_what_happened(name):
    """Version-neutral facts must not differ between the profiles (spec 9.7)."""
    text42 = SCENARIOS[name](FIX_4_2)
    text44 = SCENARIOS[name](FIX_4_4)

    lines42 = text42.splitlines()
    lines44 = text44.splitlines()
    assert len(lines42) == len(lines44)
    for line42, line44 in zip(lines42, lines44):
        assert line42.split(" ")[0] == line44.split(" ")[0]
        if not line42.startswith("SEND 8 "):
            continue
        a, b = fields(line42), fields(line44)
        # Everything except how FIX spells the event is identical.
        for tag in ("37", "11", "39", "55", "54", "38", "40", "32", "31",
                    "151", "14", "6"):
            assert a.get(tag) == b.get(tag), (tag, line42, line44)
        assert a.get("58") == b.get("58")


# 3 -------------------------------------------------------------------------

def test_handl_inst_is_optional_in_44_and_required_in_42():
    accepted = Scenario(version=FIX_4_4)
    accepted.send(accepted.new_order("H1", symbol="ZWZZT", ord_type="2",
                                     price="230.00", omit=(21,)))
    assert sends(accepted.text()), accepted.text()
    assert fields(sends(accepted.text())[0])["150"] == "0"

    refused = Scenario(version=FIX_4_2)
    refused.send(refused.new_order("H2", symbol="ZWZZT", ord_type="2",
                                   price="230.00", omit=(21,)))
    line = refused.text().strip()
    assert line.startswith("REJECT ")
    assert "ref_tag=21" in line
    assert "reason=1" in line


def test_handl_inst_is_still_validated_when_present_in_44():
    s = Scenario(version=FIX_4_4)
    s.send(s.new_order("H3", handl_inst="9"))
    assert "ref_tag=21" in s.text()
    assert "reason=5" in s.text()


@pytest.mark.parametrize("version,side,expect_reject", [
    (FIX_4_2, "A", True),      # not a 4.2 Side value
    (FIX_4_4, "A", False),     # Cross short exempt exists in 4.4
    (FIX_4_4, "Z", True),
])
def test_side_enumerations_differ_between_versions(version, side,
                                                   expect_reject):
    s = Scenario(version=version)
    s.send(s.new_order("E1", side=side))
    text = s.text()
    if expect_reject:
        assert "ref_tag=54" in text and "reason=5" in text
    else:
        # Valid in 4.4 but not one we support: a business reject, not a
        # structural one (spec 5.3).
        assert "REJECT " not in text
        assert fields(sends(text)[0])["150"] == "8"


@pytest.mark.parametrize("version,ord_type,expect_reject", [
    (FIX_4_2, "J", True),      # Market-if-touched arrived in 4.4
    (FIX_4_4, "J", False),
    (FIX_4_4, "Z", True),
])
def test_ord_type_enumerations_differ_between_versions(version, ord_type,
                                                       expect_reject):
    s = Scenario(version=version)
    s.send(s.new_order("E2", ord_type=ord_type))
    text = s.text()
    if expect_reject:
        assert "ref_tag=40" in text and "reason=5" in text
    else:
        assert "REJECT " not in text
        assert fields(sends(text)[0])["150"] == "8"


# 4 -------------------------------------------------------------------------

@pytest.mark.parametrize("reason,code42,code44", [
    (Reason.UNSUPPORTED_CHARACTERISTIC, "0", "11"),
    (Reason.BAD_QUANTITY, "0", "13"),
    (Reason.BAD_PRICE, "0", "0"),
    (Reason.DUPLICATE_CL_ORD_ID, "6", "6"),
    (Reason.BAND_BREACH, "3", "3"),
    (Reason.NO_REFERENCE, "0", "99"),
])
def test_order_reject_reason_table(reason, code42, code44):
    assert FIX42.reject_code_for(reason) == code42
    assert FIX44.reject_code_for(reason) == code44


@pytest.mark.parametrize("reason,code42,code44", [
    (Reason.CXL_TOO_LATE, "0", "0"),
    (Reason.CXL_UNKNOWN_ORDER, "1", "1"),
    (Reason.CXL_BROKER_OPTION, "2", "2"),
    (Reason.DUPLICATE_CL_ORD_ID, "2", "6"),
])
def test_cancel_reject_reason_table(reason, code42, code44):
    assert FIX42.cancel_reject_code_for(reason) == code42
    assert FIX44.cancel_reject_code_for(reason) == code44


def test_a_rule_reject_code_wins_over_the_table_in_both_versions():
    for profile in (FIX42, FIX44):
        assert profile.reject_code_for(Reason.RULE_REJECT, "1") == "1"


@pytest.mark.parametrize("version,expected", [(FIX_4_2, "0"), (FIX_4_4, "13")])
def test_bad_quantity_code_end_to_end(version, expected):
    s = Scenario(version=version)
    s.send(s.new_order("Q1", qty="0"))
    assert fields(sends(s.text())[0])["103"] == expected


@pytest.mark.parametrize("version,expected", [(FIX_4_2, "0"), (FIX_4_4, "11")])
def test_unsupported_side_code_end_to_end(version, expected):
    s = Scenario(version=version)
    s.send(s.new_order("Q2", side="3"))
    assert fields(sends(s.text())[0])["103"] == expected


@pytest.mark.parametrize("version,expected", [(FIX_4_2, "2"), (FIX_4_4, "6")])
def test_duplicate_clordid_on_cancel_code_end_to_end(version, expected):
    s = Scenario(version=version)
    s.send(s.new_order("Q3", symbol="ZWZZT", ord_type="2", price="230.00"))
    s.send(s.cancel("Q3", "Q3", symbol="ZWZZT"))
    reject = [line for line in s.text().splitlines()
              if line.startswith("SEND 9 ")][0]
    assert fields(reject)["102"] == expected


@pytest.mark.parametrize("version,expected", [(FIX_4_2, "0"), (FIX_4_4, "99")])
def test_no_reference_reject_code_end_to_end(version, expected):
    band = PriceBandConfig(enabled=True, pct=10, mode="aggressive",
                           on_no_reference="reject")
    s = Scenario(version=version)
    s.book.price_band = band
    s.send(s.new_order("Q4", symbol="ZWZZT", ord_type="2", price="230.00"),
           quote=None)
    report = fields(sends(s.text())[0])
    assert report["150"] == "8"
    assert report["103"] == expected
    assert report["58"] == "No reference price available"


# rendering directly --------------------------------------------------------

def make_report(**overrides):
    base = dict(
        kind=ReportKind.ACK, order_id="O-1", cl_ord_id="C1", exec_id="E-1",
        symbol="AAPL", side="1", order_qty="100", ord_type="1",
        ord_status="0", leaves_qty="100", cum_qty="0", avg_px="0.0000",
        transact_time="20260101-00:00:00.000",
    )
    base.update(overrides)
    return ExecReport(**base)


def test_render_puts_fields_in_the_expected_order():
    body42 = [int(tag) for tag, _ in FIX42.render_exec_report(make_report())]
    assert body42 == [37, 11, 17, 20, 150, 39, 55, 54, 38, 40, 32, 31, 151,
                      14, 6, 60]
    body44 = [int(tag) for tag, _ in FIX44.render_exec_report(make_report())]
    assert body44 == [37, 11, 17, 150, 39, 55, 54, 38, 40, 32, 31, 151,
                      14, 6, 60]


def test_render_includes_optional_fields_when_present():
    report = make_report(orig_cl_ord_id="C0", account="ACC", price="10.00",
                         reason=Reason.BAND_BREACH, text="nope")
    body = [int(tag) for tag, _ in FIX44.render_exec_report(report)]
    assert 41 in body and 1 in body and 44 in body and 103 in body and 58 in body
    assert body.index(41) == body.index(11) + 1


def test_render_cancel_reject_fields():
    reject = CancelReject(order_id="O-1", cl_ord_id="C2", orig_cl_ord_id="C1",
                          ord_status="0", response_to=ResponseTo.REPLACE,
                          reason=Reason.DUPLICATE_CL_ORD_ID, text="dup")
    body = FIX44.render_cancel_reject(reject)
    assert [int(tag) for tag, _ in body] == [37, 11, 41, 39, 434, 102, 58]
    assert dict((int(t), v) for t, v in body)[434] == "2"
    assert dict((int(t), v) for t, v in body)[102] == "6"


# 5 ------------------------------------------------------------------- wire


async def start_engine(tmp_path, fix_version, **kwargs):
    config = isolated_config(tmp_path, fix_version=fix_version,
                             fix_port=free_port(), **kwargs)
    transport = Transport(config, clock=SystemClock())
    await transport.start(config.session.port)
    return transport


@pytest.mark.parametrize("session_version,client_version", [
    (FIX_4_2, FIX_4_4),
    (FIX_4_4, FIX_4_2),
])
async def test_begin_string_mismatch_on_logon(tmp_path, session_version,
                                              client_version):
    transport = await start_engine(tmp_path, session_version)
    try:
        client = FixTestClient(fix_version=client_version)
        await client.connect("127.0.0.1", transport.port)
        await client.logon(30)

        logout = await client.recv_until("5")
        assert logout is not None
        assert logout.get(58) == (
            f"Incorrect BeginString, expected {session_version}"
        )
        assert logout.get(8) == session_version      # we answer in ours
        assert await client.wait_closed(timeout=5.0)
        assert not any(isinstance(m, FixMsg) and m.msg_type == "3"
                       for m in client.received)

        with open(transport.engine_log.path, encoding="utf-8") as handle:
            assert "begin string mismatch" in handle.read()
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


async def test_begin_string_mismatch_mid_session(tmp_path):
    transport = await start_engine(tmp_path, FIX_4_2)
    try:
        client = FixTestClient(fix_version=FIX_4_2)
        await client.connect("127.0.0.1", transport.port)
        await client.logon(30)
        assert (await client.recv()).msg_type == "A"

        # Same session, suddenly a different dialect.
        rogue = Codec(FIX_4_4).encode(
            "1", [(112, "ROGUE")], sender_comp_id="AGENT",
            target_comp_id="ORDERECHO", seq_num=2,
            sending_time=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc),
        )
        await client.send_raw(rogue)

        logout = await client.recv_until("5")
        assert logout.get(58) == "Incorrect BeginString, expected FIX.4.2"
        assert await client.wait_closed(timeout=5.0)
        assert not any(isinstance(m, FixMsg) and m.msg_type == "3"
                       for m in client.received)
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


# 6 -------------------------------------------------------------------------

def test_store_records_the_version_it_was_sent_with(tmp_path):
    store = MessageStore(str(tmp_path / "msgstore"), "ORDERECHO", "AGENT")
    store.append(1, "8", b"8=FIX.4.4\x0135=8\x0110=001\x01",
                 fix_version=FIX_4_4)
    assert store.stored_version() == FIX_4_4
    store.close()

    reloaded = MessageStore(str(tmp_path / "msgstore"), "ORDERECHO", "AGENT")
    assert reloaded.stored_version() == FIX_4_4


async def test_store_is_archived_when_the_version_changes(tmp_path):
    transport = await start_engine(tmp_path, FIX_4_2)
    port = transport.port
    try:
        client = FixTestClient(fix_version=FIX_4_2)
        await client.connect("127.0.0.1", port)
        await client.logon(30)
        assert (await client.recv()).msg_type == "A"
        await client.close()
        await transport.wait_for_session_end(timeout=5.0)
        assert len(transport.message_store) >= 1
        store_path = transport.message_store.path
    finally:
        await transport.stop()
        transport.close_logs()

    # Same storage, different version: the old bytes must not be replayable.
    config = isolated_config(tmp_path, fix_version=FIX_4_4, fix_port=port)
    restarted = Transport(config, clock=SystemClock())
    try:
        assert len(restarted.message_store) == 0
        directory = os.path.dirname(store_path)
        archived = [name for name in os.listdir(directory)
                    if name.startswith(os.path.basename(store_path) + ".")]
        assert len(archived) == 1
        with open(restarted.engine_log.path, encoding="utf-8") as handle:
            engine = handle.read()
        assert "holds FIX.4.2 messages but this session is FIX.4.4" in engine
    finally:
        restarted.close_logs()


async def test_replay_in_a_44_session_sends_44_bytes(tmp_path):
    band = PriceBandConfig(enabled=False)
    transport = await start_engine(tmp_path, FIX_4_4, band=band)
    try:
        client = FixTestClient(fix_version=FIX_4_4)
        await client.connect("127.0.0.1", transport.port)
        await client.logon(30)
        assert (await client.recv()).msg_type == "A"

        await client.send("D", [(11, "V1"), (55, "AAPL"), (54, "1"),
                                (38, "100"), (40, "2"), (44, "10.00"),
                                (60, "20260101-00:00:00.000")])
        ack = await client.recv_until("8")
        assert ack.get(8) == FIX_4_4
        assert ack.get(20) is None

        await client.send("2", [(7, str(ack.seq_num)), (16, str(ack.seq_num))])
        replay = await client.recv_until("8")
        assert replay.get(8) == FIX_4_4
        assert replay.get(20) is None
        assert replay.get(43) == "Y"
        assert replay.seq_num == ack.seq_num
        assert replay.get(122) == ack.get(52)
        await client.close()
    finally:
        await transport.stop()
        transport.close_logs()


# 7 --------------------------------------------------- both versions on wire

@pytest.mark.parametrize("version", BOTH)
async def test_order_flow_end_to_end_in_both_versions(tmp_path, version):
    transport = await start_engine(tmp_path, version)
    try:
        client = FixTestClient(fix_version=version)
        await client.connect("127.0.0.1", transport.port)
        await client.logon(30)
        assert (await client.recv()).msg_type == "A"

        fields_out = [(11, "X1"), (55, "AAPL"), (54, "1"), (38, "1000"),
                      (40, "2"), (44, "10.00"),
                      (60, "20260101-00:00:00.000")]
        if version == FIX_4_2:
            fields_out.insert(1, (21, "1"))
        await client.send("D", fields_out)

        ack = await client.recv_until("8")
        assert ack.get(8) == version
        assert ack.get(150) == "0" and ack.get(39) == "0"

        fill = await client.recv_until("8")
        assert fill.get(39) == "2"
        assert fill.get(150) == ("2" if version == FIX_4_2 else "F")
        assert (fill.get(20) is not None) == (version == FIX_4_2)
        assert fill.get(32) == "1000"
        assert fill.get(31) == "10.00"

        await client.close()
        await transport.wait_for_session_end(timeout=5.0)

        with open(transport.fix_log.path, encoding="utf-8") as handle:
            log = handle.read()
        assert f"8={version}|" in log
        if version == FIX_4_4:
            assert "|20=" not in log
    finally:
        await transport.stop()
        transport.close_logs()
