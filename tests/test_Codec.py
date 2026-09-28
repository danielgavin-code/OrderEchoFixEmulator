"""Codec: framing, BodyLength/CheckSum validation, round trips."""

from datetime import datetime, timezone

import pytest

from orderecho_Codec import Codec, DiscardedFrame, FixMsg, format_time, to_pipe

SENDING_TIME = datetime(2026, 9, 26, 14, 3, 22, 114000, tzinfo=timezone.utc)


def make_codec():
    return Codec("FIX.4.2")


def encode_logon(codec, seq=1):
    return codec.encode(
        "A",
        [(98, "0"), (108, "30")],
        sender_comp_id="ORDERECHO",
        target_comp_id="AGENT",
        seq_num=seq,
        sending_time=SENDING_TIME,
    )


def test_sending_time_format():
    assert format_time(SENDING_TIME) == "20260926-14:03:22.114"


def test_round_trip_preserves_fields_and_order():
    codec = make_codec()
    raw = codec.encode(
        "1",
        [(112, "TEST-1"), (58, "hello")],
        sender_comp_id="ORDERECHO",
        target_comp_id="AGENT",
        seq_num=7,
        sending_time=SENDING_TIME,
    )
    messages = codec.decode(raw)
    assert len(messages) == 1
    msg = messages[0]
    assert isinstance(msg, FixMsg)
    assert msg.msg_type == "1"
    assert msg.raw == raw
    tags = [tag for tag, _ in msg.pairs]
    assert tags == [8, 9, 35, 49, 56, 34, 52, 112, 58, 10]
    assert msg.get(112) == "TEST-1"
    assert msg.get(58) == "hello"
    assert msg.seq_num == 7
    # fields() is an ordered list of pairs, so order and repeats survive.
    assert msg.fields()[:3] == [
        ["8", "FIX.4.2"], ["9", msg.get(9)], ["35", "1"]
    ]
    assert msg.fields()[-1] == ["10", msg.get(10)]


def test_header_field_order_with_possdup():
    codec = make_codec()
    raw = codec.encode(
        "4",
        [(123, "Y"), (36, "9")],
        sender_comp_id="ORDERECHO",
        target_comp_id="AGENT",
        seq_num=4,
        sending_time=SENDING_TIME,
        poss_dup=True,
        orig_sending_time=SENDING_TIME,
    )
    tags = [int(field.split("=")[0]) for field in to_pipe(raw).split("|") if field]
    assert tags == [8, 9, 35, 49, 56, 34, 52, 43, 122, 123, 36, 10]


def test_body_length_and_checksum_against_hand_computed_message():
    # Hand-built message: body is everything after 9=NN<SOH> up to 10=.
    body = (
        b"35=0\x0149=ORDERECHO\x0156=AGENT\x0134=2\x01"
        b"52=20260926-14:03:22.114\x01"
    )
    head = b"8=FIX.4.2\x019=" + str(len(body)).encode() + b"\x01"
    checksum = sum(head + body) % 256
    expected = head + body + b"10=" + f"{checksum:03d}".encode() + b"\x01"

    codec = make_codec()
    raw = codec.encode(
        "0",
        [],
        sender_comp_id="ORDERECHO",
        target_comp_id="AGENT",
        seq_num=2,
        sending_time=SENDING_TIME,
    )
    assert raw == expected
    assert codec.decode(raw)[0].get(9) == str(len(body))


def test_bad_checksum_is_discarded():
    codec = make_codec()
    raw = encode_logon(codec)
    bad = raw[:-4] + b"000\x01"
    out = codec.decode(bad)
    assert len(out) == 1
    assert isinstance(out[0], DiscardedFrame)
    assert "CheckSum" in out[0].reason
    assert out[0].raw == bad


def test_bad_body_length_is_discarded():
    codec = make_codec()
    raw = encode_logon(codec)
    declared = raw.split(b"\x01")[1]
    real_len = int(declared[2:])
    bad = raw.replace(b"9=" + str(real_len).encode() + b"\x01",
                      b"9=" + str(real_len + 5).encode() + b"\x01", 1)
    out = codec.decode(bad)
    assert len(out) == 1
    assert isinstance(out[0], DiscardedFrame)
    assert "BodyLength" in out[0].reason
    assert out[0].raw == bad


def test_short_body_length_is_discarded():
    codec = make_codec()
    raw = encode_logon(codec)
    declared = raw.split(b"\x01")[1]
    real_len = int(declared[2:])
    bad = raw.replace(b"9=" + str(real_len).encode() + b"\x01",
                      b"9=" + str(real_len - 4).encode() + b"\x01", 1)
    out = codec.decode(bad)
    assert len(out) == 1
    assert isinstance(out[0], DiscardedFrame)
    assert "BodyLength" in out[0].reason


def test_discarded_frame_does_not_break_the_stream():
    codec = make_codec()
    good = encode_logon(codec, seq=2)
    bad = encode_logon(codec, seq=1)[:-4] + b"000\x01"
    out = codec.decode(bad + good)
    assert isinstance(out[0], DiscardedFrame)
    assert isinstance(out[1], FixMsg)
    assert out[1].seq_num == 2


def test_two_messages_in_one_chunk():
    codec = make_codec()
    first = encode_logon(codec, seq=1)
    second = encode_logon(codec, seq=2)
    out = codec.decode(first + second)
    assert [m.seq_num for m in out] == [1, 2]


def test_one_message_split_across_three_chunks():
    codec = make_codec()
    raw = encode_logon(codec, seq=3)
    third = len(raw) // 3
    assert codec.decode(raw[:third]) == []
    assert codec.decode(raw[third:2 * third]) == []
    out = codec.decode(raw[2 * third:])
    assert len(out) == 1
    assert out[0].seq_num == 3
    assert out[0].raw == raw


def test_to_pipe_replaces_soh():
    codec = make_codec()
    raw = encode_logon(codec)
    piped = to_pipe(raw)
    assert "\x01" not in piped
    assert piped.startswith("8=FIX.4.2|9=")
    assert piped.endswith("|")


def test_leading_garbage_is_discarded():
    codec = make_codec()
    raw = encode_logon(codec)
    out = codec.decode(b"junkjunk" + raw)
    assert isinstance(out[0], DiscardedFrame)
    assert out[0].raw == b"junkjunk"
    assert isinstance(out[1], FixMsg)
