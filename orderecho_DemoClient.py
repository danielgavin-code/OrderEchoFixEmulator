"""Demo FIX client.

A hand-driven initiator so orders can be sent at the emulator before the Go
agent exists.  It is deliberately simple: it is not part of the emulator's
pure core and makes no attempt to be a general FIX engine.

    python orderecho_DemoClient.py order AAPL 1000 buy mkt
    python orderecho_DemoClient.py order EFG 1000 sell lmt 10.25 --wait 5
    python orderecho_DemoClient.py cancel-demo HJK 500 buy mkt
    python orderecho_DemoClient.py order ZWZZT 100 buy lmt 10.00 --tif ioc
    python orderecho_DemoClient.py query DEMO-123-1 ZWZZT buy
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from datetime import datetime, timezone

from orderecho_Codec import Codec, DiscardedFrame, FixMsg, format_time
from orderecho_Config import ConfigError, load_config

DEFAULT_CONFIG = os.path.join("config", "orderecho.yaml")
DEFAULT_HEART_BT_INT = 30

SIDES = {"buy": "1", "sell": "2", "sellshort": "5", "sellshortexempt": "6"}
TIFS = {"day": "0", "gtc": "1", "ioc": "3", "fok": "4", "gtx": "5", "gtd": "6"}
ORD_TYPES = {"mkt": "1", "market": "1", "lmt": "2", "limit": "2"}

MSG_TYPE_NAMES = {
    "0": "Heartbeat", "1": "TestRequest", "2": "ResendRequest", "3": "Reject",
    "4": "SequenceReset", "5": "Logout", "8": "ExecutionReport",
    "9": "OrderCancelReject", "A": "Logon", "j": "BusinessMessageReject",
    "H": "OrderStatusRequest",
}
EXEC_TYPE_NAMES = {
    "0": "NEW", "1": "PARTIAL_FILL", "2": "FILL", "4": "CANCELED",
    "5": "REPLACE", "6": "PENDING_CANCEL", "8": "REJECTED",
    "E": "PENDING_REPLACE",
    "F": "TRADE",           # FIX 4.4 folds both fill kinds into Trade
    "3": "DONE_FOR_DAY", "A": "PENDING_NEW", "C": "EXPIRED",
    "I": "ORDER_STATUS",    # FIX 4.4's answer to an OrderStatusRequest
}

FIX_VERSIONS = {"4.2": "FIX.4.2", "4.4": "FIX.4.4",
                "FIX.4.2": "FIX.4.2", "FIX.4.4": "FIX.4.4"}
ORD_STATUS_NAMES = {
    "0": "NEW", "1": "PARTIALLY_FILLED", "2": "FILLED", "4": "CANCELED",
    "5": "REPLACED", "6": "PENDING_CANCEL", "8": "REJECTED",
    "E": "PENDING_REPLACE",
    "3": "DONE_FOR_DAY", "A": "PENDING_NEW", "C": "EXPIRED",
}

#: An order in one of these states will never change again, so there is
#: nothing left to wait for.
TERMINAL_ORD_STATUS = frozenset({"2", "4", "8", "3", "C"})


def describe(msg: FixMsg) -> str:
    """One readable line for an inbound message."""
    name = MSG_TYPE_NAMES.get(msg.msg_type, f"MsgType={msg.msg_type}")
    parts = [f"<- {name:<20}"]
    if msg.get(43) == "Y":
        # A replayed or duplicated message; say so rather than let it look
        # like fresh news.
        parts.append("(PossDup)")

    if msg.msg_type == "8":
        exec_type = EXEC_TYPE_NAMES.get(msg.get(150), msg.get(150))
        status = ORD_STATUS_NAMES.get(msg.get(39), msg.get(39))
        parts.append(f"{msg.get(55)} exec={exec_type} status={status}")
        if msg.get(32) and msg.get(32) != "0":
            parts.append(f"last={msg.get(32)}@{msg.get(31)}")
        parts.append(
            f"cum={msg.get(14)} leaves={msg.get(151)} avg={msg.get(6)}"
        )
        parts.append(f"11={msg.get(11)} 37={msg.get(37)}")
        if msg.get(20) == "3":
            parts.append("(status answer, 20=3)")
        if msg.get(59) and msg.get(59) != "0":
            parts.append(f"59={msg.get(59)}")
        if msg.get(103):
            parts.append(f"103={msg.get(103)}")
    elif msg.msg_type == "9":
        parts.append(
            f"11={msg.get(11)} 41={msg.get(41)} 102={msg.get(102)} "
            f"434={msg.get(434)}"
        )
    elif msg.msg_type == "A":
        parts.append(f"HeartBtInt={msg.get(108)}")
    elif msg.msg_type == "1":
        parts.append(f"TestReqID={msg.get(112)}")

    if msg.get(58):
        parts.append(f'"{msg.get(58)}"')
    return "  ".join(part for part in parts if part)


class DemoClient:
    def __init__(self, host: str, port: int, sender_comp_id: str,
                 target_comp_id: str, heart_bt_int: int = DEFAULT_HEART_BT_INT,
                 out=None, fix_version: str = "FIX.4.2") -> None:
        self.host = host
        self.port = port
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.heart_bt_int = heart_bt_int
        self.out = out or sys.stdout

        self.fix_version = fix_version
        self.codec = Codec(fix_version)
        self.next_out = 1
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.last_sent = time.monotonic()
        self.received: list = []
        self.logged_on = asyncio.Event()
        self.logged_out = asyncio.Event()
        # Our own ClOrdIDs, so an ExecutionReport for somebody else's order
        # (a working order from an earlier session, say) is not mistaken for
        # the one we are waiting on.
        self.my_cl_ord_ids: set = set()
        #: ClOrdID -> what we know about that order, for `status` and for
        #: filling in symbol/side on a cancel or replace.
        self.my_orders: dict = {}
        #: Bumped for every ClOrdID, so two orders in the same millisecond
        #: still get different ids.
        self._cl_ord_seq = 0
        self.terminal = asyncio.Event()
        self.cancel_settled = asyncio.Event()
        self._ack_waiters: list = []
        self._tasks: list = []

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    # --------------------------------------------------------------- wire

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port
        )
        self._tasks.append(asyncio.create_task(self._read_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

    async def send(self, msg_type: str, body_fields=None) -> None:
        raw = self.codec.encode(
            msg_type, body_fields or [],
            sender_comp_id=self.sender_comp_id,
            target_comp_id=self.target_comp_id,
            seq_num=self.next_out,
            sending_time=datetime.now(timezone.utc),
        )
        self.next_out += 1
        self.last_sent = time.monotonic()
        self.writer.write(raw)
        await self.writer.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    break
                for item in self.codec.decode(data):
                    if isinstance(item, DiscardedFrame):
                        self.say(f"<- (discarded frame: {item.reason})")
                        continue
                    await self._handle(item)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self.say(f"<- read error: {exc}")

    async def _handle(self, msg: FixMsg) -> None:
        self.received.append(msg)
        if msg.msg_type != "0":          # heartbeats are noise at this level
            self.say(describe(msg))

        if msg.msg_type == "A":
            self.logged_on.set()
        elif msg.msg_type == "1":
            await self.send("0", [(112, msg.get(112))])
        elif msg.msg_type == "5":
            self.logged_out.set()
        elif msg.msg_type == "8":
            for waiter in list(self._ack_waiters):
                if not waiter.done():
                    waiter.set_result(msg)
                self._ack_waiters.remove(waiter)
            if msg.get(11) in self.my_cl_ord_ids:
                self._remember_report(msg)
                if msg.get(39) in TERMINAL_ORD_STATUS:
                    self.terminal.set()
                if msg.get(150) == "4":
                    self.cancel_settled.set()
        elif msg.msg_type == "9":
            if msg.get(11) in self.my_cl_ord_ids:
                self.cancel_settled.set()

    def next_cl_ord_id(self, suffix: str = "") -> str:
        self._cl_ord_seq += 1
        return f"DEMO-{int(time.time() * 1000)}-{self._cl_ord_seq}{suffix}"

    def _remember_report(self, msg) -> None:
        """Keep the latest state of an order we sent, keyed by its ClOrdID."""
        cl_ord_id = msg.get(11)
        orig = msg.get(41)
        known = self.my_orders.get(orig) if orig else None
        entry = self.my_orders.setdefault(cl_ord_id, dict(known or {}))
        entry.update({
            "cl_ord_id": cl_ord_id,
            "order_id": msg.get(37) or entry.get("order_id"),
            "symbol": msg.get(55) or entry.get("symbol"),
            "side": msg.get(54) or entry.get("side"),
            "ord_status": msg.get(39),
            "cum_qty": msg.get(14),
            "leaves_qty": msg.get(151),
            "avg_px": msg.get(6),
        })
        if orig and orig in self.my_orders:
            # The chain moved on; keep the old id pointing at the same order.
            self.my_orders[orig] = entry

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if time.monotonic() - self.last_sent >= self.heart_bt_int:
                    await self.send("0")
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ session

    async def logon(self, timeout: float = 10.0) -> bool:
        # 141=Y so a demo run never fights persisted sequence numbers.
        await self.send("A", [(98, "0"), (108, str(self.heart_bt_int)),
                              (141, "Y")])
        try:
            await asyncio.wait_for(self.logged_on.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            self.say("!! no Logon reply; giving up")
            return False

    async def logout(self, timeout: float = 5.0) -> None:
        await self.send("5", [(58, "Demo client done")])
        try:
            await asyncio.wait_for(self.logged_out.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None

    # -------------------------------------------------------------- orders

    async def wait_for(self, event: asyncio.Event, seconds: float,
                       what: str) -> bool:
        """Wait up to *seconds* for *event*.  True if it happened."""
        try:
            await asyncio.wait_for(event.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            self.say(f".. still waiting for {what} after {seconds:g}s; "
                     f"giving up")
            return False

    def next_ack(self) -> asyncio.Future:
        waiter = asyncio.get_running_loop().create_future()
        self._ack_waiters.append(waiter)
        return waiter

    async def send_status_request(self, cl_ord_id: str, symbol: str,
                                  side: str, order_id: str | None = None) -> None:
        """OrderStatusRequest (35=H) for one of our orders, or any ClOrdID."""
        fields = [(11, cl_ord_id), (55, symbol), (54, side)]
        if order_id:
            fields.insert(0, (37, order_id))
        self.my_cl_ord_ids.add(cl_ord_id)
        self.say(f"-> OrderStatusRequest  11={cl_ord_id} {symbol}"
                 + (f" 37={order_id}" if order_id else ""))
        await self.send("H", fields)

    async def send_order(self, symbol: str, qty: int, side: str,
                         ord_type: str, price=None, tif: str | None = None,
                         expire_time: str | None = None,
                         expire_date: str | None = None,
                         account: str | None = None) -> str:
        cl_ord_id = self.next_cl_ord_id()
        self.my_cl_ord_ids.add(cl_ord_id)
        # Listed from the moment it is sent, not the moment it is acked.
        self.my_orders[cl_ord_id] = {
            "cl_ord_id": cl_ord_id, "order_id": None, "symbol": symbol,
            "side": side, "ord_status": None, "cum_qty": None,
            "leaves_qty": str(qty), "avg_px": None,
        }
        fields = [
            (11, cl_ord_id), (21, "1"), (55, symbol), (54, side),
            (38, str(qty)), (40, ord_type),
            (60, format_time(datetime.now(timezone.utc))),
        ]
        if price is not None:
            fields.append((44, str(price)))
        if tif is not None:
            fields.append((59, tif))
        if expire_time is not None:
            fields.append((126, expire_time))
        if expire_date is not None:
            fields.append((432, expire_date))
        if account is not None:
            fields.insert(0, (1, account))
        extra = ""
        if tif is not None:
            names = {code: name.upper() for name, code in TIFS.items()}
            extra += f" {names.get(tif, '59=' + tif)}"
        if expire_time is not None:
            extra += f" until {expire_time}"
        if expire_date is not None:
            extra += f" until {expire_date}"
        if account is not None:
            extra += f" account={account}"
        self.say(
            f"-> NewOrderSingle      {symbol} {qty} "
            f"{'BUY' if side == '1' else 'SELL'} "
            f"{'MKT' if ord_type == '1' else f'LMT {price}'}{extra}  "
            f"11={cl_ord_id}"
        )
        await self.send("D", fields)
        return cl_ord_id

    async def send_cancel(self, orig_cl_ord_id: str, symbol: str,
                          side: str) -> str:
        cl_ord_id = self.next_cl_ord_id("-C")
        self.my_cl_ord_ids.add(cl_ord_id)
        self.say(f"-> OrderCancelRequest  41={orig_cl_ord_id}  11={cl_ord_id}")
        await self.send("F", [
            (11, cl_ord_id), (41, orig_cl_ord_id), (55, symbol), (54, side),
            (60, format_time(datetime.now(timezone.utc))),
        ])
        return cl_ord_id


    async def send_replace(self, orig_cl_ord_id: str, qty: int,
                           price=None) -> str | None:
        known = self.my_orders.get(orig_cl_ord_id)
        if known is None:
            self.say(f"!! unknown ClOrdID {orig_cl_ord_id}; "
                     f"try `status` to see what this client has sent")
            return None
        cl_ord_id = self.next_cl_ord_id("-R")
        self.my_cl_ord_ids.add(cl_ord_id)
        fields = [
            (11, cl_ord_id), (41, orig_cl_ord_id), (21, "1"),
            (55, known.get("symbol")), (54, known.get("side")),
            (38, str(qty)), (40, "2" if price is not None else "1"),
            (60, format_time(datetime.now(timezone.utc))),
        ]
        if price is not None:
            fields.append((44, str(price)))
        self.say(f"-> OrderCancelReplaceRequest  41={orig_cl_ord_id} "
                 f"11={cl_ord_id} qty={qty}"
                 + (f" px={price}" if price is not None else ""))
        await self.send("G", fields)
        return cl_ord_id

    async def send_resend_request(self, begin: int, end: int = 0) -> None:
        self.say(f"-> ResendRequest       7={begin} 16={end}")
        await self.send("2", [(7, str(begin)), (16, str(end))])

    def print_status(self) -> None:
        if not self.my_orders:
            self.say("   (this client has not sent any orders yet)")
            return
        seen = []
        for entry in self.my_orders.values():
            key = entry.get("order_id") or entry.get("cl_ord_id")
            if key in seen:
                continue
            seen.append(key)
            status = ORD_STATUS_NAMES.get(entry.get("ord_status"),
                                          entry.get("ord_status"))
            if entry.get("ord_status") is None:
                status = "SENT"            # sent, nothing heard back yet
            self.say(
                f"   {entry.get('cl_ord_id'):<24} {entry.get('symbol', '?'):<8} "
                f"{status:<18} cum={entry.get('cum_qty')} "
                f"leaves={entry.get('leaves_qty')} avg={entry.get('avg_px')} "
                f"({entry.get('order_id')})"
            )


HELP = """commands:
  order <SYM> <QTY> <buy|sell> <mkt|lmt> [PX] [tif=<day|gtc|ioc|fok|gtx|gtd>]
        [expire=<YYYYMMDD-HH:MM:SS>] [account=<ACCT>]
                                                send a NewOrderSingle
  query <ClOrdID>                               OrderStatusRequest (35=H)
  cancel <ClOrdID>                              cancel one of your orders
  replace <ClOrdID> <QTY> [PX]                  replace qty (and price)
  resend <begin> [end]                          send a ResendRequest
  status                                        orders this client has sent
  help                                          this text
  quit                                          log out and exit"""


async def _stdin_stream(loop):
    """An asyncio reader over stdin, or None if stdin is not a real pipe.

    Reading stdin through the event loop rather than a worker thread is what
    lets Ctrl+C interrupt a prompt: a thread blocked in readline() cannot be
    cancelled, and would hold up the exit.
    """
    try:
        fileno = sys.stdin.fileno()
    except Exception:
        return None
    if not os.isatty(fileno) and not hasattr(sys.stdin, "buffer"):
        return None
    try:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        return reader
    except Exception:
        return None


async def read_stdin_lines(queue: asyncio.Queue) -> None:
    """Feed stdin lines to the command loop without blocking the event loop."""
    loop = asyncio.get_running_loop()
    stream = await _stdin_stream(loop)
    if stream is not None:
        while True:
            raw = await stream.readline()
            if not raw:
                await queue.put(None)          # EOF
                return
            await queue.put(raw.decode("utf-8", "replace").rstrip("\n"))
        return
    # Scripted or otherwise unusual stdin: fall back to a worker thread.
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            await queue.put(None)
            return
        await queue.put(line.rstrip("\n"))


@contextlib.contextmanager
def interrupt_handler(stop: asyncio.Event, client: DemoClient):
    """First Ctrl+C asks for a clean logout; a second one gives up waiting."""
    loop = asyncio.get_running_loop()
    installed = []
    pressed = {"count": 0}

    def on_interrupt():
        pressed["count"] += 1
        if pressed["count"] == 1:
            client.say("\n(Ctrl+C) logging out; press again to exit now")
            stop.set()
        else:
            client.say("\n(Ctrl+C) exiting")
            raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, on_interrupt)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    try:
        yield stop
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass


async def command_loop(client: DemoClient, prompt: bool = True) -> None:
    """Read commands until `quit`, EOF or Ctrl+C."""
    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.create_task(read_stdin_lines(queue))
    stop = asyncio.Event()
    client.say(HELP)
    try:
        with interrupt_handler(stop, client):
            while True:
                if prompt:
                    print("> ", end="", flush=True)
                getter = asyncio.create_task(queue.get())
                stopper = asyncio.create_task(stop.wait())
                done, _pending = await asyncio.wait(
                    {getter, stopper}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    getter.cancel()
                    return
                stopper.cancel()
                line = getter.result()
                if line is None:
                    return
                line = line.strip()
                if not line:
                    continue
                if not await handle_command(client, line):
                    return
    finally:
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass


async def handle_command(client: DemoClient, line: str) -> bool:
    """Run one typed command.  False means 'stop the loop'."""
    parts = line.split()
    command, args = parts[0].lower(), parts[1:]

    if command in ("quit", "exit"):
        return False
    if command == "help":
        client.say(HELP)
        return True
    if command == "status":
        client.print_status()
        return True

    if command == "order":
        if len(args) < 4:
            client.say("!! usage: order <SYM> <QTY> <buy|sell> <mkt|lmt> [PX]")
            return True
        options = {}
        positional = []
        for arg in args:
            key, sep, value = arg.partition("=")
            if sep and key.lower() in ("tif", "expire", "account"):
                options[key.lower()] = value
            else:
                positional.append(arg)
        args = positional
        if len(args) < 4:
            client.say("!! usage: order <SYM> <QTY> <buy|sell> <mkt|lmt> [PX]")
            return True
        symbol, qty, side, ord_type = args[0], args[1], args[2], args[3]
        price = args[4] if len(args) > 4 else None
        tif = options.get("tif")
        if tif is not None and tif.lower() not in TIFS:
            client.say(f"!! tif must be one of {', '.join(TIFS)}")
            return True
        if side.lower() not in SIDES or ord_type.lower() not in ORD_TYPES:
            client.say(f"!! side must be one of {', '.join(sorted(SIDES))}; "
                       f"type one of {', '.join(sorted(ORD_TYPES))}")
            return True
        if ORD_TYPES[ord_type.lower()] == "2" and price is None:
            client.say("!! a limit order needs a price")
            return True
        try:
            quantity = int(qty)
        except ValueError:
            client.say(f"!! quantity must be a whole number, got {qty!r}")
            return True
        await client.send_order(symbol.upper(), quantity,
                                SIDES[side.lower()], ORD_TYPES[ord_type.lower()],
                                price if ORD_TYPES[ord_type.lower()] == "2"
                                else None,
                                tif=TIFS[tif.lower()] if tif else None,
                                expire_time=options.get("expire"),
                                account=options.get("account"))
        return True

    if command == "query":
        if not args:
            client.say("!! usage: query <ClOrdID>")
            return True
        known = client.my_orders.get(args[0], {})
        if not known:
            client.say(f"!! unknown ClOrdID {args[0]}; try `status`")
            return True
        await client.send_status_request(args[0], known.get("symbol"),
                                         known.get("side"),
                                         known.get("order_id"))
        return True

    if command == "cancel":
        if not args:
            client.say("!! usage: cancel <ClOrdID>")
            return True
        known = client.my_orders.get(args[0])
        if known is None:
            client.say(f"!! unknown ClOrdID {args[0]}; try `status`")
            return True
        await client.send_cancel(args[0], known.get("symbol"), known.get("side"))
        return True

    if command == "replace":
        if len(args) < 2:
            client.say("!! usage: replace <ClOrdID> <QTY> [PX]")
            return True
        try:
            quantity = int(args[1])
        except ValueError:
            client.say(f"!! quantity must be a whole number, got {args[1]!r}")
            return True
        await client.send_replace(args[0], quantity,
                                  args[2] if len(args) > 2 else None)
        return True

    if command == "resend":
        if not args:
            client.say("!! usage: resend <begin> [end]")
            return True
        try:
            begin = int(args[0])
            end = int(args[1]) if len(args) > 1 else 0
        except ValueError:
            client.say("!! begin and end must be whole numbers")
            return True
        await client.send_resend_request(begin, end)
        return True

    client.say(f"!! unknown command {command!r}; try `help`")
    return True


# ---------------------------------------------------------------- commands


async def run_session(client: DemoClient, args) -> int:
    """Stay connected and take typed commands until `quit` or Ctrl+C."""
    await client.connect()
    if not await client.logon():
        return 1
    client.say("Connected. Inbound messages print as they arrive.")
    try:
        await command_loop(client)
    except (KeyboardInterrupt, asyncio.CancelledError):
        client.say("")
    await client.logout()
    await client.close()
    return 0


async def run_order(client: DemoClient, args) -> int:
    await client.connect()
    if not await client.logon():
        return 1
    await client.send_order(args.symbol, args.qty, args.side, args.ord_type,
                            args.price, tif=args.tif,
                            expire_time=args.expire_time,
                            expire_date=args.expire_date,
                            account=args.account)
    # --wait is a ceiling, not a sleep: stop as soon as the order is settled.
    await client.wait_for(client.terminal, args.wait, "a terminal order state")
    if getattr(args, "no_exit", False):
        client.say("Order settled; staying connected (--no-exit).")
        try:
            await command_loop(client)
        except (KeyboardInterrupt, asyncio.CancelledError):
            client.say("")
    await client.logout()
    await client.close()
    return 0


async def run_cancel_demo(client: DemoClient, args) -> int:
    await client.connect()
    if not await client.logon():
        return 1
    waiter = client.next_ack()
    cl_ord_id = await client.send_order(args.symbol, args.qty, args.side,
                                        args.ord_type, args.price,
                                        tif=args.tif,
                                        expire_time=args.expire_time,
                                        expire_date=args.expire_date,
                                        account=args.account)
    try:
        await asyncio.wait_for(waiter, timeout=10.0)
    except asyncio.TimeoutError:
        client.say("!! no ExecutionReport for the order; cancelling anyway")
    await client.send_cancel(cl_ord_id, args.symbol, args.side)
    await client.wait_for(client.cancel_settled, args.wait,
                          "the cancel to be acked or rejected")
    if getattr(args, "no_exit", False):
        client.say("Cancel settled; staying connected (--no-exit).")
        try:
            await command_loop(client)
        except (KeyboardInterrupt, asyncio.CancelledError):
            client.say("")
    await client.logout()
    await client.close()
    return 0


async def run_query(client: DemoClient, args) -> int:
    """One OrderStatusRequest, its answer printed, then log out."""
    await client.connect()
    if not await client.logon():
        return 1
    waiter = client.next_ack()
    await client.send_status_request(args.cl_ord_id, args.symbol, args.side,
                                     args.order_id)
    try:
        await asyncio.wait_for(waiter, timeout=args.wait)
    except asyncio.TimeoutError:
        client.say(f".. no answer within {args.wait:g}s")
    await client.logout()
    await client.close()
    return 0


def _add_global_options(parser, suppress: bool = False) -> None:
    """The options that may appear before *or* after the subcommand."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", default=DEFAULT_CONFIG if not suppress
                        else argparse.SUPPRESS)
    parser.add_argument("--host", default=default,
                        help="override config host")
    parser.add_argument("--port", type=int, default=default,
                        help="override config port")
    parser.add_argument("--fix", choices=["4.2", "4.4"], default=default,
                        help="FIX version to speak (default: from the config)")
    parser.add_argument("--session", default=default,
                        help="use this session id from the engine config")


def build_parser() -> argparse.ArgumentParser:
    """The command line, on its own, so the docs can be checked against it."""
    parser = argparse.ArgumentParser(
        prog="orderecho_DemoClient.py",
        description="Send demo orders to a running OrderEchoFixEmulator",
    )
    _add_global_options(parser)

    # The same options on every subparser, defaulting to SUPPRESS so that an
    # option given before the subcommand is not wiped out by one that was not
    # given after it.
    globals_after = argparse.ArgumentParser(add_help=False)
    _add_global_options(globals_after, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("order", "send one NewOrderSingle"),
                            ("cancel-demo", "send an order, then cancel it")):
        cmd = sub.add_parser(name, help=help_text, parents=[globals_after])
        cmd.add_argument("symbol")
        cmd.add_argument("qty", type=int)
        cmd.add_argument("side", choices=sorted(SIDES))
        cmd.add_argument("ord_type", choices=sorted(ORD_TYPES))
        cmd.add_argument("price", nargs="?", default=None)
        cmd.add_argument("--wait", type=float, default=10.0,
                         help="maximum seconds to wait for the order to "
                              "settle before logging out (default 10)")
        cmd.add_argument("--no-exit", action="store_true",
                         help="after the order settles, stay connected and "
                              "take typed commands until quit")
        cmd.add_argument("--tif", choices=list(TIFS), default=None,
                         help="TimeInForce (59); default: none sent (Day)")
        cmd.add_argument("--expire-time", default=None,
                         help="ExpireTime (126) for --tif gtd, "
                              "YYYYMMDD-HH:MM:SS UTC")
        cmd.add_argument("--expire-date", default=None,
                         help="ExpireDate (432) for --tif gtd, YYYYMMDD")
        cmd.add_argument("--account", default=None,
                         help="Account (1), for engines with "
                              "orders.valid_accounts")

    query = sub.add_parser("query", help="send an OrderStatusRequest (35=H)",
                           parents=[globals_after])
    query.add_argument("cl_ord_id")
    query.add_argument("symbol")
    query.add_argument("side", choices=sorted(SIDES))
    query.add_argument("--order-id", default=None,
                       help="OrderID (37), when you know it")
    query.add_argument("--wait", type=float, default=5.0,
                       help="maximum seconds to wait for the answer")

    sub.add_parser("session", help="stay connected and take typed commands",
                   parents=[globals_after])
    return parser


def parse_args(argv=None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("host", "port", "fix", "session"):
        if not hasattr(args, name):
            setattr(args, name, None)
    if not hasattr(args, "config"):
        args.config = DEFAULT_CONFIG

    if args.fix and args.session:
        parser.error("--fix and --session are mutually exclusive: the session "
                     "already says which FIX version it speaks")

    if args.command == "session":
        return args
    args.side = SIDES[args.side]
    if args.command == "query":
        return args
    if args.tif is not None:
        args.tif = TIFS[args.tif]
    args.ord_type = ORD_TYPES[args.ord_type]
    if args.ord_type == "2" and args.price is None:
        parser.error("a limit order needs a price, e.g. 'order EFG 100 buy lmt 10.25'")
    if args.ord_type == "1":
        args.price = None
    return args


def resolve_endpoint(config, args):
    """Work out who to be and where to connect.

    Without --session that is the config's own (only, or default) session;
    with it, the named one.  Either way the client mirrors the session: our
    SenderCompID is the session's target, and vice versa.
    """
    spec = None
    if args.session:
        spec = config.session_by_id(args.session)
        if spec is None:
            available = ", ".join(entry.id for entry in config.sessions)
            raise SystemExit(
                f"error: no session {args.session!r} in {args.config} "
                f"(have {available})"
            )
    else:
        spec = config.default_session_spec or config.sessions[0]

    host = args.host or spec.session.host
    port = args.port or spec.port
    fix_version = (FIX_VERSIONS[args.fix] if args.fix
                   else spec.fix_version)
    return spec, host, port, fix_version


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    spec, host, port, fix_version = resolve_endpoint(config, args)
    client = DemoClient(
        host, port,
        sender_comp_id=spec.target_comp_id,   # we are the counterparty
        target_comp_id=spec.sender_comp_id,
        fix_version=fix_version,
    )
    print(f"Connecting to {host}:{port} as {spec.target_comp_id} "
          f"[{fix_version}] session={spec.id}", flush=True)

    runner = {"cancel-demo": run_cancel_demo, "query": run_query,
              "session": run_session}.get(args.command, run_order)
    try:
        return asyncio.run(runner(client, args))
    except ConnectionRefusedError:
        print(f"error: nothing is listening on {host}:{port} "
              f"(is orderecho_Main.py running?)", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
