"""FIX message log and engine log."""

import os
import re
from datetime import datetime, timezone

import pytest

from orderecho_Clock import FakeClock
from orderecho_Codec import Codec, DiscardedFrame
from orderecho_Logging import EngineLog, FixMessageLog

START = datetime(2026, 9, 26, 14, 3, 22, 114000, tzinfo=timezone.utc)


def make_message(msg_type="1", seq=5, body=None, sender="AGENT",
                 target="ORDERECHO"):
    codec = Codec("FIX.4.2")
    raw = codec.encode(
        msg_type,
        body or [(112, "TEST-1")],
        sender_comp_id=sender,
        target_comp_id=target,
        seq_num=seq,
        sending_time=START,
    )
    return codec.decode(raw)[0]


def read_lines(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def test_fix_log_line_format_in_out_and_disc(tmp_path):
    clock = FakeClock(START)
    log = FixMessageLog(str(tmp_path), "ORDERECHO-AGENT", clock)

    inbound = make_message("1", seq=5)
    log.inbound(inbound)
    clock.advance(0.001)
    outbound = make_message("0", seq=6, sender="ORDERECHO", target="AGENT")
    log.outbound(outbound)
    clock.advance(2.887)
    log.discarded(DiscardedFrame(b"8=FIX.4.2\x019=99\x0135=D\x0110=000\x01",
                                 "bad checksum"))

    lines = read_lines(log.path)
    assert len(lines) == 3

    assert lines[0] == (
        "20260926-14:03:22.114 IN   seq=5    35=1  " + inbound.to_pipe()
    )
    assert lines[1] == (
        "20260926-14:03:22.115 OUT  seq=6    35=0  " + outbound.to_pipe()
    )
    assert lines[2] == (
        "20260926-14:03:25.002 DISC -        -     "
        "8=FIX.4.2|9=99|35=D|10=000|  # bad checksum"
    )

    # Columns line up and the raw message is always last.
    for line in lines:
        assert line.index("8=FIX.4.2") == 42
        assert re.match(r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{3} ", line)


def test_fix_log_path_and_directory(tmp_path):
    clock = FakeClock(START)
    log = FixMessageLog(str(tmp_path), "ORDERECHO-AGENT", clock)
    log.inbound(make_message())
    assert log.path == str(tmp_path / "fix" / "ORDERECHO-AGENT_20260926.log")


def test_soh_delimiter_writes_real_soh(tmp_path):
    clock = FakeClock(START)
    log = FixMessageLog(str(tmp_path), "ORDERECHO-AGENT", clock, delimiter="SOH")
    msg = make_message()
    log.inbound(msg)
    content = open(log.path, encoding="utf-8").read()
    assert "\x01" in content
    assert "|" not in content
    assert content.rstrip("\n").endswith(msg.raw.decode("latin-1"))


def test_fix_log_rolls_over_at_utc_midnight(tmp_path):
    clock = FakeClock(datetime(2026, 9, 26, 23, 59, 59, tzinfo=timezone.utc))
    log = FixMessageLog(str(tmp_path), "ORDERECHO-AGENT", clock)
    log.inbound(make_message(seq=1))
    first = log.path
    clock.advance(2)
    log.inbound(make_message(seq=2))
    second = log.path
    assert first != second
    assert first.endswith("_20260926.log")
    assert second.endswith("_20260927.log")
    assert len(read_lines(first)) == 1
    assert len(read_lines(second)) == 1


def test_engine_log_format_and_rollover(tmp_path):
    clock = FakeClock(datetime(2026, 9, 26, 23, 59, 59, tzinfo=timezone.utc))
    engine = EngineLog(str(tmp_path), "ORDERECHO-AGENT", clock, level="INFO")
    engine.info("Logon accepted, HeartBtInt=30, next_in=2 next_out=2")
    first = engine.path
    assert read_lines(first) == [
        "20260926-23:59:59.000 INFO    session  ORDERECHO-AGENT  "
        "Logon accepted, HeartBtInt=30, next_in=2 next_out=2"
    ]
    assert first.endswith(os.path.join("engine", "orderecho_20260926.log"))

    clock.advance(2)
    engine.warning("seq gap detected")
    second = engine.path
    assert second.endswith("orderecho_20260927.log")
    assert read_lines(second) == [
        "20260927-00:00:01.000 WARNING session  ORDERECHO-AGENT  seq gap detected"
    ]
    engine.close()


def test_engine_log_respects_level(tmp_path):
    clock = FakeClock(START)
    engine = EngineLog(str(tmp_path), "ORDERECHO-AGENT", clock, level="INFO")
    engine.debug("not written")
    engine.info("written")
    assert len(read_lines(engine.path)) == 1
    engine.close()


def test_engine_log_records_exceptions_with_stack_trace(tmp_path):
    clock = FakeClock(START)
    engine = EngineLog(str(tmp_path), "ORDERECHO-AGENT", clock, level="DEBUG")
    try:
        raise ValueError("boom")
    except ValueError:
        engine.exception("Unhandled error in session loop")
    content = open(engine.path, encoding="utf-8").read()
    assert "Unhandled error in session loop" in content
    assert "Traceback (most recent call last)" in content
    assert "ValueError: boom" in content
    engine.close()


def test_unwritable_log_dir_does_not_raise(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)          # read + execute, no write
    try:
        clock = FakeClock(START)
        fix_log = FixMessageLog(str(blocked), "ORDERECHO-AGENT", clock)
        fix_log.inbound(make_message())      # must not raise
        fix_log.inbound(make_message(seq=6))
        engine = EngineLog(str(blocked), "ORDERECHO-AGENT", clock)
        engine.info("still alive")           # must not raise
        engine.error("and still alive")
        engine.close()
        fix_log.close()
        assert "cannot open" in capsys.readouterr().err
    finally:
        os.chmod(blocked, 0o700)


def test_console_echo(tmp_path, capsys):
    clock = FakeClock(START)
    log = FixMessageLog(str(tmp_path), "ORDERECHO-AGENT", clock, console=True)
    log.inbound(make_message())
    assert "8=FIX.4.2|" in capsys.readouterr().out

    engine = EngineLog(str(tmp_path), "ORDERECHO-AGENT", clock, level="DEBUG",
                       console=True)
    engine.debug("quiet")
    engine.info("loud")
    out = capsys.readouterr().out
    assert "loud" in out
    assert "quiet" not in out
    engine.close()
