"""The log parser (spec 3) and the FIX dictionary (spec 4)."""

import json
import os

import pytest

from orderecho_Codec import Codec
from orderecho_FixDict import BASE_PATH, OVERLAY_PATH, FixDictionary
from orderecho_LogParse import (
    DIR_DISC,
    DIR_IN,
    DIR_OUT,
    DIR_UNKNOWN,
    SOH,
    LogParser,
    find_messages,
    parse_timestamp,
    session_from_filename,
    split_fields,
    verify_framing,
)

REAL_42 = ("8=FIX.4.2\x019=69\x0135=A\x0149=AGENT\x0156=ORDERECHO\x0134=1\x01"
           "52=20260927-08:51:55.029\x0198=0\x01108=30\x0110=157\x01")


def good_message(msg_type="8", seq=2, version="FIX.4.2", **fields):
    """A genuinely well-framed message, so checksum flags mean something."""
    from datetime import datetime, timezone

    body = [(tag, value) for tag, value in fields.items()]
    raw = Codec(version).encode(
        msg_type, [(int(t), v) for t, v in body],
        sender_comp_id="ORDERECHO", target_comp_id="AGENT", seq_num=seq,
        sending_time=datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc),
    )
    return raw.decode("latin-1")


# --- framing helpers -------------------------------------------------------

def test_split_fields_keeps_order_and_repeats():
    pairs = split_fields("8=FIX.4.2\x0155=A\x0155=B\x01junk\x01=x\x01")
    assert pairs == [(8, "FIX.4.2"), (55, "A"), (55, "B")]


def test_verify_framing_accepts_a_real_message():
    raw = good_message()
    assert verify_framing(raw) == (False, False)


def test_verify_framing_spots_a_broken_checksum():
    raw = good_message()
    broken = raw[:-4] + "999" + SOH
    assert verify_framing(broken) == (False, True)


def test_verify_framing_spots_a_wrong_body_length():
    raw = good_message()
    declared = raw.split(SOH)[1]
    length = int(declared[2:])
    broken = raw.replace(f"9={length}{SOH}", f"9={length + 7}{SOH}", 1)
    assert verify_framing(broken)[0] is True


@pytest.mark.parametrize("stamp,expected_year", [
    ("20260927-08:51:55.029", 2026),
    ("20260927-08:51:55", 2026),
    ("2026-09-27T08:51:55.029Z", 2026),
    ("2026-09-27 08:51:55", 2026),
])
def test_parse_timestamp_handles_the_common_shapes(stamp, expected_year):
    parsed = parse_timestamp(stamp)
    assert parsed is not None and parsed.year == expected_year


def test_parse_timestamp_gives_up_quietly():
    assert parse_timestamp("yesterday afternoon") is None
    assert parse_timestamp("") is None


# 1 --- our own FIX log -----------------------------------------------------

def test_orderecho_log_lines(tmp_path):
    raw_in = good_message("A", 1)
    raw_out = good_message("8", 2, order_qty=None) if False else good_message("8", 2)
    lines = [
        f"20260927-08:51:55.029 IN   seq=1    35=A  {raw_in.replace(SOH, '|')}",
        f"20260927-08:51:55.041 OUT  seq=2    35=8  {raw_out.replace(SOH, '|')}",
    ]
    parser = LogParser()
    messages = list(parser.parse_lines(lines, source="agent42_20260927.log",
                                       session="agent42"))
    assert [m.direction for m in messages] == [DIR_IN, DIR_OUT]
    assert [m.msg_type for m in messages] == ["A", "8"]
    assert [m.seq for m in messages] == [1, 2]
    assert all(m.session == "agent42" for m in messages)
    assert all(not m.suspect for m in messages)
    assert messages[0].ts.hour == 8 and messages[0].ts.minute == 51


def test_orderecho_disc_line_keeps_its_reason():
    line = ("20260927-08:54:25.002 DISC -        -     "
            "8=FIX.4.2|9=99|35=D|10=000|  # Bad CheckSum (10)")
    message = LogParser().parse_line(line, 1)[0]
    assert message.direction == DIR_DISC
    assert message.comment == "Bad CheckSum (10)"
    assert message.bad_checksum is True          # kept, not dropped
    assert message.injected is False


def test_orderecho_injected_line_is_flagged():
    raw = good_message("8", 3).replace(SOH, "|")
    line = (f"20260927-08:51:55.041 OUT  seq=3    35=8  {raw}"
            "  # injected: set 9999=FOO")
    message = LogParser().parse_line(line, 1)[0]
    assert message.injected is True
    assert message.comment == "injected: set 9999=FOO"


def test_session_is_taken_from_our_log_file_name():
    assert session_from_filename("logs/fix/agent44_20260927.log") == "agent44"
    assert session_from_filename("logs/fix/ORDERECHO-AGENT_20260926.log") == \
        "ORDERECHO-AGENT"
    assert session_from_filename("messages.log") is None


# 1 --- evidence JSONL ------------------------------------------------------

def test_evidence_jsonl_messages_and_events():
    raw = good_message("8", 5).replace(SOH, "|")
    lines = [
        json.dumps({"ts": "2026-09-27T08:51:55.041Z", "kind": "out",
                    "session": "agent44", "seq": 5, "msg_type": "8",
                    "raw": raw, "injected": True, "detail": "replay of seq 5"}),
        json.dumps({"ts": "2026-09-27T08:51:55.100Z", "kind": "event",
                    "session": "agent44", "detail": "rule matched: ..."}),
        json.dumps({"kind": "discarded", "session": "agent44",
                    "raw": "8=FIX.4.2|9=99|35=D|10=000|",
                    "detail": "Bad CheckSum (10)"}),
    ]
    parser = LogParser()
    messages = list(parser.parse_lines(lines))
    assert [m.direction for m in messages] == [DIR_OUT, DIR_DISC]
    assert messages[0].session == "agent44"
    assert messages[0].injected is True
    assert messages[0].ts is not None
    assert parser.stats.events == 1          # the event line was counted


def test_json_that_is_not_evidence_falls_through_to_generic():
    line = '{"hello": "world"}'
    parser = LogParser()
    assert parser.parse_line(line, 1) == []
    assert parser.stats.skipped_lines == 1


# 1 --- generic logs --------------------------------------------------------

def test_quickfix_style_line_with_soh():
    raw = good_message("D", 4)
    line = f"20260927-08:51:55.029 : {raw}"
    message = LogParser().parse_line(line, 1)[0]
    assert message.msg_type == "D"
    assert message.ts is not None and message.ts.year == 2026
    assert message.direction == DIR_UNKNOWN


@pytest.mark.parametrize("delimiter", [SOH, "|", "^A"])
def test_every_delimiter_is_understood(delimiter):
    raw = good_message("D", 7).replace(SOH, delimiter)
    message = LogParser().parse_line(raw, 1)[0]
    assert message.msg_type == "D"
    assert message.seq == 7
    assert SOH in message.raw                # normalized on the way in


def test_several_messages_on_one_line():
    first = good_message("A", 1).replace(SOH, "|")
    second = good_message("8", 2).replace(SOH, "|")
    messages = LogParser().parse_line(first + second, 1)
    assert [m.msg_type for m in messages] == ["A", "8"]
    assert [m.seq for m in messages] == [1, 2]


def test_garbage_lines_are_counted_not_fatal():
    parser = LogParser()
    for line in ("", "   ", "this is not FIX", "8=NOTFIX|35=D|"):
        assert parser.parse_line(line, 1) == []
    assert parser.stats.skipped_lines == 2      # blank lines are not "skipped"
    assert parser.stats.messages == 0


def test_a_bad_checksum_message_is_kept_and_flagged():
    raw = good_message("8", 9)
    broken = (raw[:-4] + "001" + SOH).replace(SOH, "|")
    parser = LogParser()
    message = parser.parse_line(broken, 1)[0]
    assert message.msg_type == "8"              # still usable
    assert message.bad_checksum is True
    assert message.suspect is True
    assert parser.stats.bad_checksum == 1
    assert parser.stats.messages == 1


def test_direction_is_inferred_from_me():
    raw = good_message("8", 2)                  # 49=ORDERECHO 56=AGENT
    assert LogParser(me="ORDERECHO").parse_line(raw, 1)[0].direction == DIR_OUT
    assert LogParser(me="AGENT").parse_line(raw, 1)[0].direction == DIR_IN
    assert LogParser(me="SOMEONE").parse_line(raw, 1)[0].direction == DIR_UNKNOWN


def test_find_messages_ignores_text_around_them():
    raw = good_message("D", 3).replace(SOH, "|")
    found = find_messages(f"chatter before {raw} and after")
    assert len(found) == 1 and found[0][1] == "|"


def test_parsing_a_file_streams_and_names_it(tmp_path):
    path = tmp_path / "agent42_20260927.log"
    raw = good_message("D", 2).replace(SOH, "|")
    path.write_text(
        f"20260927-08:00:00.000 IN   seq=2    35=D  {raw}\n", encoding="utf-8")
    parser = LogParser()
    stream = parser.parse_file(str(path))
    assert hasattr(stream, "__next__")           # a generator, not a list
    messages = list(stream)
    assert len(messages) == 1
    assert messages[0].session == "agent42"
    assert messages[0].source == "agent42_20260927.log"
    assert messages[0].source_line_no == 1


def test_a_missing_file_is_not_fatal(tmp_path):
    parser = LogParser()
    assert list(parser.parse_file(str(tmp_path / "nope.log"))) == []


# 2 --- the dictionary ------------------------------------------------------

def test_the_real_dictionary_loads():
    dictionary = FixDictionary()
    assert dictionary.warnings == []
    assert len(dictionary) > 150
    assert dictionary.tag_name(55) == "Symbol"
    assert dictionary.tag_name(11) == "ClOrdID"


OVERLAY_TAGS = [35, 39, 150, 54, 40, 59, 20, 21, 103, 102, 434, 373, 380,
                141, 123, 43]

#: A base path that cannot exist, which is how the overlay-only path is
#: actually reached: the FIXReader file is missing.
NO_BASE = "/nonexistent/fix_tags.json"


def overlay_only() -> FixDictionary:
    return FixDictionary(base_path=NO_BASE)


@pytest.mark.parametrize("tag", OVERLAY_TAGS)
def test_overlay_guarantees_an_enum_for_every_tag_the_viewer_needs(tag):
    dictionary = FixDictionary()
    assert dictionary.is_enumerated(tag), tag
    # And with no base file at all, the overlay still has it.
    assert overlay_only().is_enumerated(tag), tag


@pytest.mark.parametrize("tag,value,expected", [
    (35, "D", "NewOrderSingle"), (39, "2", "Filled"),
    (150, "F", "Trade"), (54, "1", "Buy"), (40, "2", "Limit"),
    (59, "0", "Day"), (20, "0", "New"), (21, "1", "AutomatedPrivate"),
    (103, "3", "OrderExceedsLimit"), (102, "6", "DuplicateClOrdID"),
    (434, "2", "OrderCancelReplaceRequest"), (373, "1", "RequiredTagMissing"),
    (380, "3", "UnsupportedMessageType"), (141, "Y", "ResetSequenceNumbers"),
    (123, "Y", "GapFill"), (43, "Y", "PossibleDuplicate"),
])
def test_overlay_names_when_the_base_file_is_absent(tag, value, expected):
    assert overlay_only().enum_name(tag, value) == expected


def test_version_aware_exec_type():
    dictionary = FixDictionary()
    assert dictionary.enum_name(150, "1", "FIX.4.2") == "PartialFill"
    assert dictionary.enum_name(150, "2", "FIX.4.2") == "Fill"
    assert dictionary.enum_name(150, "F", "FIX.4.4") == "Trade"
    assert "deprecated" in dictionary.enum_name(150, "2", "FIX.4.4")


def test_version_aware_tag_32_name():
    dictionary = FixDictionary()
    assert dictionary.tag_name(32, "FIX.4.2") == "LastShares"
    assert dictionary.tag_name(32, "FIX.4.4") == "LastQty"
    assert dictionary.tag_name(32) == "LastQty"        # the modern default


def test_base_file_wins_where_it_has_a_value():
    dictionary = FixDictionary()
    # The base file names 54=2; the overlay would too. Base wins.
    assert dictionary.enum_name(54, "2") == "Sell"
    assert dictionary.tag_name(54) == "Side"


def test_a_missing_dictionary_warns_and_keeps_going(tmp_path):
    dictionary = FixDictionary(base_path=str(tmp_path / "absent.json"))
    assert dictionary.warnings
    assert "not found" in dictionary.warnings[0]
    assert dictionary.enum_name(35, "D") == "NewOrderSingle"   # overlay
    assert dictionary.tag_name(9999) == "Tag9999"              # never raises


def test_an_unreadable_dictionary_warns_and_keeps_going(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json at all", encoding="utf-8")
    dictionary = FixDictionary(base_path=str(bad))
    assert dictionary.warnings and "could not be read" in dictionary.warnings[0]
    assert dictionary.enum_name(39, "2") == "Filled"


def test_a_dictionary_of_the_wrong_shape_warns(tmp_path):
    odd = tmp_path / "odd.json"
    odd.write_text('{"tags": []}', encoding="utf-8")
    dictionary = FixDictionary(base_path=str(odd))
    assert dictionary.warnings and "not the expected list" in \
        dictionary.warnings[0]


def test_describe_reads_like_a_decoded_field():
    dictionary = FixDictionary()
    assert dictionary.describe(54, "1") == "54 Side = 1 (Buy)"
    assert dictionary.describe(11, "ABC") == "11 ClOrdID = ABC"


def test_the_shipped_dictionary_files_exist():
    assert os.path.exists(BASE_PATH)
    assert os.path.exists(OVERLAY_PATH)
