"""Messages from several files, back in the order they happened (spec 4.1)."""

from datetime import datetime, timezone

import pytest

from orderecho_LogParse import LogParser, merge_chronologically


def at(stamp: str, text: str = "8=FIX.4.2|9=5|35=0|10=000|") -> str:
    return f"{stamp} OUT  seq=1 35=0 {text}"


def parse(name: str, lines) -> list:
    """Parse as if from one file, so each message remembers where it came."""
    parser = LogParser()
    return list(parser.parse_lines(lines, source=name, session=name))


def stamps(messages) -> list:
    return [message.ts.strftime("%H:%M:%S") if message.ts else None
            for message in messages]


def sources(messages) -> list:
    return [message.source for message in messages]


def test_two_files_interleave_by_time():
    first = parse("a.log", [at("20260927-09:00:01.000"),
                            at("20260927-09:00:05.000")])
    second = parse("b.log", [at("20260927-09:00:02.000"),
                             at("20260927-09:00:03.000")])
    merged = merge_chronologically(first + second)
    assert stamps(merged) == ["09:00:01", "09:00:02", "09:00:03", "09:00:05"]
    assert sources(merged) == ["a.log", "b.log", "b.log", "a.log"]


def test_reading_order_does_not_change_the_answer():
    first = parse("a.log", [at("20260927-09:00:01.000"),
                            at("20260927-09:00:09.000")])
    second = parse("b.log", [at("20260927-09:00:04.000")])
    assert stamps(merge_chronologically(first + second)) == \
        stamps(merge_chronologically(second + first))


def test_equal_timestamps_keep_the_order_they_were_read_in():
    same = "20260927-09:00:01.000"
    first = parse("a.log", [at(same), at(same)])
    second = parse("b.log", [at(same)])
    merged = merge_chronologically(first + second)
    assert sources(merged) == ["a.log", "a.log", "b.log"]
    # ... and the other way round, because the sort is stable, not clever.
    merged = merge_chronologically(second + first)
    assert sources(merged) == ["b.log", "a.log", "a.log"]


def test_a_file_never_shuffles_within_itself():
    lines = [at(f"20260927-09:00:0{n}.000") for n in (1, 2, 3)]
    messages = parse("a.log", lines)
    assert merge_chronologically(messages) == messages


def test_an_unstamped_line_stays_with_the_line_above_it():
    """A QuickFIX-style log has no timestamp on a continuation line."""
    parser = LogParser()
    first = list(parser.parse_lines(
        ["20260927-09:00:01.000 OUT  seq=1 35=0 8=FIX.4.2|9=5|35=0|10=000|",
         "8=FIX.4.2|9=5|35=1|10=000|"],           # no stamp of its own
        source="a.log", session="a"))
    second = parse("b.log", [at("20260927-09:00:02.000")])
    assert first[1].ts is None

    merged = merge_chronologically(first + second)
    assert [m.source for m in merged] == ["a.log", "a.log", "b.log"]
    assert merged[1] is first[1]                  # still after its own line


def test_an_unstamped_head_goes_before_its_own_first_stamp():
    parser = LogParser()
    messages = list(parser.parse_lines(
        ["8=FIX.4.2|9=5|35=1|10=000|",            # no stamp yet
         "20260927-09:00:05.000 OUT  seq=1 35=0 8=FIX.4.2|9=5|35=0|10=000|"],
        source="a.log", session="a"))
    other = parse("b.log", [at("20260927-09:00:01.000")])

    merged = merge_chronologically(messages + other)
    # It inherits 09:00:05, so it lands after b's 09:00:01 and before its own.
    assert [m.source for m in merged] == ["b.log", "a.log", "a.log"]
    assert merged[1].ts is None


def test_a_file_with_no_timestamps_at_all_goes_last():
    """Nothing places it, so it is not allowed to claim it came first."""
    stamped = parse("a.log", [at("20260927-09:00:01.000")])
    blind = parse("b.log", ["8=FIX.4.2|9=5|35=1|10=000|"])
    merged = merge_chronologically(blind + stamped)
    assert sources(merged) == ["a.log", "b.log"]


def test_merging_nothing_is_nothing():
    assert merge_chronologically([]) == []


def test_view_merges_across_files(tmp_path, capsys):
    from orderecho_LogView import main

    (tmp_path / "later_20260927.log").write_text(
        at("20260927-09:00:09.000") + "\n", encoding="utf-8")
    (tmp_path / "earlier_20260927.log").write_text(
        at("20260927-09:00:01.000") + "\n", encoding="utf-8")

    import io
    out = io.StringIO()
    # `later` sorts first by name, so unmerged output would print it first.
    assert main(["view", str(tmp_path / "*.log"), "--no-color"], out=out) == 0
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert "earlier" in lines[0] and "later" in lines[1]


def test_stats_spans_every_file_whatever_the_order(tmp_path):
    import io

    from orderecho_LogView import main

    (tmp_path / "later_20260927.log").write_text(
        at("20260927-09:00:09.000") + "\n", encoding="utf-8")
    (tmp_path / "earlier_20260927.log").write_text(
        at("20260927-09:00:01.000") + "\n", encoding="utf-8")

    out = io.StringIO()
    assert main(["stats", str(tmp_path / "*.log"), "--no-color"], out=out) == 0
    text = out.getvalue()
    assert "09:00:01" in text.split("first:")[1].split("\n")[0]
    assert "09:00:09" in text.split("last :")[1].split("\n")[0]
