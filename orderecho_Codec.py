"""FIX 4.2 codec on top of simplefix.

Framing is done here rather than in simplefix so that BodyLength (9) and
CheckSum (10) are verified by us, and so a frame that fails either check can
be handed back intact as a DiscardedFrame for the evidence log.  Per the FIX
spec such a message is *discarded*, never rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import simplefix

SOH = b"\x01"
SOH_CHAR = "\x01"
PIPE = "|"

#: A frame larger than this is assumed to be garbage rather than a message.
MAX_FRAME_BYTES = 1 << 20

#: Frames are located by any FIX BeginString, not just our own version: a
#: counterparty speaking the wrong version must still be decoded so the session
#: can answer it properly (spec 5.5) rather than silently discarding bytes.
FRAME_MARKER = b"8=FIX"

TIME_FORMAT = "%Y%m%d-%H:%M:%S"


def format_time(when: datetime) -> str:
    """SendingTime / OrigSendingTime format: YYYYMMDD-HH:MM:SS.sss in UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return f"{when.strftime(TIME_FORMAT)}.{when.microsecond // 1000:03d}"


def to_pipe(raw: bytes | str) -> str:
    """Display helper: SOH -> '|' so a line can be pasted into a decoder."""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    return raw.replace(SOH_CHAR, PIPE)


def to_delimiter(raw: bytes | str, delimiter: str) -> str:
    """Render *raw* with the configured delimiter ('|' or real SOH)."""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    if delimiter == "SOH":
        return raw
    return raw.replace(SOH_CHAR, delimiter)


@dataclass
class FixMsg:
    """A decoded, framing-valid FIX message."""

    pairs: list[tuple[int, str]] = field(default_factory=list)
    raw: bytes = b""

    @property
    def msg_type(self) -> str | None:
        return self.get(35)

    @property
    def seq_num(self) -> int | None:
        """MsgSeqNum as an int, or None if absent/not a number."""
        raw_seq = self.get(34)
        if raw_seq is None:
            return None
        try:
            return int(raw_seq)
        except ValueError:
            return None

    def get(self, tag: int, nth: int = 1) -> str | None:
        tag = int(tag)
        for pair_tag, value in self.pairs:
            if pair_tag == tag:
                nth -= 1
                if nth == 0:
                    return value
        return None

    def has(self, tag: int) -> bool:
        return self.get(tag) is not None

    def fields(self) -> list[list[str]]:
        """Ordered [tag, value] pairs, as the evidence log records them.

        A list rather than a mapping so field order survives and a repeated
        tag is not silently collapsed.
        """
        return [[str(tag), value] for tag, value in self.pairs]

    def to_pipe(self) -> str:
        return to_pipe(self.raw)


@dataclass
class DiscardedFrame:
    """Bytes that framed as a message but failed 9 or 10 validation."""

    raw: bytes
    reason: str

    def to_pipe(self) -> str:
        return to_pipe(self.raw)


def _checksum(data: bytes) -> int:
    return sum(data) % 256


class Codec:
    """Incremental decoder plus encoder for one connection."""

    def __init__(self, fix_version: str = "FIX.4.2") -> None:
        self.fix_version = fix_version
        self._begin = FRAME_MARKER
        self._buffer = b""

    # ------------------------------------------------------------------ encode

    def encode(
        self,
        msg_type: str,
        body_fields,
        *,
        sender_comp_id: str,
        target_comp_id: str,
        seq_num: int,
        sending_time,
        poss_dup: bool = False,
        orig_sending_time=None,
    ) -> bytes:
        """Build a wire message.

        Field order is 8, 9, 35, 49, 56, 34, 52, [43, 122], body..., 10.
        """
        if isinstance(sending_time, datetime):
            sending_time = format_time(sending_time)
        if isinstance(orig_sending_time, datetime):
            orig_sending_time = format_time(orig_sending_time)

        msg = simplefix.FixMessage()
        msg.append_pair(8, self.fix_version)
        msg.append_pair(35, str(msg_type))
        msg.append_pair(49, sender_comp_id)
        msg.append_pair(56, target_comp_id)
        msg.append_pair(34, str(int(seq_num)))
        msg.append_pair(52, sending_time)
        if poss_dup:
            msg.append_pair(43, "Y")
        if orig_sending_time is not None:
            msg.append_pair(122, orig_sending_time)
        for tag, value in body_fields or ():
            msg.append_pair(int(tag), str(value))
        # simplefix places 8/9/35 first and computes 9 and 10 itself; the
        # remaining fields keep insertion order.
        return msg.encode()

    def rebuild(self, pairs, corrupt_checksum: bool = False) -> bytes:
        """Re-encode a message from an ordered list of (tag, value) pairs.

        Framing fields (8, 9, 10) in *pairs* are dropped and recomputed, so a
        caller can mutate a decoded message freely and get a well-formed frame
        back.  With ``corrupt_checksum`` the CheckSum is deliberately wrong --
        the one way to hand the counterparty a frame it must discard.
        """
        body = b""
        for tag, value in pairs:
            if int(tag) in (8, 9, 10):
                continue
            body += f"{int(tag)}=".encode("ascii") + str(value).encode("latin-1") + SOH

        head = (b"8=" + self.fix_version.encode("ascii") + SOH
                + b"9=" + str(len(body)).encode("ascii") + SOH)
        frame = head + body
        checksum = _checksum(frame)
        if corrupt_checksum:
            checksum = (checksum + 1) % 256
        return frame + b"10=" + f"{checksum:03d}".encode("ascii") + SOH

    # ------------------------------------------------------------------ decode

    def decode(self, data: bytes) -> list:
        """Feed raw bytes; return zero or more FixMsg / DiscardedFrame."""
        if data:
            self._buffer += data
        out: list = []
        while True:
            item, consumed = self._next_frame()
            if consumed == 0:
                break
            self._buffer = self._buffer[consumed:]
            if item is not None:
                out.append(item)
        if len(self._buffer) > MAX_FRAME_BYTES:
            junk, self._buffer = self._buffer, b""
            out.append(DiscardedFrame(junk, "Buffer overflow without a complete frame"))
        return out

    # Alias: reads better at the call site in the transport.
    feed = decode

    def reset(self) -> None:
        self._buffer = b""

    @property
    def buffer(self) -> bytes:
        return self._buffer

    def _next_frame(self):
        """Return (item, bytes_consumed).  consumed == 0 means 'need more'."""
        buf = self._buffer
        if not buf:
            return None, 0

        # 1. Line up on a BeginString.
        start = buf.find(self._begin)
        if start == -1:
            # Keep whatever might still be the head of a BeginString.
            if len(buf) > len(self._begin):
                keep = len(self._begin) - 1
                junk = buf[:-keep]
                if junk:
                    return (
                        DiscardedFrame(junk, "Leading bytes before BeginString (8)"),
                        len(junk),
                    )
            return None, 0
        if start > 0:
            return (
                DiscardedFrame(buf[:start], "Leading bytes before BeginString (8)"),
                start,
            )

        # 2. BodyLength must be the second field, whatever BeginString says.
        begin_end = buf.find(SOH)
        if begin_end == -1:
            return None, 0
        len_start = begin_end + 1
        if not buf.startswith(b"9=", len_start):
            return self._discard_to_next_frame("BodyLength (9) is not the second field")
        len_end = buf.find(SOH, len_start)
        if len_end == -1:
            return None, 0
        try:
            body_length = int(buf[len_start + 2:len_end])
        except ValueError:
            return self._discard_to_next_frame("BodyLength (9) is not a number")
        if body_length < 0 or body_length > MAX_FRAME_BYTES:
            return self._discard_to_next_frame(
                f"BodyLength (9) out of range: {body_length}"
            )

        body_start = len_end + 1
        body_end = body_start + body_length
        # Checksum field is exactly "10=NNN<SOH>" -> 7 bytes.
        frame_end = body_end + 7
        if len(buf) < frame_end:
            # Might just be a short read -- unless the trailer has clearly
            # already gone by, which means BodyLength lied.
            early = buf.find(SOH + b"10=", body_start, min(body_end, len(buf)))
            if early == -1:
                return None, 0
            actual = early + 1 - body_start
            return self._discard_bad_body_length(body_length, actual, early + 1 + 7)

        if buf[body_end:body_end + 3] != b"10=" or buf[frame_end - 1:frame_end] != SOH:
            found = buf.find(SOH + b"10=", body_start)
            if found == -1:
                return self._discard_to_next_frame(
                    f"Bad BodyLength (9): declared {body_length}, no CheckSum found"
                )
            actual = found + 1 - body_start
            return self._discard_bad_body_length(body_length, actual, found + 1 + 7)

        frame = buf[:frame_end]
        declared = frame[body_end + 3:frame_end - 1]
        computed = _checksum(frame[:body_end])
        try:
            declared_value = int(declared)
        except ValueError:
            return (
                DiscardedFrame(frame, f"Bad CheckSum (10): {declared!r} is not a number"),
                frame_end,
            )
        if declared_value != computed:
            return (
                DiscardedFrame(
                    frame,
                    f"Bad CheckSum (10): declared {declared.decode('latin-1')}, "
                    f"computed {computed:03d}",
                ),
                frame_end,
            )

        return self._parse(frame), frame_end

    def _discard_bad_body_length(self, declared: int, actual: int, frame_end: int):
        if len(self._buffer) < frame_end:
            return None, 0
        return (
            DiscardedFrame(
                self._buffer[:frame_end],
                f"Bad BodyLength (9): declared {declared}, computed {actual}",
            ),
            frame_end,
        )

    def _discard_to_next_frame(self, reason: str):
        """Drop bytes up to the next plausible BeginString."""
        nxt = self._buffer.find(self._begin, 1)
        if nxt == -1:
            if len(self._buffer) > MAX_FRAME_BYTES:
                return DiscardedFrame(self._buffer, reason), len(self._buffer)
            return None, 0
        return DiscardedFrame(self._buffer[:nxt], reason), nxt

    @staticmethod
    def _parse(frame: bytes) -> FixMsg:
        parser = simplefix.FixParser()
        parser.append_buffer(frame)
        parsed = parser.get_message()
        pairs: list[tuple[int, str]] = []
        if parsed is not None:
            for tag, value in parsed.pairs:
                pairs.append((int(tag), value.decode("latin-1")))
        return FixMsg(pairs=pairs, raw=frame)
