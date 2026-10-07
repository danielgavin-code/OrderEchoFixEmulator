"""Timeline check fixes for parity with the Go agent (Cook 9 spec 7).

Each regression replays the shape of log that exposed the bug in the agent's
parity runs (OrderEcho REPORT_A2 section 6, REPORT_A3 section 3.2).
"""

from datetime import datetime, timezone

from orderecho_Codec import Codec
from orderecho_LogParse import (
    DIR_IN,
    DIR_OUT,
    SOH,
    LogParser,
    ParsedMessage,
    find_messages,
    split_fields,
    verify_framing,
)
from orderecho_Timeline import PASS, WARN, build_chain, sent_by_same_side

WHEN = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
AGENT, EMULATOR = "AGENT", "ORDERECHO"


def wire(msg_type, seq, sender, fields, version="FIX.4.2") -> bytes:
    target = EMULATOR if sender == AGENT else AGENT
    return Codec(version).encode(msg_type, fields, sender_comp_id=sender,
                                 target_comp_id=target, seq_num=seq,
                                 sending_time=WHEN)


def log_line(direction, raw: bytes, seq, msg_type, encoding="latin-1") -> str:
    """An OrderEcho-format FIX log line, pipe-delimited."""
    text = raw.decode(encoding).replace(SOH, "|")
    return (f"20260929-09:00:{seq:02d}.000 {direction:<4} seq={seq:<4} "
            f"35={msg_type:<3} {text}")


def parse(lines, session="emu42"):
    return list(LogParser().parse_lines(lines, source="agent.log",
                                        session=session))


def check(chain, name):
    return next(c for c in chain.checks if c.name == name)


D_FIELDS = [(11, "C1"), (21, "1"), (55, "AAPL"), (54, "1"), (38, "100"),
            (40, "2"), (44, "10.00"), (60, "20260929-09:00:00.000")]


# --------------------- 1. a Reject only answers the other side's request

def test_our_own_reject_does_not_answer_our_own_request():
    """The agent's log: our D is seq 5; later *we* reject the counterparty's
    message 5.  Python used to call the D answered (PASS); Go says WARN."""
    lines = [
        log_line("OUT", wire("D", 5, AGENT, D_FIELDS), 5, "D"),
        log_line("OUT", wire("3", 6, AGENT, [(45, "5"), (373, "1"),
                                              (58, "Required tag missing")]),
                 6, "3"),
    ]
    chain = build_chain(parse(lines), cl_ord_id="C1")
    assert check(chain, "requests_answered").status == WARN
    assert [m.msg_type for m in chain.messages] == ["D"]   # not pulled in


def test_the_counterpartys_reject_does_answer_it():
    lines = [
        log_line("OUT", wire("D", 5, AGENT, D_FIELDS), 5, "D"),
        log_line("IN", wire("3", 5, EMULATOR, [(45, "5"), (373, "1"),
                                                (58, "Required tag missing")]),
                 5, "3"),
    ]
    chain = build_chain(parse(lines), cl_ord_id="C1")
    assert check(chain, "requests_answered").status == PASS
    assert [m.msg_type for m in chain.messages] == ["D", "3"]


def test_without_comp_ids_the_log_direction_decides():
    request = ParsedMessage(raw="", fields=[(35, "D"), (34, "5"), (11, "C1")],
                            direction=DIR_OUT)
    ours = ParsedMessage(raw="", fields=[(35, "3"), (45, "5")],
                         direction=DIR_OUT)
    theirs = ParsedMessage(raw="", fields=[(35, "3"), (45, "5")],
                           direction=DIR_IN)
    unknown = ParsedMessage(raw="", fields=[(35, "3"), (45, "5")])
    assert sent_by_same_side(ours, request)
    assert not sent_by_same_side(theirs, request)
    assert not sent_by_same_side(unknown, request)    # cannot tell: count it


# -------------------------- 2. 110= (and friends) does not end a message

def test_a_tag_ending_in_10_does_not_cut_the_message():
    """MinQty (110) used to end the message at `110=100|`, flagging it and
    losing every field after it."""
    raw = wire("D", 5, AGENT, D_FIELDS[:6] + [(110, "100"), (1010, "7"),
                                              (44, "10.00"), (58, "after")])
    messages = parse([log_line("OUT", raw, 5, "D")])
    assert len(messages) == 1
    msg = messages[0]
    assert not msg.bad_checksum and not msg.bad_length
    assert (msg.get(110), msg.get(1010), msg.get(58)) == ("100", "7", "after")


def test_a_wrong_body_length_is_still_found_and_flagged():
    raw = wire("D", 5, AGENT, D_FIELDS + [(110, "100")]).decode("latin-1")
    raw = raw.replace("\x019=", "\x019=1", 1)          # BodyLength now wrong
    found = find_messages(raw.replace(SOH, "|"))
    assert len(found) == 1
    text = found[0][0]
    assert "|110=100|" in text and text.endswith("|10=" + text[-4:-1] + "|")
    bad_length, _bad_checksum = verify_framing(text.replace("|", SOH))
    assert bad_length


def test_two_messages_on_one_line_are_both_found():
    first = wire("0", 2, AGENT, [(112, "x110=1")]).decode("latin-1")
    second = wire("0", 3, AGENT, []).decode("latin-1")
    found = find_messages((first + second).replace(SOH, "|"))
    assert [split_fields(f.replace("|", SOH))[-1][0] for f, _d in found] == [10, 10]
    assert len(found) == 2


# --------------------------- 3. non-ASCII values are checked as written

def test_a_utf8_value_written_byte_for_byte_is_not_flagged():
    """The Go agent writes the wire bytes as they are, so a UTF-8 Text reads
    back as 'reçu'.  Re-encoding that as latin-1 changed the bytes and the
    checksum; the bytes as written verify."""
    raw = wire("j", 7, EMULATOR, [(45, "5"), (372, "ZZ"), (380, "3"),
                                  (58, "reçu")])
    assert b"re\xc3\xa7u" in raw                 # UTF-8 on the wire
    msg = parse([log_line("IN", raw, 7, "j", encoding="utf-8")])[0]
    assert msg.get(58) == "reçu"
    assert not msg.bad_checksum and not msg.bad_length


def test_the_emulators_own_one_char_per_byte_logging_still_verifies():
    """This emulator logs one character per wire byte (latin-1 decoding), so
    the same message reads back as 'reÃ§u' -- and must verify too."""
    raw = wire("8", 7, EMULATOR, [(37, "O-1"), (11, "C1"), (58, "reçu")])
    msg = parse([log_line("OUT", raw, 7, "8", encoding="latin-1")])[0]
    assert msg.get(58) == "re\u00c3\u00a7u"
    assert not msg.bad_checksum and not msg.bad_length


def test_a_really_broken_checksum_is_still_flagged():
    raw = wire("0", 7, AGENT, [(58, "reçu")]).decode("latin-1")
    broken = raw[:-4] + ("000" if raw[-4:-1] != "000" else "001") + SOH
    msg = parse([log_line("OUT", broken.encode("latin-1"), 7, "0")])[0]
    assert msg.bad_checksum


# ------------------ 4. a duplicate request's reject forms its own chain

def _scenario_9():
    """A2 scenario 9: an order, then a second D reusing its ClOrdID, which
    the counterparty rejects as a duplicate on a new OrderID."""
    er_common = [(55, "AAPL"), (54, "1"), (38, "100"), (40, "2"),
                 (44, "10.00"), (60, "20260929-09:00:00.000")]
    return [
        log_line("OUT", wire("D", 2, AGENT, D_FIELDS), 2, "D"),
        log_line("IN", wire("8", 2, EMULATOR, [
            (37, "O-1"), (11, "C1"), (17, "E-1"), (20, "0"), (150, "0"),
            (39, "0"), *er_common, (32, "0"), (31, "0"), (151, "100"),
            (14, "0"), (6, "0")]), 2, "8"),
        log_line("OUT", wire("D", 3, AGENT, D_FIELDS), 3, "D"),
        log_line("IN", wire("8", 3, EMULATOR, [
            (37, "O-2"), (11, "C1"), (17, "E-2"), (20, "0"), (150, "8"),
            (39, "8"), *er_common, (32, "0"), (31, "0"), (151, "0"),
            (14, "0"), (6, "0"), (103, "6"), (58, "Duplicate ClOrdID")]),
            3, "8"),
        log_line("IN", wire("8", 4, EMULATOR, [
            (37, "O-1"), (11, "C1"), (17, "E-3"), (20, "0"), (150, "2"),
            (39, "2"), *er_common, (32, "100"), (31, "10.00"), (151, "0"),
            (14, "100"), (6, "10.0000")]), 4, "8"),
    ]


def test_the_original_order_is_not_judged_by_its_duplicates_reject():
    chain = build_chain(parse(_scenario_9()), cl_ord_id="C1")
    assert check(chain, "order_id_constant").status == PASS
    assert chain.verdict == PASS
    assert chain.order_ids == {"O-1"}
    assert [m.get(37) for m in chain.messages if m.msg_type == "8"] == [
        "O-1", "O-1"]


def test_the_duplicates_order_id_finds_the_split_off_chain():
    chain = build_chain(parse(_scenario_9()), order_id="O-2")
    assert [(m.msg_type, m.seq) for m in chain.messages] == [("D", 3), ("8", 3)]
    assert check(chain, "order_id_constant").status == PASS
    assert check(chain, "requests_answered").status == PASS
