"""Outbound message store.

Every message we send is kept exactly as it went on the wire, keyed by its
MsgSeqNum, so a ResendRequest can be answered with the real thing rather than a
gap fill.  The file is JSONL and is loaded at startup, so replay survives an
engine restart.

Two things are deliberately *not* stored: gap fills and replays, because both
reuse sequence numbers that already belong to an earlier message.  Storing them
would overwrite the original with its own substitute.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

#: Message types that are session plumbing rather than business content.  These
#: are never replayed -- a stale Heartbeat or Logon helps nobody.
ADMIN_MSG_TYPES = frozenset({"0", "1", "2", "3", "4", "5", "A"})


def _timestamp(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return f"{when.strftime('%Y-%m-%dT%H:%M:%S')}.{when.microsecond // 1000:03d}Z"


class MessageStore:
    """Append-only record of outbound messages, indexed by sequence number."""

    def __init__(self, directory: str, sender_comp_id: str,
                 target_comp_id: str, clock=None,
                 name: str | None = None) -> None:
        self.directory = directory
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.clock = clock
        self.name = name or f"{sender_comp_id}-{target_comp_id}"
        self.path = os.path.join(directory, f"{self.name}.jsonl")
        self._messages: dict[int, dict] = {}
        self._handle = None
        self._warned = False
        self.load()

    # ------------------------------------------------------------------ io

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _warn(self, message: str) -> None:
        if not self._warned:
            print(message, file=sys.stderr)
            self._warned = True

    def load(self) -> int:
        """Read the store from disk.  A corrupt line is skipped, not fatal."""
        self._messages = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        self._messages[int(record["seq"])] = record
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn(f"message store: cannot read {self.path}: {exc}")
        return len(self._messages)

    def _open(self):
        if self._handle is None:
            try:
                os.makedirs(self.directory, exist_ok=True)
                self._handle = open(self.path, "a", encoding="utf-8")
            except OSError as exc:
                self._warn(f"message store: cannot open {self.path}: {exc}")
                return None
        return self._handle

    def append(self, seq: int, msg_type: str, raw: bytes,
               injected: bool = False, fix_version: str | None = None) -> dict:
        """Record one outbound message exactly as it was sent."""
        record = {
            "seq": int(seq),
            "msg_type": msg_type,
            "fix_version": fix_version,
            # Real SOH, JSON-escaped; the wire bytes round-trip exactly.
            "raw": raw.decode("latin-1") if isinstance(raw, bytes) else raw,
            "sent_ts": _timestamp(self._now()),
            "injected": bool(injected),
        }
        self._messages[record["seq"]] = record
        handle = self._open()
        if handle is not None:
            try:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
            except OSError as exc:
                self._warn(f"message store: write failed: {exc}")
        return record

    # ------------------------------------------------------------- lookup

    def get(self, seq: int):
        return self._messages.get(int(seq))

    def raw_bytes(self, seq: int):
        record = self.get(seq)
        if record is None:
            return None
        return record["raw"].encode("latin-1")

    def is_admin(self, seq: int) -> bool:
        """True if the seq holds an admin message, or nothing at all.

        A missing sequence number (skipped by an injected gap, or archived) is
        treated the same way: there is nothing worth resending, so it gets
        gap-filled.
        """
        record = self.get(seq)
        if record is None:
            return True
        return record.get("msg_type") in ADMIN_MSG_TYPES

    def __len__(self) -> int:
        return len(self._messages)

    def sequences(self) -> list:
        return sorted(self._messages)

    def stored_version(self) -> str | None:
        """The FIX version the stored messages were sent with, if known.

        Replaying another version's bytes would be worse than not replaying at
        all, so the engine archives the store when this disagrees with the
        session it is about to run.
        """
        for seq in sorted(self._messages, reverse=True):
            version = self._messages[seq].get("fix_version")
            if version:
                return version
        return None

    # ------------------------------------------------------------ archive

    def archive(self) -> str | None:
        """Rename the current file aside and start fresh.

        Called when sequence numbers reset, because seq 1 is about to mean a
        different message.  The old file is kept, never deleted.
        """
        self.close()
        had = len(self._messages)
        self._messages = {}
        if not os.path.exists(self.path):
            return None
        suffix = self._now().astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archived = f"{self.path}.{suffix}"
        counter = 1
        while os.path.exists(archived):
            counter += 1
            archived = f"{self.path}.{suffix}-{counter}"
        try:
            os.replace(self.path, archived)
        except OSError as exc:
            self._warn(f"message store: cannot archive {self.path}: {exc}")
            return None
        return archived if had or True else None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
