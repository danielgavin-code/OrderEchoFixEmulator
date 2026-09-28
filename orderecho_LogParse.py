"""FIX log parsing (PURE).

Reads any log that has FIX in it and hands back structured messages.  It is
deliberately not tied to this emulator: the same parser has to cope with a
QuickFIX `messages.log`, another engine's output, or a handful of lines pasted
into a file, because the viewer is meant to be useful on somebody else's logs
too.

Two rules shape everything here:

* It never raises.  A line it cannot make sense of is counted and skipped; a
  message whose BodyLength or CheckSum is wrong is *kept and flagged*, because
  a broken message is exactly what someone reading a log is hunting for.
* It streams.  Files are read a line at a time and messages are yielded as
  they are found, so memory follows what you keep, not how big the file is.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

SOH = "\x01"
PIPE = "|"
CARET_A = "^A"

DIR_IN = "in"
DIR_OUT = "out"
DIR_DISC = "disc"
DIR_UNKNOWN = "unknown"

#: Our own FIX log line (Cook 1 §10A):
#: 20260927-08:54:19.100 IN   seq=1    35=A  8=FIX.4.2|...|10=070|  # comment
ORDERECHO_LINE = re.compile(
    r"^(?P<ts>\d{8}-\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<dir>IN|OUT|DISC)\s+"
    r"(?P<seq>seq=\S+|-)\s+"
    r"(?P<mtype>35=\S+|-)\s+"
    r"(?P<rest>.*)$"
)

#: QuickFIX prefixes its lines with a timestamp and " : ".
QUICKFIX_PREFIX = re.compile(
    r"^(?P<ts>\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*:\s*(?P<rest>.*)$"
)

#: A leading ISO 8601 timestamp, with or without a T and a zone.
ISO_PREFIX = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z?)"
    r"[\s,:|-]*(?P<rest>.*)$"
)

#: One whole FIX message, whatever the field delimiter is.
MESSAGE_PATTERN = re.compile(
    r"8=FIX(?:T)?\.[\d.]+(?P<delim>\x01|\|(?!\|)|\^A)"   # BeginString + delim
    r".*?"
    r"10=\d{1,3}(?P=delim)",
    re.DOTALL,
)

#: Our FIX log names its file <session>_<YYYYMMDD>.log.
FIX_LOG_NAME = re.compile(r"^(?P<session>.+)_(?P<date>\d{8})\.log$")

TAG_BEGIN_STRING = 8
TAG_BODY_LENGTH = 9
TAG_MSG_TYPE = 35
TAG_MSG_SEQ_NUM = 34
TAG_SENDER_COMP_ID = 49
TAG_TARGET_COMP_ID = 56
TAG_POSS_DUP_FLAG = 43
TAG_CHECKSUM = 10


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_timestamp(text: str):
    """Best-effort timestamp from the handful of shapes logs use."""
    if not text:
        return None
    text = text.strip().rstrip(":").strip()
    formats = (
        "%Y%m%d-%H:%M:%S.%f",
        "%Y%m%d-%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    cleaned = text[:-1] if text.endswith("Z") else text
    for fmt in formats:
        try:
            return _utc(datetime.strptime(cleaned, fmt))
        except ValueError:
            continue
    return None


def checksum_of(body: bytes) -> int:
    return sum(body) % 256


@dataclass
class ParsedMessage:
    """One FIX message, however it was written down."""

    raw: str                                  # normalized to SOH
    fields: list = field(default_factory=list)   # ordered (tag, value) pairs
    ts: datetime | None = None
    direction: str = DIR_UNKNOWN
    session: str | None = None
    injected: bool = False
    source: str | None = None
    source_line_no: int = 0
    comment: str | None = None
    bad_checksum: bool = False
    bad_length: bool = False
    index: int = 0                            # position in the stream

    # ------------------------------------------------------------ lookups

    def get(self, tag: int, nth: int = 1):
        tag = int(tag)
        for pair_tag, value in self.fields:
            if pair_tag == tag:
                nth -= 1
                if nth == 0:
                    return value
        return None

    def get_all(self, tag: int) -> list:
        tag = int(tag)
        return [value for pair_tag, value in self.fields if pair_tag == tag]

    @property
    def msg_type(self):
        return self.get(TAG_MSG_TYPE)

    @property
    def begin_string(self):
        return self.get(TAG_BEGIN_STRING)

    @property
    def seq(self):
        raw = self.get(TAG_MSG_SEQ_NUM)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def poss_dup(self) -> bool:
        return self.get(TAG_POSS_DUP_FLAG) == "Y"

    @property
    def suspect(self) -> bool:
        return self.bad_checksum or self.bad_length

    def pipe(self) -> str:
        return self.raw.replace(SOH, PIPE)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "ts": self.ts.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z") if self.ts else None,
            "direction": self.direction,
            "session": self.session,
            "msg_type": self.msg_type,
            "begin_string": self.begin_string,
            "seq": self.seq,
            "injected": self.injected,
            "poss_dup": self.poss_dup,
            "bad_checksum": self.bad_checksum,
            "bad_length": self.bad_length,
            "comment": self.comment,
            "source": self.source,
            "source_line_no": self.source_line_no,
            "raw": self.pipe(),
            "fields": [[str(tag), value] for tag, value in self.fields],
        }


@dataclass
class ParseStats:
    """What the parser saw, including what it could not use."""

    messages: int = 0
    skipped_lines: int = 0
    bad_checksum: int = 0
    bad_length: int = 0
    events: int = 0
    files: list = field(default_factory=list)

    def merge(self, other: "ParseStats") -> None:
        self.messages += other.messages
        self.skipped_lines += other.skipped_lines
        self.bad_checksum += other.bad_checksum
        self.bad_length += other.bad_length
        self.events += other.events
        for name in other.files:
            if name not in self.files:
                self.files.append(name)


# --------------------------------------------------------------- field split


def split_fields(raw: str) -> list:
    """Ordered (tag, value) pairs; repeats preserved, junk dropped."""
    pairs = []
    for chunk in raw.split(SOH):
        if not chunk:
            continue
        tag, sep, value = chunk.partition("=")
        if not sep:
            continue
        try:
            pairs.append((int(tag), value))
        except ValueError:
            continue
    return pairs


def verify_framing(raw: str):
    """(bad_length, bad_checksum) for a whole message.

    A wrong BodyLength or CheckSum is information, not a reason to drop the
    message, so this only reports.
    """
    data = raw.encode("latin-1", "replace")
    bad_length = bad_checksum = False
    try:
        after_begin = data.index(b"\x01") + 1
        if not data.startswith(b"9=", after_begin):
            return True, True
        length_end = data.index(b"\x01", after_begin)
        declared = int(data[after_begin + 2:length_end])
        body_start = length_end + 1
        trailer = data.rfind(b"\x0110=")
        if trailer == -1:
            return True, True
        actual = trailer + 1 - body_start
        bad_length = actual != declared
        computed = checksum_of(data[:trailer + 1])
        stated = int(data[trailer + 4:trailer + 7])
        bad_checksum = computed != stated
    except (ValueError, IndexError):
        return True, True
    return bad_length, bad_checksum


def normalize(raw: str, delimiter: str) -> str:
    if delimiter == SOH:
        return raw
    return raw.replace(delimiter, SOH)


def find_messages(text: str) -> list:
    """Every FIX message embedded in a line, with its delimiter."""
    found = []
    for match in MESSAGE_PATTERN.finditer(text):
        found.append((match.group(0), match.group("delim")))
    return found


# --------------------------------------------------------------- the parser


class LogParser:
    """Turns log lines into ParsedMessages.  Stateless between files."""

    def __init__(self, me: str | None = None) -> None:
        #: With --me we can tell which way a generic log's messages were going.
        self.me = me
        self.stats = ParseStats()
        self._index = 0

    # ------------------------------------------------------------ helpers

    def _direction_from_comp_ids(self, fields: list) -> str:
        if not self.me:
            return DIR_UNKNOWN
        sender = target = None
        for tag, value in fields:
            if tag == TAG_SENDER_COMP_ID and sender is None:
                sender = value
            elif tag == TAG_TARGET_COMP_ID and target is None:
                target = value
        if sender == self.me:
            return DIR_OUT
        if target == self.me:
            return DIR_IN
        return DIR_UNKNOWN

    def _build(self, raw: str, delimiter: str, **kwargs) -> ParsedMessage:
        normalized = normalize(raw, delimiter)
        fields = split_fields(normalized)
        bad_length, bad_checksum = verify_framing(normalized)
        direction = kwargs.pop("direction", None)
        if direction is None:
            direction = self._direction_from_comp_ids(fields)
        self._index += 1
        message = ParsedMessage(
            raw=normalized, fields=fields, direction=direction,
            bad_length=bad_length, bad_checksum=bad_checksum,
            index=self._index, **kwargs,
        )
        self.stats.messages += 1
        if bad_checksum:
            self.stats.bad_checksum += 1
        if bad_length:
            self.stats.bad_length += 1
        return message

    # --------------------------------------------------------- one line

    def parse_line(self, line: str, line_no: int = 0, source: str | None = None,
                   session: str | None = None) -> list:
        """Every message on one line.  Never raises."""
        try:
            return self._parse_line(line, line_no, source, session)
        except Exception:
            self.stats.skipped_lines += 1
            return []

    def _parse_line(self, line: str, line_no: int, source, session) -> list:
        text = line.rstrip("\r\n")
        if not text.strip():
            return []

        # 1. Our own evidence JSONL.
        stripped = text.lstrip()
        if stripped.startswith("{"):
            parsed = self._parse_evidence(stripped, line_no, source)
            if parsed is not None:
                return parsed

        # 2. Our own FIX log line.
        match = ORDERECHO_LINE.match(text)
        if match:
            parsed = self._parse_orderecho(match, line_no, source, session)
            if parsed is not None:
                return parsed

        # 3. Anything else with FIX in it.
        return self._parse_generic(text, line_no, source, session)

    def _parse_evidence(self, text: str, line_no: int, source):
        try:
            record = json.loads(text)
        except ValueError:
            return None
        if not isinstance(record, dict) or "kind" not in record:
            return None
        kind = record.get("kind")
        if kind == "event":
            self.stats.events += 1
            return []
        raw = record.get("raw")
        if not raw:
            self.stats.events += 1
            return []
        direction = {"in": DIR_IN, "out": DIR_OUT,
                     "discarded": DIR_DISC}.get(kind, DIR_UNKNOWN)
        delimiter = SOH if SOH in raw else PIPE
        return [self._build(
            raw, delimiter, ts=parse_timestamp(record.get("ts") or ""),
            direction=direction, session=record.get("session"),
            injected=bool(record.get("injected")),
            comment=record.get("detail"), source=source,
            source_line_no=line_no,
        )]

    def _parse_orderecho(self, match, line_no: int, source, session):
        rest = match.group("rest")
        comment = None
        if "  # " in rest:
            rest, _, comment = rest.partition("  # ")
            comment = comment.strip()
        found = find_messages(rest)
        if not found:
            return None
        direction = {"IN": DIR_IN, "OUT": DIR_OUT,
                     "DISC": DIR_DISC}[match.group("dir").strip()]
        injected = bool(comment and comment.startswith("injected:"))
        ts = parse_timestamp(match.group("ts"))
        return [
            self._build(raw, delimiter, ts=ts, direction=direction,
                        session=session, injected=injected, comment=comment,
                        source=source, source_line_no=line_no)
            for raw, delimiter in found
        ]

    def _parse_generic(self, text: str, line_no: int, source, session):
        found = find_messages(text)
        if not found:
            self.stats.skipped_lines += 1
            return []
        prefix = text[:text.index(found[0][0])] if found else ""
        ts = None
        for pattern in (QUICKFIX_PREFIX, ISO_PREFIX):
            match = pattern.match(prefix if prefix.strip() else text)
            if match:
                ts = parse_timestamp(match.group("ts"))
                if ts:
                    break
        return [
            self._build(raw, delimiter, ts=ts, session=session,
                        source=source, source_line_no=line_no)
            for raw, delimiter in found
        ]

    # ------------------------------------------------------------ streams

    def parse_lines(self, lines, source: str | None = None,
                    session: str | None = None):
        for line_no, line in enumerate(lines, start=1):
            for message in self.parse_line(line, line_no, source, session):
                yield message

    def parse_file(self, path: str):
        """Stream one file.  A session name is taken from our log file name."""
        session = session_from_filename(path)
        name = os.path.basename(path)
        if name not in self.stats.files:
            self.stats.files.append(name)
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for message in self.parse_lines(handle, source=name,
                                            session=session):
                yield message

    def parse_files(self, paths):
        for path in paths:
            for message in self.parse_file(path):
                yield message


def session_from_filename(path: str):
    """`logs/fix/agent44_20260927.log` -> `agent44`."""
    match = FIX_LOG_NAME.match(os.path.basename(path))
    return match.group("session") if match else None


#: Earlier than any real log line, for messages with nothing to place them by.
_BEFORE_EVERYTHING = datetime.min.replace(tzinfo=timezone.utc)


def merge_chronologically(messages) -> list:
    """Order messages from several files by time without shuffling a file.

    Logs from different sessions are different files, so reading them one
    after another interleaves nothing: a conversation that happened at 09:31
    can print after one that happened at 09:38.  This puts them back in the
    order they happened.

    A line with no timestamp of its own keeps the timestamp of the last
    stamped line before it *in its own file*, so a continuation line stays
    where it was written.  An unstamped run at the head of a file inherits
    that file's first timestamp, which leaves it just before the line it
    belongs to.  A file with no timestamps anywhere cannot be placed at all,
    so it goes last rather than pretending to be the oldest.

    The sort is stable, so messages sharing a timestamp stay in the order the
    files were read in.
    """
    messages = list(messages)

    first_seen: dict = {}
    for message in messages:
        if message.ts is not None and message.source not in first_seen:
            first_seen[message.source] = message.ts

    keys: list = []
    carried: dict = {}
    for message in messages:
        if message.ts is not None:
            carried[message.source] = message.ts
        keys.append(carried.get(message.source,
                                first_seen.get(message.source)))

    order = sorted(
        range(len(messages)),
        key=lambda i: (keys[i] is None, keys[i] or _BEFORE_EVERYTHING),
    )
    return [messages[i] for i in order]


def parse_files(paths, me: str | None = None):
    """Convenience: messages and the stats that go with them."""
    parser = LogParser(me=me)
    return list(parser.parse_files(paths)), parser.stats
