"""FIX log viewer -- command line.

    orderecho_LogView.py view <files...> [filters] [--decode] [--follow]
    orderecho_LogView.py timeline <files...> (--clordid X | --order-id X)
    orderecho_LogView.py stats <files...> [filters]
    orderecho_LogView.py serve <files...> [--port 8091]

It reads any log with FIX in it, not just this emulator's, so it is useful on
a counterparty's file or on lines pasted out of a chat window.
"""

from __future__ import annotations

import argparse
import glob as globmodule
import os
import re
import sys
import time
from datetime import datetime, timezone

from orderecho_FixDict import default_dictionary
from orderecho_LogParse import (
    DIR_DISC,
    DIR_IN,
    DIR_OUT,
    FIX_LOG_NAME,
    LogParser,
    merge_chronologically,
    parse_timestamp,
)
from orderecho_Summary import (
    FILL_EXEC_TYPES,
    REJECT_TYPES,
    TAG_AVG_PX,
    TAG_BUSINESS_REJECT_REASON,
    TAG_CL_ORD_ID,
    TAG_CUM_QTY,
    TAG_CXL_REJ_REASON,
    TAG_CXL_REJ_RESPONSE_TO,
    TAG_EXEC_TYPE,
    TAG_LAST_PX,
    TAG_LAST_QTY,
    TAG_LEAVES_QTY,
    TAG_ORDER_ID,
    TAG_ORDER_QTY,
    TAG_ORD_REJ_REASON,
    TAG_ORD_STATUS,
    TAG_ORD_TYPE,
    TAG_ORIG_CL_ORD_ID,
    TAG_PRICE,
    TAG_REF_SEQ_NUM,
    TAG_REF_TAG_ID,
    TAG_SESSION_REJECT_REASON,
    TAG_SIDE,
    TAG_SYMBOL,
    TAG_TEXT,
    SummaryContext,
    flags_for,
    stamp_of,
    is_reject,
    msg_type_name,
    summarize,
)
from orderecho_Timeline import FAIL, PASS, WARN, build_chain

# ------------------------------------------------------------------- colour

RESET = "\033[0m"
COLORS = {
    "in": "\033[36m",        # cyan
    "out": "\033[37m",       # grey
    "disc": "\033[35m",      # magenta
    "reject": "\033[31m",    # red
    "fill": "\033[32m",      # green
    "flag": "\033[33m",      # yellow
    "dim": "\033[2m",
    "bold": "\033[1m",
    PASS: "\033[32m",
    WARN: "\033[33m",
    FAIL: "\033[31m",
}

ARROWS = {DIR_IN: "-->", DIR_OUT: "<--", DIR_DISC: " x ", "unknown": " ? "}

class Painter:
    """Colour when it helps and never when it would be noise."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, style: str | None) -> str:
        if not self.enabled or not style or style not in COLORS:
            return text
        return f"{COLORS[style]}{text}{RESET}"


def want_color(no_color: bool, stream=None) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


# ------------------------------------------------------------------ filters


class Filters:
    """Everything `view` and `stats` can narrow by."""

    def __init__(self, args) -> None:
        self.session = getattr(args, "session", None)
        self.msg_types = _split_list(getattr(args, "msg_type", None))
        self.direction = getattr(args, "dir", None)
        self.symbol = getattr(args, "symbol", None)
        self.injected = bool(getattr(args, "injected", False))
        self.rejects = bool(getattr(args, "rejects", False))
        self.grep = getattr(args, "grep", None)
        self.since = parse_timestamp(getattr(args, "since", None) or "")
        self.until = parse_timestamp(getattr(args, "until", None) or "")
        self.chain_ids: set | None = None      # filled in for --clordid

    def matches(self, message) -> bool:
        if self.chain_ids is not None:
            ids = {message.get(TAG_CL_ORD_ID), message.get(TAG_ORIG_CL_ORD_ID),
                   message.get(TAG_ORDER_ID)}
            if not (ids & self.chain_ids):
                return False
        if self.session and message.session != self.session:
            return False
        if self.msg_types and message.msg_type not in self.msg_types:
            return False
        if self.direction and message.direction != self.direction:
            return False
        if self.symbol and message.get(TAG_SYMBOL) != self.symbol:
            return False
        if self.injected and not message.injected:
            return False
        if self.rejects and not is_reject(message):
            return False
        if self.grep and self.grep.lower() not in message.raw.lower():
            return False
        if self.since and (message.ts is None or message.ts < self.since):
            return False
        if self.until and (message.ts is None or message.ts > self.until):
            return False
        return True


def _split_list(value):
    if not value:
        return None
    return {part.strip() for part in str(value).split(",") if part.strip()}


def format_line(message, dictionary, paint: Painter,
                context: SummaryContext | None = None) -> str:
    """One line, laid out for a terminal.  The words come from the summary."""
    stamp = stamp_of(message)
    arrow = ARROWS.get(message.direction, " ? ")
    session = message.session or "-"
    seq = message.seq if message.seq is not None else "-"
    name = msg_type_name(message, dictionary)
    body, style = summarize(message, dictionary, context)

    head = (f"{stamp} {paint(arrow, message.direction)} "
            f"{session:<14} {str(seq):>5} {name:<22}")
    text = f"{head} {paint(body, style)}"
    flags = flags_for(message)
    if flags:
        text += " " + paint("[" + " ".join(flags) + "]", "flag")
    return text


def decode_block(message, dictionary, paint: Painter) -> str:
    version = message.begin_string
    lines = []
    for tag, value in message.fields:
        name = dictionary.tag_name(tag, version)
        enum = dictionary.enum_name(tag, value, version)
        rendered = f"      {tag:>5}  {name:<24} = {value}"
        if enum:
            rendered += paint(f"  ({enum})", "dim")
        lines.append(rendered)
    return "\n".join(lines)


# ----------------------------------------------------------------- following


class Follower:
    """Tails files, coping with truncation and a new day's file."""

    def __init__(self, paths, parser: LogParser) -> None:
        self.parser = parser
        self.handles: dict = {}
        self.offsets: dict = {}
        self.line_no: dict = {}
        self.directories: dict = {}
        for path in paths:
            self._open(path, seek_end=False)
            match = FIX_LOG_NAME.match(os.path.basename(path))
            if match:
                # Remember the shape so a new day's file is picked up.
                self.directories[os.path.dirname(path) or "."] = \
                    match.group("session")

    def _open(self, path: str, seek_end: bool) -> None:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        if seek_end:
            handle.seek(0, os.SEEK_END)
        self.handles[path] = handle
        self.offsets[path] = handle.tell()
        self.line_no.setdefault(path, 0)

    def _discover(self) -> None:
        for directory, session in self.directories.items():
            pattern = os.path.join(directory, f"{session}_*.log")
            for path in sorted(globmodule.glob(pattern)):
                if path not in self.handles:
                    self._open(path, seek_end=False)

    def poll(self) -> list:
        """Whatever has been appended since last time."""
        self._discover()
        found = []
        for path, handle in list(self.handles.items()):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < self.offsets.get(path, 0):
                # Truncated underneath us: start again from the top.
                handle.close()
                self._open(path, seek_end=False)
                self.line_no[path] = 0
                handle = self.handles.get(path)
                if handle is None:
                    continue
            session = None
            match = FIX_LOG_NAME.match(os.path.basename(path))
            if match:
                session = match.group("session")
            while True:
                line = handle.readline()
                if not line:
                    break
                self.line_no[path] += 1
                found.extend(self.parser.parse_line(
                    line, self.line_no[path], os.path.basename(path), session))
            self.offsets[path] = handle.tell()
        return found

    def close(self) -> None:
        for handle in self.handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self.handles = {}


# ------------------------------------------------------------------ commands


def expand_paths(patterns) -> list:
    paths = []
    for pattern in patterns:
        matched = sorted(globmodule.glob(pattern))
        if matched:
            paths.extend(matched)
        elif os.path.exists(pattern):
            paths.append(pattern)
    seen = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


def _chain_ids(messages, cl_ord_id=None, order_id=None) -> set:
    chain = build_chain(messages, cl_ord_id=cl_ord_id, order_id=order_id)
    return set(chain.ids) | set(chain.order_ids)


def command_view(args, out=sys.stdout) -> int:
    paths = expand_paths(args.files)
    if not paths:
        print(f"error: no such file(s): {' '.join(args.files)}", file=sys.stderr)
        return 2
    dictionary = default_dictionary()
    paint = Painter(want_color(args.no_color, out))
    parser = LogParser(me=args.me)
    filters = Filters(args)

    messages = merge_chronologically(parser.parse_files(paths))
    if args.clordid or args.order_id:
        filters.chain_ids = _chain_ids(messages, args.clordid, args.order_id)

    # The context sees every message, not just the ones that pass the filter:
    # a replace can only say what changed if we watched the order change.
    context = SummaryContext()
    for message in messages:
        if filters.matches(message):
            print(format_line(message, dictionary, paint, context), file=out)
            if args.decode:
                print(decode_block(message, dictionary, paint), file=out)
        context.note(message)

    if not args.follow:
        if parser.stats.skipped_lines:
            print(paint(f"  ({parser.stats.skipped_lines} unparseable line(s) "
                        f"skipped)", "dim"), file=out)
        return 0

    follower = Follower(paths, parser)
    follower.poll()                     # we already printed the backlog
    try:
        while True:
            for message in follower.poll():
                if filters.matches(message):
                    print(format_line(message, dictionary, paint, context),
                          file=out, flush=True)
                    if args.decode:
                        print(decode_block(message, dictionary, paint),
                              file=out, flush=True)
                context.note(message)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        follower.close()


def command_timeline(args, out=sys.stdout) -> int:
    paths = expand_paths(args.files)
    if not paths:
        print(f"error: no such file(s): {' '.join(args.files)}", file=sys.stderr)
        return 2
    if not (args.clordid or args.order_id):
        print("error: timeline needs --clordid or --order-id", file=sys.stderr)
        return 2

    dictionary = default_dictionary()
    paint = Painter(want_color(args.no_color, out))
    parser = LogParser(me=args.me)
    messages = merge_chronologically(parser.parse_files(paths))
    chain = build_chain(messages, cl_ord_id=args.clordid,
                        order_id=args.order_id)

    if not chain.steps:
        print(f"no messages found for {args.clordid or args.order_id}",
              file=out)
        return 2

    print(paint(f"Order chain for {chain.seed}", "bold"), file=out)
    print(f"  ClOrdIDs: {', '.join(sorted(chain.ids))}", file=out)
    if chain.order_ids:
        print(f"  OrderID : {', '.join(sorted(chain.order_ids))}", file=out)
    print("", file=out)

    header = (f"  {'time':<21} {'dir':<4} {'type':<20} {'exec/status':<28} "
              f"{'qty':>7} {'last':>14} {'cum':>7} {'leaves':>7} {'avg':>10}")
    print(paint(header, "bold"), file=out)
    for step in chain.steps:
        row = step.as_dict(dictionary)
        stamp = (row["ts"] or "-")[:23]
        exec_status = f"{row['exec_type'] or '-'} / {row['ord_status'] or '-'}"
        line = (f"  {stamp:<21} {ARROWS.get(row['direction'], '?'):<4} "
                f"{(row['msg_type_name'] or row['msg_type'] or '?'):<20} "
                f"{exec_status:<28} {str(row['order_qty'] or '-'):>7} "
                f"{str(row['last'] or '-'):>14} {str(row['cum_qty'] or '-'):>7} "
                f"{str(row['leaves_qty'] or '-'):>7} "
                f"{str(row['avg_px'] or '-'):>10}")
        if row["replay"]:
            line += paint("  [replay]", "flag")
        if row["flags"]:
            line += " " + paint("[" + " ".join(row["flags"]) + "]", "flag")
        if row["text"]:
            line += f'  "{row["text"]}"'
        print(line, file=out)

    print("", file=out)
    print(paint("Checks", "bold"), file=out)
    for check in chain.checks:
        badge = paint(f"[{check.status}]", check.status)
        print(f"  {badge} {check.name}: {check.explanation}", file=out)

    print("", file=out)
    print(f"  {paint('verdict: ' + chain.verdict, chain.verdict)}", file=out)
    return chain.exit_code


def command_stats(args, out=sys.stdout) -> int:
    paths = expand_paths(args.files)
    if not paths:
        print(f"error: no such file(s): {' '.join(args.files)}", file=sys.stderr)
        return 2
    dictionary = default_dictionary()
    paint = Painter(want_color(args.no_color, out))
    parser = LogParser(me=args.me)
    filters = Filters(args)

    by_session: dict = {}
    by_type: dict = {}
    by_direction: dict = {}
    rejects: dict = {}
    injected = 0
    resends = gap_fills = 0
    first = last = None
    total = 0

    for message in parser.parse_files(paths):
        if not filters.matches(message):
            continue
        total += 1
        by_session[message.session or "-"] = \
            by_session.get(message.session or "-", 0) + 1
        msg_type = message.msg_type or "?"
        by_type[msg_type] = by_type.get(msg_type, 0) + 1
        by_direction[message.direction] = \
            by_direction.get(message.direction, 0) + 1
        if message.injected:
            injected += 1
        if msg_type == "2":
            resends += 1
        if msg_type == "4" and message.get(123) == "Y":
            gap_fills += 1
        if is_reject(message):
            reason = _reject_reason(message, dictionary)
            rejects[reason] = rejects.get(reason, 0) + 1
        if message.ts:
            first = message.ts if first is None else min(first, message.ts)
            last = message.ts if last is None else max(last, message.ts)

    def section(title, mapping):
        print(paint(title, "bold"), file=out)
        if not mapping:
            print("  (none)", file=out)
            return
        width = max(len(str(key)) for key in mapping)
        for key, count in sorted(mapping.items(),
                                 key=lambda item: (-item[1], str(item[0]))):
            print(f"  {str(key):<{width}}  {count}", file=out)

    print(paint(f"{total} message(s) from {len(parser.stats.files)} file(s)",
                "bold"), file=out)
    print(f"  first: {first.isoformat() if first else '-'}", file=out)
    print(f"  last : {last.isoformat() if last else '-'}", file=out)
    print("", file=out)
    section("By session", by_session)
    print("", file=out)
    section("By message type", {
        f"{key} {dictionary.enum_name(35, key) or ''}".strip(): value
        for key, value in by_type.items()})
    print("", file=out)
    section("By direction", by_direction)
    print("", file=out)
    section("Rejects by reason", rejects)
    print("", file=out)
    print(paint("Other", "bold"), file=out)
    print(f"  resend requests   {resends}", file=out)
    print(f"  gap fills         {gap_fills}", file=out)
    print(f"  injected          {injected}", file=out)
    print(f"  bad checksum      {parser.stats.bad_checksum}", file=out)
    print(f"  bad body length   {parser.stats.bad_length}", file=out)
    print(f"  unparseable lines {parser.stats.skipped_lines}", file=out)
    return 0


def _reject_reason(message, dictionary) -> str:
    version = message.begin_string
    for tag in (TAG_ORD_REJ_REASON, TAG_CXL_REJ_REASON,
                TAG_SESSION_REJECT_REASON, TAG_BUSINESS_REJECT_REASON):
        value = message.get(tag)
        if value is not None:
            name = dictionary.enum_name(tag, value, version)
            return f"{message.msg_type}: {tag}={value}" + (
                f" ({name})" if name else "")
    return f"{message.msg_type}: unspecified"


def command_serve(args, out=sys.stdout) -> int:
    from orderecho_LogViewer import serve_files

    paths = expand_paths(args.files)
    if not paths:
        print(f"error: no such file(s): {' '.join(args.files)}",
              file=sys.stderr)
        return 2
    return serve_files(paths, host=args.host, port=args.port, me=args.me,
                       out=out)


# --------------------------------------------------------------------- CLI


def _add_filters(parser) -> None:
    parser.add_argument("--session")
    parser.add_argument("--msg-type", dest="msg_type",
                        help="comma-separated, e.g. D,8")
    parser.add_argument("--dir", choices=["in", "out", "disc", "unknown"])
    parser.add_argument("--clordid")
    parser.add_argument("--order-id", dest="order_id")
    parser.add_argument("--symbol")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--injected", action="store_true")
    parser.add_argument("--rejects", action="store_true")
    parser.add_argument("--grep")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orderecho_LogView.py",
        description="Read, filter, follow and explain FIX logs",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    view = sub.add_parser("view", help="one line per message")
    view.add_argument("files", nargs="+")
    _add_filters(view)
    view.add_argument("--decode", action="store_true",
                      help="show every field, named and decoded")
    view.add_argument("--follow", action="store_true",
                      help="keep printing new messages as they arrive")
    view.add_argument("--interval", type=float, default=1.0)
    view.add_argument("--no-color", action="store_true")
    view.add_argument("--me", help="our CompID, to infer direction")

    timeline = sub.add_parser("timeline", help="one order's life, with checks")
    timeline.add_argument("files", nargs="+")
    timeline.add_argument("--clordid")
    timeline.add_argument("--order-id", dest="order_id")
    timeline.add_argument("--no-color", action="store_true")
    timeline.add_argument("--me")

    stats = sub.add_parser("stats", help="counts and totals")
    stats.add_argument("files", nargs="+")
    _add_filters(stats)
    stats.add_argument("--no-color", action="store_true")
    stats.add_argument("--me")

    serve = sub.add_parser("serve", help="the web viewer over these files")
    serve.add_argument("files", nargs="+")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    serve.add_argument("--me")
    serve.add_argument("--no-color", action="store_true")
    return parser


COMMANDS = {
    "view": command_view,
    "timeline": command_timeline,
    "stats": command_stats,
    "serve": command_serve,
}


def main(argv=None, out=sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args, out)


if __name__ == "__main__":
    sys.exit(main())
