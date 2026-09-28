"""Demo client, driven against a live acceptor on a random port."""

import asyncio
import contextlib
import os
import re
import sys
import time
import io
import socket

import pytest

from fix_TestClient import FixTestClient
from orderecho_Clock import SystemClock
from orderecho_Config import load_config
from orderecho_DemoClient import ORD_TYPES, SIDES, main, parse_args
from orderecho_Transport import Transport
from orderecho_Transport import Transport

CONFIG_TEMPLATE = """
session:
  fix_version: FIX.4.2
  sender_comp_id: ORDERECHO
  target_comp_id: AGENT
  host: 127.0.0.1
  port: {port}
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10

storage:
  seqnum_dir: {tmp}/data/seqnums
  evidence_dir: {tmp}/data/evidence
  msgstore_dir: {tmp}/data/msgstore

logging:
  log_dir: {tmp}/logs
  fix_delimiter: "|"
  engine_level: INFO
  console: false

orders:
  default_delay_ms: 50
  send_pending_acks: false
  replace_ack_ordstatus: current

rules:
  - name: a-to-d-full
    match: {{ first_letter: "A-D" }}
    behavior: full_fill
  - name: h-to-j-hold
    match: {{ first_letter: "H-J" }}
    behavior: ack_only
  - name: default
    match: {{ any: true }}
    behavior: full_fill

pricing:
  mode: static
  provider: yfinance
  cache_seconds: 300
  timeout_sec: 3
  static:
    AAPL: 227.50
    default: 100.00
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _start_emulator(tmp_path, fix_version="FIX.4.2"):
    port = free_port()
    config_path = tmp_path / f"orderecho_{fix_version.replace('.', '')}.yaml"
    body = CONFIG_TEMPLATE.format(port=port, tmp=tmp_path)
    body = body.replace("fix_version: FIX.4.2", f"fix_version: {fix_version}")
    config_path.write_text(body, encoding="utf-8")
    return config_path


@pytest.fixture
async def running_emulator(tmp_path):
    config_path = await _start_emulator(tmp_path)
    transport = Transport(load_config(str(config_path)), clock=SystemClock())
    await transport.start()
    try:
        yield transport, str(config_path)
    finally:
        await transport.stop()
        transport.close_logs()


@pytest.fixture
async def running_emulator_44(tmp_path):
    """The same emulator, speaking FIX 4.4 (spec 8)."""
    config_path = await _start_emulator(tmp_path, "FIX.4.4")
    transport = Transport(load_config(str(config_path)), clock=SystemClock())
    await transport.start()
    try:
        yield transport, str(config_path)
    finally:
        await transport.stop()
        transport.close_logs()


async def run_demo_client(argv) -> tuple[int, str]:
    """Run the CLI in a worker thread (it owns its own event loop)."""
    buffer = io.StringIO()

    def go():
        with contextlib.redirect_stdout(buffer):
            return main(argv)

    code = await asyncio.to_thread(go)
    return code, buffer.getvalue()


# --- argument parsing ------------------------------------------------------

def test_side_and_ord_type_words_map_to_fix_values():
    args = parse_args(["order", "AAPL", "1000", "buy", "mkt"])
    assert args.side == SIDES["buy"] == "1"
    assert args.ord_type == ORD_TYPES["mkt"] == "1"
    assert args.price is None
    assert args.wait == 10.0        # spec 3.2: a maximum, not a sleep


def test_limit_order_requires_a_price():
    with pytest.raises(SystemExit):
        parse_args(["order", "EFG", "1000", "sell", "lmt"])


def test_limit_order_keeps_its_price_and_wait():
    args = parse_args(["order", "EFG", "1000", "sell", "lmt", "10.25",
                       "--wait", "5"])
    assert args.ord_type == "2"
    assert args.price == "10.25"
    assert args.wait == 5.0
    assert args.side == "2"


# --- against a live acceptor ----------------------------------------------

async def test_order_command_completes_and_reports_the_fill(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "order", "AAPL", "1000", "buy", "mkt",
         "--wait", "1"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "-> NewOrderSingle" in output
    assert "<- Logon" in output
    assert "exec=NEW" in output
    assert "exec=FILL" in output
    assert "status=FILLED" in output
    assert "last=1000@227.50" in output
    assert "cum=1000 leaves=0 avg=227.5000" in output
    assert "<- Logout" in output


async def test_limit_order_command(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "order", "EFG", "400", "sell", "lmt",
         "10.25", "--wait", "1"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "LMT 10.25" in output
    assert "exec=FILL" in output
    assert "last=400@10.25" in output


async def test_cancel_demo_command(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "cancel-demo", "HJK", "500", "buy", "mkt",
         "--wait", "1"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "-> NewOrderSingle" in output
    assert "-> OrderCancelRequest" in output
    assert "exec=NEW" in output
    assert "exec=CANCELED" in output
    assert "status=CANCELED" in output


async def test_client_logs_on_with_reset_so_seqnums_never_fight(running_emulator):
    """Two runs in a row both work, because each resets the sequence."""
    transport, config_path = running_emulator
    for _ in range(2):
        code, output = await run_demo_client(
            ["--config", config_path, "order", "AAPL", "10", "buy", "mkt",
             "--wait", "1"]
        )
        await transport.wait_for_session_end(timeout=5.0)
        assert code == 0
        assert "exec=FILL" in output


async def test_port_override_beats_the_config(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "--port", str(transport.port),
         "order", "AAPL", "5", "buy", "mkt", "--wait", "1"]
    )
    await transport.wait_for_session_end(timeout=5.0)
    assert code == 0
    assert f"Connecting to 127.0.0.1:{transport.port}" in output


async def test_refused_connection_exits_non_zero(tmp_path):
    config_path = tmp_path / "orderecho.yaml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(port=free_port(), tmp=tmp_path), encoding="utf-8"
    )
    code, _output = await run_demo_client(
        ["--config", str(config_path), "order", "AAPL", "1", "buy", "mkt",
         "--wait", "1"]
    )
    assert code == 1


# --- spec 3.2: --wait is a maximum, not a sleep ---------------------------

async def test_order_returns_as_soon_as_the_order_settles(running_emulator):
    """A generous --wait must not be waited out once the order is Filled."""
    transport, config_path = running_emulator
    begin = time.monotonic()
    code, output = await run_demo_client(
        ["--config", config_path, "order", "AAPL", "1000", "buy", "mkt",
         "--wait", "30"]
    )
    elapsed = time.monotonic() - begin
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "exec=FILL" in output
    assert "status=FILLED" in output
    assert "still waiting" not in output
    assert elapsed < 10          # not the 30 it was allowed


async def test_order_waits_out_the_maximum_when_nothing_settles(running_emulator):
    """An ack_only symbol never settles, so the maximum is the whole story."""
    transport, config_path = running_emulator
    begin = time.monotonic()
    code, output = await run_demo_client(
        ["--config", config_path, "order", "HJK", "500", "buy", "mkt",
         "--wait", "1"]
    )
    elapsed = time.monotonic() - begin
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "exec=NEW" in output
    assert "exec=FILL" not in output
    assert "still waiting for a terminal order state" in output
    assert elapsed >= 1.0


async def test_cancel_demo_returns_on_the_cancel_ack(running_emulator):
    transport, config_path = running_emulator
    begin = time.monotonic()
    code, output = await run_demo_client(
        ["--config", config_path, "cancel-demo", "HJK", "500", "buy", "mkt",
         "--wait", "30"]
    )
    elapsed = time.monotonic() - begin
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "exec=CANCELED" in output
    assert elapsed < 10


async def test_a_report_for_someone_elses_order_does_not_end_the_wait(
        running_emulator):
    """The client waits for *its* order, not the first report it happens to see."""
    transport, config_path = running_emulator

    # Leave a working order behind from an earlier session.
    first = FixTestClient()
    await first.connect("127.0.0.1", transport.port)
    await first.logon(30, reset=True)
    assert (await first.recv()).msg_type == "A"
    await first.send("D", [(11, "STALE-1"), (21, "1"), (55, "HJK"), (54, "1"),
                           (38, "100"), (40, "1"),
                           (60, "20260927-00:00:00.000")])
    assert (await first.recv_until("8")).get(150) == "0"
    await first.close()
    await transport.wait_for_session_end(timeout=5.0)

    # That order is still open; the demo client must ignore reports about it.
    code, output = await run_demo_client(
        ["--config", config_path, "order", "HJK", "500", "buy", "mkt",
         "--wait", "1"]
    )
    await transport.wait_for_session_end(timeout=5.0)
    assert code == 0
    assert "still waiting for a terminal order state" in output


# ===========================================================================
# Cook 4: session mode (spec 7, tests 10.23 / 10.24)
# ===========================================================================

class ScriptedStdin:
    """Feeds the client typed commands, one readline at a time.

    An entry may be a plain string, or a callable taking the output produced
    so far -- which is how a test can cancel an order whose ClOrdID the client
    only invented at runtime.
    """

    def __init__(self, lines, output):
        self._lines = list(lines)
        self._output = output

    def readline(self):
        if not self._lines:
            return ""                      # EOF ends the command loop
        entry = self._lines.pop(0)
        if callable(entry):
            entry = entry(self._output)
        return entry + "\n"


async def run_session_client(argv, script) -> tuple[int, str]:
    buffer = io.StringIO()

    def go():
        stdin = ScriptedStdin(script, buffer)
        with contextlib.redirect_stdout(buffer):
            real_stdin, sys.stdin = sys.stdin, stdin
            try:
                return main(argv)
            finally:
                sys.stdin = real_stdin

    code = await asyncio.to_thread(go)
    return code, buffer.getvalue()


def wait_for_text(marker, timeout=10.0):
    """A script step that pauses until *marker* shows up in the output."""
    def step(buffer):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in buffer.getvalue():
                break
            time.sleep(0.05)
        return "status"
    return step


def wait_for_cl_ord_id(buffer, timeout=10.0):
    """Block until the client has printed a ClOrdID, then return a cancel."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Spec 3.6: DEMO-<unix ms>-<counter>, with optional -C / -R suffix.
        found = re.findall(r"11=(DEMO-\d+-\d+)", buffer.getvalue())
        if found:
            return f"cancel {found[0]}"
        time.sleep(0.05)
    return "status"                        # fall back rather than hang


async def test_session_mode_takes_typed_commands(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "session"],
        ["order HJK 500 buy mkt", wait_for_cl_ord_id, "status", "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "commands:" in output                   # the help banner
    assert "-> NewOrderSingle      HJK 500 BUY MKT" in output
    assert "exec=NEW" in output
    assert "-> OrderCancelRequest" in output
    assert "exec=CANCELED" in output
    assert "status=CANCELED" in output
    assert "<- Logout" in output


async def test_session_mode_reports_status_and_unknown_commands(
        running_emulator):
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "session"],
        ["status", "wibble", "help", "order", "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "has not sent any orders yet" in output
    assert "unknown command 'wibble'" in output
    assert "usage: order" in output


async def test_session_mode_can_send_a_resend_request(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "session"],
        ["order AAPL 100 buy mkt", "resend 1 0", "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "-> ResendRequest       7=1 16=0" in output


async def test_no_exit_stays_connected_after_the_order_settles(
        running_emulator):
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "order", "AAPL", "1000", "buy", "mkt",
         "--wait", "10", "--no-exit"],
        ["status", "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "exec=FILL" in output
    assert "staying connected (--no-exit)" in output
    assert "FILLED" in output                      # from `status`
    assert "<- Logout" in output


async def test_without_no_exit_the_order_command_still_exits(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "order", "AAPL", "1000", "buy", "mkt",
         "--wait", "10"]
    )
    await transport.wait_for_session_end(timeout=5.0)
    assert code == 0
    assert "staying connected" not in output


# --- both FIX versions (spec 8, test 9.13) --------------------------------

async def test_order_command_speaks_the_configured_version(running_emulator_44):
    transport, config_path = running_emulator_44
    version = transport.config.session.fix_version
    code, output = await run_demo_client(
        ["--config", config_path, "order", "AAPL", "1000", "buy", "mkt",
         "--wait", "20"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert f"[{version}]" in output
    assert "status=FILLED" in output
    # 4.4 folds both fill kinds into Trade; 4.2 keeps FILL.
    assert ("exec=TRADE" if version == "FIX.4.4" else "exec=FILL") in output

    with open(transport.fix_log.path, encoding="utf-8") as handle:
        log = handle.read()
    assert f"8={version}|" in log
    if version == "FIX.4.4":
        assert "|20=" not in log


async def test_session_mode_over_fix_44(running_emulator_44):
    transport, config_path = running_emulator_44
    code, output = await run_session_client(
        ["--config", config_path, "--fix", "4.4", "session"],
        ["order AAPL 100 buy mkt", wait_for_text("exec=TRADE"), "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "[FIX.4.4]" in output
    assert "exec=TRADE" in output
    assert "status=FILLED" in output
    assert "FILLED" in output          # from `status`


async def test_a_44_client_against_a_42_acceptor_is_shown_the_door(
        running_emulator):
    """Spec 5.5, from the client's side."""
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "--fix", "4.4", "session"],
        ["quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)
    # The Logon is answered with a Logout, so the client never gets going and
    # says so with a non-zero exit.
    assert code == 1
    assert "Incorrect BeginString, expected FIX.4.2" in output
    assert "no Logon reply" in output


async def test_status_shows_sent_orders_before_any_report(running_emulator):
    transport, config_path = running_emulator
    code, output = await run_session_client(
        ["--config", config_path, "session"],
        ["order HJK 500 buy mkt", "status", "quit"],
    )
    await transport.wait_for_session_end(timeout=5.0)
    assert code == 0
    # HJK is ack_only, so it is listed with whatever state it has reached.
    assert "HJK" in output


# ===========================================================================
# Cook 6: sessions, Ctrl+C, option order (spec 3.1, 3.2, 8; test 9.12)
# ===========================================================================

MULTI_TEMPLATE = """
engine:
  host: 127.0.0.1
  fix_port: {port42}
  default_session: agent42

defaults:
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10
  orders: {{ default_delay_ms: 50 }}
  rules:
    - {{ name: default, match: {{ any: true }}, behavior: full_fill }}

sessions:
  - {{ id: agent42, fix_version: FIX.4.2, sender_comp_id: ORDERECHO, target_comp_id: AGENT }}
  - {{ id: agent44, fix_version: FIX.4.4, sender_comp_id: ORDERECHO, target_comp_id: AGENT }}
  - {{ id: strict-broker, fix_version: FIX.4.2, sender_comp_id: STRICTBRK, target_comp_id: AGENT, port: {port_strict} }}

storage:
  seqnum_dir: {tmp}/data/seqnums
  evidence_dir: {tmp}/data/evidence
  msgstore_dir: {tmp}/data/msgstore

logging:
  log_dir: {tmp}/logs
  fix_delimiter: "|"
  engine_level: DEBUG
  console: false

pricing:
  mode: static
  provider: yfinance
  cache_seconds: 300
  timeout_sec: 3
  warm_symbols: []
  static:
    AAPL: 227.50
    default: 100.00
"""


@pytest.fixture
async def multi_emulator(tmp_path):
    port42 = free_port()
    port_strict = free_port()
    config_path = tmp_path / "multi.yaml"
    config_path.write_text(
        MULTI_TEMPLATE.format(port42=port42, port_strict=port_strict,
                              tmp=tmp_path),
        encoding="utf-8",
    )
    transport = Transport(load_config(str(config_path)), clock=SystemClock())
    await transport.start()
    try:
        yield transport, str(config_path)
    finally:
        await transport.stop()
        transport.close_logs()


async def test_session_option_picks_that_sessions_identity(multi_emulator):
    transport, config_path = multi_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "--session", "agent44",
         "order", "AAPL", "100", "buy", "mkt", "--wait", "20"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "session=agent44" in output
    assert "[FIX.4.4]" in output
    assert "exec=TRADE" in output          # 4.4 naming
    assert transport.runtime("agent44").message_store.path.endswith(
        "agent44.jsonl"
    )
    assert len(transport.runtime("agent42").messages) == 0


async def test_session_option_finds_a_session_on_its_own_port(multi_emulator):
    transport, config_path = multi_emulator
    code, output = await run_demo_client(
        ["--config", config_path, "--session", "strict-broker",
         "order", "AAPL", "100", "buy", "mkt", "--wait", "20"]
    )
    await transport.wait_for_session_end(timeout=5.0)

    assert code == 0
    assert "session=strict-broker" in output
    strict_port = transport.config.session_by_id("strict-broker").port
    assert f"127.0.0.1:{strict_port}" in output
    assert "as AGENT" in output            # mirrored CompIDs


async def test_global_options_are_accepted_after_the_subcommand(
        multi_emulator):
    transport, config_path = multi_emulator
    code, output = await run_demo_client(
        ["order", "AAPL", "100", "buy", "mkt", "--wait", "20",
         "--config", config_path, "--session", "agent44"]
    )
    await transport.wait_for_session_end(timeout=5.0)
    assert code == 0
    assert "session=agent44" in output
    assert "[FIX.4.4]" in output


def test_unknown_session_id_is_reported(tmp_path):
    from orderecho_DemoClient import resolve_endpoint

    config_path = tmp_path / "multi.yaml"
    config_path.write_text(
        MULTI_TEMPLATE.format(port42=1, port_strict=2, tmp=tmp_path),
        encoding="utf-8",
    )
    config = load_config(str(config_path))
    args = parse_args(["--config", str(config_path), "--session", "ghost",
                       "session"])
    with pytest.raises(SystemExit) as caught:
        resolve_endpoint(config, args)
    assert "ghost" in str(caught.value)
    assert "agent42" in str(caught.value)


async def test_ctrl_c_in_session_mode_logs_out(multi_emulator):
    """Spec 3.1: SIGINT takes the same path as `quit`."""
    import signal
    import subprocess
    import sys as _sys

    transport, config_path = multi_emulator
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    client = subprocess.Popen(
        [_sys.executable, "orderecho_DemoClient.py", "--config", config_path,
         "--session", "agent42", "session"],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    try:
        runtime = transport.runtime("agent42")
        for _ in range(100):
            if runtime.session_active:
                break
            await asyncio.sleep(0.1)
        assert runtime.session_active, "the demo client never logged on"

        client.send_signal(signal.SIGINT)
        output = await asyncio.to_thread(client.communicate, None, 30)
        stdout = output[0]

        assert client.returncode == 0, stdout
        assert "Ctrl+C" in stdout
        assert "<- Logout" in stdout

        await runtime.wait_for_session_end(timeout=10.0)
        inbound = [row for row in runtime.messages
                   if row["kind"] == "in" and row["msg_type"] == "5"]
        assert inbound, "the engine never saw a Logout"
        assert "58=Demo client done" in inbound[0]["raw"]
    finally:
        if client.poll() is None:
            client.kill()
            client.wait(timeout=10)
