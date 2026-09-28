"""JSONL evidence log.

One file per engine run, one JSON object per line, flushed after every line.
This is the machine-readable record; humans read logs/ instead (see
orderecho_Logging).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from orderecho_Codec import to_pipe

KIND_IN = "in"
KIND_OUT = "out"
KIND_DISCARDED = "discarded"
KIND_EVENT = "event"


def make_run_id(when: datetime) -> str:
    """Run id: YYYYMMDD-HHMMSS in UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _timestamp(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return f"{when.strftime('%Y-%m-%dT%H:%M:%S')}.{when.microsecond // 1000:03d}Z"


class EvidenceWriter:
    """Append-only JSONL writer for one engine run."""

    def __init__(self, evidence_dir: str, session_name: str, clock,
                 run_id: str | None = None) -> None:
        self.clock = clock
        self.session_name = session_name
        self.run_id = run_id or make_run_id(clock.now())
        self.directory = evidence_dir
        self.path = os.path.join(evidence_dir, f"{self.run_id}.jsonl")
        self._handle = None
        self._open()

    def _open(self) -> None:
        try:
            os.makedirs(self.directory, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")
        except OSError as exc:  # never take the session down over logging
            self._handle = None
            print(f"evidence: cannot open {self.path}: {exc}", file=sys.stderr)

    def _write(self, record: dict) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(json.dumps(record) + "\n")
            self._handle.flush()
        except OSError as exc:
            print(f"evidence: write failed: {exc}", file=sys.stderr)

    def _record(self, kind: str, seq=None, msg_type=None, raw=None,
                fields=None, detail=None, order=None,
                injected: bool = False, session: str | None = None) -> dict:
        return {
            "ts": _timestamp(self.clock.now()),
            "run_id": self.run_id,
            "kind": kind,
            # One evidence file per engine run, so each record says which
            # session it belongs to.
            "session": session or self.session_name,
            "seq": seq,
            "msg_type": msg_type,
            "raw": to_pipe(raw) if raw is not None else None,
            "fields": fields,
            "detail": detail,
            "order": order,
            "injected": bool(injected),
        }

    def message(self, kind: str, msg, seq=None, detail=None, order=None,
                injected: bool = False, session: str | None = None) -> dict:
        """Record an inbound or outbound message."""
        if seq is None:
            seq = getattr(msg, "seq_num", None)
        record = self._record(
            kind,
            seq=int(seq) if seq is not None else None,
            msg_type=msg.msg_type,
            raw=msg.raw,
            fields=msg.fields(),
            detail=detail,
            order=order,
            injected=injected,
            session=session,
        )
        self._write(record)
        return record

    def discarded(self, frame, injected: bool = False,
                  session: str | None = None) -> dict:
        record = self._record(
            KIND_DISCARDED,
            raw=frame.raw,
            detail=getattr(frame, "reason", None),
            injected=injected,
            session=session,
        )
        self._write(record)
        return record

    def event(self, event: str, detail: str = "", order=None,
              injected: bool = False, session: str | None = None) -> dict:
        """Record a non-message event.

        The schema has no dedicated event field, so the event name is written
        at the front of the free-text detail.  ``order`` carries an order
        snapshot for application events and is null for everything else.
        """
        text = f"{event}: {detail}" if detail else event
        record = self._record(KIND_EVENT, detail=text, order=order,
                              injected=injected, session=session)
        self._write(record)
        return record

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
