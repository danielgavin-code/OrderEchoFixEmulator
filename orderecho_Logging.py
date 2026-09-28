"""Human-readable logs: the FIX message log and the engine log.

Both are append-only, flushed after every line, roll over when the UTC date
changes, and never raise out into the session -- a logging failure is written
to stderr and the engine keeps running.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime, timezone

from orderecho_Codec import to_delimiter

DIR_IN = "IN"
DIR_OUT = "OUT"
DIR_DISC = "DISC"

_LINE_TS_FORMAT = "%Y%m%d-%H:%M:%S"


def _stamp(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return f"{when.strftime(_LINE_TS_FORMAT)}.{when.microsecond // 1000:03d}"


def _date_part(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%d")


class _RollingFile:
    """A file whose name carries the UTC date, reopened when the date turns."""

    def __init__(self, directory: str, name_template: str, clock, label: str) -> None:
        self.directory = directory
        self.name_template = name_template
        self.clock = clock
        self.label = label
        self._handle = None
        self._date = None
        self.path = None
        self._warned = False

    def _ensure(self) -> None:
        date = _date_part(self.clock.now())
        if self._handle is not None and date == self._date:
            return
        self.close()
        self._date = date
        self.path = os.path.join(self.directory, self.name_template.format(date=date))
        try:
            os.makedirs(self.directory, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")
            self._warned = False
        except OSError as exc:
            self._handle = None
            self._warn(f"{self.label}: cannot open {self.path}: {exc}")

    def _warn(self, message: str) -> None:
        if not self._warned:
            print(message, file=sys.stderr)
            self._warned = True

    def write_line(self, line: str) -> None:
        try:
            self._ensure()
            if self._handle is None:
                return
            self._handle.write(line + "\n")
            self._handle.flush()
        except OSError as exc:
            self._warn(f"{self.label}: write failed: {exc}")
        except Exception as exc:  # logging must never take the session down
            self._warn(f"{self.label}: unexpected failure: {exc}")

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


class FixMessageLog:
    """Every FIX message in and out, including discarded frames. Nothing else.

    Format (columns padded, raw message always last):
        20260926-14:03:22.114 IN   seq=5    35=1  8=FIX.4.2|9=62|...|10=201|
    """

    def __init__(self, log_dir: str, session_name: str, clock,
                 delimiter: str = "|", console: bool = False) -> None:
        self.directory = os.path.join(log_dir, "fix")
        self.session_name = session_name
        self.clock = clock
        self.delimiter = delimiter
        self.console = console
        self._file = _RollingFile(
            self.directory, f"{session_name}_{{date}}.log", clock, "fix log"
        )

    @property
    def path(self):
        return self._file.path

    def format_line(self, direction: str, seq, msg_type, raw,
                    reason: str | None = None) -> str:
        seq_col = f"seq={seq}" if seq is not None else "-"
        type_col = f"35={msg_type}" if msg_type else "-"
        body = to_delimiter(raw, self.delimiter)
        line = (
            f"{_stamp(self.clock.now())} "
            f"{direction:<4} "
            f"{seq_col:<8} "
            f"{type_col:<5} "
            f"{body}"
        )
        if reason:
            line += f"  # {reason}"
        return line

    def log(self, direction: str, seq, msg_type, raw,
            reason: str | None = None) -> str:
        line = self.format_line(direction, seq, msg_type, raw, reason)
        self._file.write_line(line)
        if self.console:
            print(line, flush=True)
        return line

    def inbound(self, msg, seq=None) -> str:
        if seq is None:
            seq = getattr(msg, "seq_num", None)
        return self.log(DIR_IN, seq, msg.msg_type, msg.raw)

    def outbound(self, msg, seq=None) -> str:
        if seq is None:
            seq = getattr(msg, "seq_num", None)
        return self.log(DIR_OUT, seq, msg.msg_type, msg.raw)

    def discarded(self, frame) -> str:
        return self.log(DIR_DISC, None, None, frame.raw,
                        getattr(frame, "reason", "discarded"))

    def close(self) -> None:
        self._file.close()


class _EngineHandler(logging.Handler):
    """Writes engine lines through a date-rolling file, using our clock."""

    def __init__(self, rolling: _RollingFile, session_name: str,
                 console: bool = False) -> None:
        super().__init__()
        self.rolling = rolling
        self.session_name = session_name
        self.console = console

    def format_line(self, record: logging.LogRecord) -> str:
        when = getattr(record, "when", None) or datetime.now(timezone.utc)
        session = getattr(record, "session", self.session_name)
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return (
            f"{_stamp(when)} "
            f"{record.levelname:<7} "
            f"{record.name:<8} "
            f"{session}  "
            f"{message}"
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format_line(record)
        except Exception:  # pragma: no cover - formatting must not kill us
            print("engine log: could not format record", file=sys.stderr)
            return
        self.rolling.write_line(line)
        if self.console and record.levelno >= logging.INFO:
            print(line, flush=True)


class EngineLog:
    """Engine events, state changes and errors."""

    _counter = 0

    def __init__(self, log_dir: str, session_name: str, clock,
                 level: str = "INFO", console: bool = False,
                 logger_name: str = "session") -> None:
        self.directory = os.path.join(log_dir, "engine")
        self.session_name = session_name
        self.clock = clock
        self._file = _RollingFile(
            self.directory, "orderecho_{date}.log", clock, "engine log"
        )
        self._handler = _EngineHandler(self._file, session_name, console)
        EngineLog._counter += 1
        self.logger = logging.getLogger(
            f"{logger_name}.{EngineLog._counter}"
        )
        # The visible name is the short one; the suffix only keeps instances
        # from sharing handlers across tests.
        self.logger.name = logger_name
        self.logger.handlers = [self._handler]
        self.logger.propagate = False
        self.logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    @property
    def path(self):
        return self._file.path

    def _log(self, level: int, message: str, exc_info=None,
             session: str | None = None) -> None:
        try:
            self.logger.log(
                level,
                message,
                exc_info=exc_info,
                extra={"when": self.clock.now(),
                       # One engine log for the whole engine, so the session
                       # column says which session a line came from.
                       "session": session or self.session_name},
            )
        except Exception as exc:  # never raise out of the logger
            print(f"engine log: {exc}", file=sys.stderr)

    def debug(self, message: str, session: str | None = None) -> None:
        self._log(logging.DEBUG, message, session=session)

    def info(self, message: str, session: str | None = None) -> None:
        self._log(logging.INFO, message, session=session)

    def warning(self, message: str, session: str | None = None) -> None:
        self._log(logging.WARNING, message, session=session)

    def error(self, message: str, exc_info=None,
              session: str | None = None) -> None:
        self._log(logging.ERROR, message, exc_info=exc_info, session=session)

    def exception(self, message: str, session: str | None = None) -> None:
        self._log(logging.ERROR, message, exc_info=sys.exc_info(),
                  session=session)

    def close(self) -> None:
        self.logger.handlers = []
        self._file.close()


def build_logs(config, session_name: str, clock):
    """Create both log streams from a Config."""
    fix_log = FixMessageLog(
        config.logging.log_dir,
        session_name,
        clock,
        delimiter=config.logging.fix_delimiter,
        console=config.logging.console,
    )
    engine_log = EngineLog(
        config.logging.log_dir,
        session_name,
        clock,
        level=config.logging.engine_level,
        console=config.logging.console,
    )
    return fix_log, engine_log
