"""The viewer's command line (spec 6) and its performance (spec 9.7)."""

import io
import os
import re
import time

import pytest

from orderecho_LogView import main
from test_Timeline import line, report

ANSI = re.compile(r"\033\[[0-9;]*m")

CHAIN = [
    line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "1000",
                               40: "2", 44: "10.00", 21: "1",
                               60: "20260927-09:00:00.000"}),
    report(2, "0", "0"),
    report(3, "1", "1", cum="400", leaves="600", last_qty="400",
           last_px="10.00", avg="10.0000"),
    report(4, "2", "2", cum="1000", leaves="0", last_qty="600",
           last_px="10.00", avg="10.0000"),
    line("D", 5, "IN", fields={11: "OTHER", 55: "MSFT", 54: "2", 38: "50",
                               40: "1", 21: "1",
                               60: "20260927-09:00:00.000"}),
    line("j", 6, fields={45: "5", 372: "D", 380: "3",
                         58: "Not supported in this build"}),
]


def write_log(tmp_path, lines=None, name="agent42_20260927.log"):
    path = tmp_path / name
    path.write_text("\n".join(lines or CHAIN) + "\n", encoding="utf-8")
    return str(path)


def run(args):
    out = io.StringIO()
    code = main(args, out=out)
    return code, out.getvalue()


# --- view ------------------------------------------------------------------

def test_view_lists_every_message(tmp_path):
    path = write_log(tmp_path)
    code, text = run(["view", path, "--no-color"])
    assert code == 0
    body = [row for row in text.splitlines() if row.strip()]
    assert len(body) == 6
    assert "New Order" in text and "Execution Report" in text
    assert "Buy 1000 AAPL" in text
    assert "Trade" in text or "Fill" in text


def test_view_has_no_ansi_when_told_not_to(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["view", path, "--no-color"])
    assert ANSI.search(text) is None


def test_view_filters_by_message_type(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["view", path, "--no-color", "--msg-type", "D"])
    assert len([r for r in text.splitlines() if r.strip()]) == 2
    assert "Execution Report" not in text


def test_view_filters_by_direction_and_symbol(tmp_path):
    path = write_log(tmp_path)
    _code, inbound = run(["view", path, "--no-color", "--dir", "in"])
    assert len([r for r in inbound.splitlines() if r.strip()]) == 2

    _code, msft = run(["view", path, "--no-color", "--symbol", "MSFT"])
    assert "MSFT" in msft
    assert "AAPL" not in msft


def test_view_filters_by_rejects_and_grep(tmp_path):
    path = write_log(tmp_path)
    _code, rejects = run(["view", path, "--no-color", "--rejects"])
    assert "Business Message Reject" in rejects or "j " in rejects
    assert len([r for r in rejects.splitlines() if r.strip()]) == 1

    _code, grepped = run(["view", path, "--no-color", "--grep", "MSFT"])
    assert len([r for r in grepped.splitlines() if r.strip()]) == 1


def test_view_clordid_shows_the_whole_chain(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["view", path, "--no-color", "--clordid", "C1"])
    rows = [row for row in text.splitlines() if row.strip()]
    assert len(rows) == 4              # the D and its three reports
    assert "MSFT" not in text


def test_view_decode_names_every_field(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["view", path, "--no-color", "--msg-type", "D",
                       "--clordid", "C1", "--decode"])
    assert "ClOrdID" in text
    assert "Side" in text and "(Buy)" in text
    assert "OrdType" in text and "(Limit)" in text
    assert "BeginString" in text


def test_view_counts_unparseable_lines(tmp_path):
    path = write_log(tmp_path, CHAIN + ["this line is not FIX at all"])
    _code, text = run(["view", path, "--no-color"])
    assert "1 unparseable line(s) skipped" in text


def test_view_reports_a_missing_file(tmp_path):
    code, _text = run(["view", str(tmp_path / "nope.log"), "--no-color"])
    assert code == 2


# --- timeline --------------------------------------------------------------

def test_timeline_prints_the_chain_and_passes(tmp_path):
    path = write_log(tmp_path)
    code, text = run(["timeline", path, "--clordid", "C1", "--no-color"])
    assert code == 0
    assert "Order chain for C1" in text
    assert "Checks" in text
    assert text.count("[PASS]") == 11
    assert "verdict: PASS" in text


def test_timeline_exit_code_1_on_a_warning(tmp_path):
    lonely = [line("D", 1, "IN", fields={11: "C9", 55: "AAPL", 54: "1",
                                         38: "1", 40: "1", 21: "1",
                                         60: "20260927-09:00:00.000"})]
    path = write_log(tmp_path, lonely)
    code, text = run(["timeline", path, "--clordid", "C9", "--no-color"])
    assert code == 1
    assert "[WARN]" in text
    assert "verdict: WARN" in text


def test_timeline_exit_code_2_on_a_failure(tmp_path):
    broken = [
        report(1, "1", "1", cum="500", leaves="500", last_qty="500",
               last_px="10.00", avg="10.0000"),
        report(2, "1", "1", cum="400", leaves="600", last_qty="100",
               last_px="10.00", avg="10.0000"),
    ]
    path = write_log(tmp_path, broken)
    code, text = run(["timeline", path, "--clordid", "C1", "--no-color"])
    assert code == 2
    assert "[FAIL]" in text
    assert "verdict: FAIL" in text


def test_timeline_needs_an_identifier(tmp_path):
    path = write_log(tmp_path)
    code, _text = run(["timeline", path, "--no-color"])
    assert code == 2


def test_timeline_says_so_when_nothing_matches(tmp_path):
    path = write_log(tmp_path)
    code, text = run(["timeline", path, "--clordid", "GHOST", "--no-color"])
    assert code == 2
    assert "no messages found" in text


# --- stats -----------------------------------------------------------------

def test_stats_counts_everything(tmp_path):
    path = write_log(tmp_path, CHAIN + ["not a fix line"])
    code, text = run(["stats", path, "--no-color"])
    assert code == 0
    assert "6 message(s) from 1 file(s)" in text
    assert "By session" in text and "agent42" in text
    assert "By message type" in text
    assert "unparseable lines 1" in text
    # 4 out, 2 in
    directions = text.split("By direction")[1].split("Rejects")[0]
    assert "out" in directions and "in" in directions


def test_stats_respects_filters(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["stats", path, "--no-color", "--msg-type", "8"])
    assert "3 message(s)" in text


def test_stats_counts_rejects_by_reason(tmp_path):
    path = write_log(tmp_path)
    _code, text = run(["stats", path, "--no-color"])
    rejects = text.split("Rejects by reason")[1].split("Other")[0]
    assert "380=3" in rejects


# --- follow ----------------------------------------------------------------

def test_follow_picks_up_appended_lines_and_a_new_day_file(tmp_path):
    """--follow tails the file and notices tomorrow's log appearing."""
    import threading

    path = write_log(tmp_path, CHAIN[:2])
    out = io.StringIO()
    stop = threading.Event()

    def run_follow():
        # A short interval so the test does not dawdle.
        main(["view", path, "--no-color", "--follow", "--interval", "0.05"],
             out=out)

    thread = threading.Thread(target=run_follow, daemon=True)
    thread.start()
    time.sleep(0.4)
    assert "New Order" in out.getvalue()

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(CHAIN[3] + "\n")
        handle.flush()
    deadline = time.time() + 5
    while time.time() < deadline and "cum=1000" not in out.getvalue():
        time.sleep(0.05)
    assert "cum=1000" in out.getvalue(), out.getvalue()

    # Tomorrow's file, same session prefix.
    tomorrow = tmp_path / "agent42_20260928.log"
    tomorrow.write_text(
        line("D", 9, "IN", fields={11: "NEXTDAY", 55: "TSLA", 54: "1",
                                   38: "7", 40: "1", 21: "1",
                                   60: "20260928-09:00:00.000"}) + "\n",
        encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline and "TSLA" not in out.getvalue():
        time.sleep(0.05)
    assert "TSLA" in out.getvalue(), out.getvalue()
    stop.set()


def test_follow_survives_the_file_being_truncated(tmp_path):
    import threading

    path = write_log(tmp_path, CHAIN[:2])
    out = io.StringIO()

    thread = threading.Thread(
        target=lambda: main(
            ["view", path, "--no-color", "--follow", "--interval", "0.05"],
            out=out),
        daemon=True)
    thread.start()
    time.sleep(0.4)

    with open(path, "w", encoding="utf-8") as handle:     # truncate
        handle.write(CHAIN[4] + "\n")
    deadline = time.time() + 5
    while time.time() < deadline and "MSFT" not in out.getvalue():
        time.sleep(0.05)
    assert "MSFT" in out.getvalue(), out.getvalue()


# --- 7: performance --------------------------------------------------------

def test_stats_over_a_hundred_thousand_messages(tmp_path):
    """Streaming, so a big file is a matter of time, not memory."""
    import tracemalloc

    path = tmp_path / "big_20260927.log"
    template = report(2, "1", "1", cum="400", leaves="600", last_qty="400",
                      last_px="10.00", avg="10.0000")
    with open(path, "w", encoding="utf-8") as handle:
        for _ in range(100_000):
            handle.write(template + "\n")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    assert size_mb > 15, "the generated log should be substantial"

    # Timed without tracemalloc, which costs about 4x and would be
    # measuring the profiler rather than the parser.
    begin = time.monotonic()
    code, text = run(["stats", str(path), "--no-color"])
    elapsed = time.monotonic() - begin

    assert code == 0
    assert "100000 message(s)" in text
    assert elapsed < 15, f"stats took {elapsed:.1f}s over {size_mb:.0f}MB"

    # Memory measured in its own pass: streaming means the peak has nothing
    # to do with how big the file is.
    tracemalloc.start()
    run(["stats", str(path), "--no-color"])
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20 * 1024 * 1024, (
        f"peak was {peak / 1e6:.0f}MB for a {size_mb:.0f}MB file")


# --- Cook 8: replace and cancel rows say what they are changing (spec 4.3) --

def chain_log(tmp_path):
    """new 500 @10.00 -> replace to 800 @10.50 -> cancel, then a reject."""
    from test_Timeline import line, report

    lines = [
        line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "500",
                                   40: "2", 44: "10.00", 21: "1",
                                   60: "20260927-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="C1", order_qty="500", leaves="500"),
        line("G", 3, "IN", fields={11: "C2", 41: "C1", 55: "AAPL", 54: "1",
                                   38: "800", 40: "2", 44: "10.50", 21: "1",
                                   60: "20260927-09:00:01.000"}),
        report(4, "5", "0", cl_ord_id="C2", order_qty="800", leaves="800",
               extra={41: "C1"}),
        line("F", 5, "IN", fields={11: "C3", 41: "C2", 55: "AAPL", 54: "1",
                                   60: "20260927-09:00:02.000"}),
        line("9", 6, "OUT", fields={11: "C3", 41: "C2", 37: "O-1", 39: "0",
                                    434: "1", 102: "1",
                                    58: "Unknown order"}),
    ]
    path = tmp_path / "chain_20260927.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_a_replace_row_shows_what_changed(tmp_path):
    code, text = run(["view", chain_log(tmp_path), "--no-color"])
    assert code == 0
    assert "Replace C1->C2 qty 500->800 px 10.00->10.50" in text


def test_a_cancel_row_names_both_ids(tmp_path):
    _code, text = run(["view", chain_log(tmp_path), "--no-color"])
    assert "Cancel C2->C3" in text


def test_a_cancel_reject_row_names_the_reason(tmp_path):
    _code, text = run(["view", chain_log(tmp_path), "--no-color"])
    row = next(line for line in text.splitlines() if "Order Cancel Reject" in line)
    assert "to=Order Cancel Request" in row        # 434=1, by name
    assert "reason=Unknown order" in row           # 102=1, by name
    assert "11=C3" in row and "41=C2" in row


def test_a_replace_with_no_history_states_what_it_wants(tmp_path):
    """Half a log is normal; the row should still say something useful."""
    from test_Timeline import line

    path = tmp_path / "half_20260927.log"
    path.write_text(line("G", 3, "IN", fields={
        11: "C2", 41: "C1", 55: "AAPL", 54: "1", 38: "800", 40: "2",
        44: "10.50", 21: "1", 60: "20260927-09:00:01.000"}) + "\n",
        encoding="utf-8")
    _code, text = run(["view", str(path), "--no-color"])
    assert "Replace C1->C2 qty 800 px 10.50" in text


def test_a_rejected_replace_is_not_taken_as_the_new_truth(tmp_path):
    """A refused request never happened, so it is not the order's state."""
    from test_Timeline import line, report

    lines = [
        line("D", 1, "IN", fields={11: "C1", 55: "AAPL", 54: "1", 38: "500",
                                   40: "2", 44: "10.00", 21: "1",
                                   60: "20260927-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="C1", order_qty="500", leaves="500"),
        line("G", 3, "IN", fields={11: "C2", 41: "C1", 55: "AAPL", 54: "1",
                                   38: "900", 40: "2", 44: "99.00", 21: "1",
                                   60: "20260927-09:00:01.000"}),
        report(4, "8", "8", cl_ord_id="C2", order_qty="900", leaves="0",
               extra={41: "C1", 103: "3"}),
        line("G", 5, "IN", fields={11: "C3", 41: "C1", 55: "AAPL", 54: "1",
                                   38: "700", 40: "2", 44: "10.25", 21: "1",
                                   60: "20260927-09:00:02.000"}),
    ]
    path = tmp_path / "rejected_20260927.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _code, text = run(["view", str(path), "--no-color"])
    # The second replace is measured against the original, not the refused one.
    assert "Replace C1->C3 qty 500->700 px 10.00->10.25" in text


# --- Cook 8: framing flags on timeline rows (spec 4.4) ---------------------

def tampered_log(tmp_path):
    """Edit a CumQty in place, exactly as a careless sed would."""
    from test_Timeline import line, report

    lines = [
        line("D", 1, "IN", fields={11: "T1", 55: "AAPL", 54: "1", 38: "1000",
                                   40: "2", 44: "10.00", 21: "1",
                                   60: "20260927-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="T1"),
        report(3, "1", "1", cl_ord_id="T1", cum="400", leaves="600",
               last_qty="400", last_px="10.00", avg="10.0000"),
    ]
    path = tmp_path / "tampered_20260927.log"
    path.write_text("\n".join(lines).replace("|14=400|", "|14=401|") + "\n",
                    encoding="utf-8")
    return str(path)


def test_a_tampered_message_is_flagged_on_its_timeline_row(tmp_path):
    code, text = run(["timeline", tampered_log(tmp_path), "--no-color",
                      "--clordid", "T1"])
    assert code == 2                       # the arithmetic is wrong too
    row = next(line for line in text.splitlines() if "401" in line)
    assert "[BAD-CHECKSUM]" in row


def test_framing_intact_warns_and_says_where(tmp_path):
    _code, text = run(["timeline", tampered_log(tmp_path), "--no-color",
                       "--clordid", "T1"])
    check = next(line for line in text.splitlines() if "framing_intact" in line)
    assert "[WARN]" in check
    assert "bad CheckSum" in check
    assert "seq=3" in check
    assert "tampered_20260927.log:3" in check


def test_framing_intact_passes_on_an_untouched_log(tmp_path):
    from test_Timeline import line, report

    path = tmp_path / "clean_20260927.log"
    path.write_text("\n".join([
        line("D", 1, "IN", fields={11: "K1", 55: "AAPL", 54: "1", 38: "1000",
                                   40: "2", 44: "10.00", 21: "1",
                                   60: "20260927-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="K1"),
    ]) + "\n", encoding="utf-8")
    code, text = run(["timeline", str(path), "--no-color", "--clordid", "K1"])
    assert code == 0
    assert "[PASS] framing_intact: all 2 message(s) correctly framed" in text


def test_an_injected_message_is_flagged_on_its_timeline_row(tmp_path):
    from test_Timeline import line, report

    path = tmp_path / "injected_20260927.log"
    rows = [
        line("D", 1, "IN", fields={11: "J1", 55: "AAPL", 54: "1", 38: "1000",
                                   40: "2", 44: "10.00", 21: "1",
                                   60: "20260927-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="J1") + "  # injected: removed 60",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    _code, text = run(["timeline", str(path), "--no-color", "--clordid", "J1"])
    row = next(line for line in text.splitlines() if "Execution Report" in line)
    assert "[INJECTED]" in row
