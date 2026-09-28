"""Sequence number stores.

The session owns the numbers; a store only persists them.

Interface:
    load()  -> (next_out, next_in)
    save(next_out, next_in)
    reset()
"""

from __future__ import annotations

import json
import os


class MemorySeqStore:
    """Non-persistent store, used by unit tests."""

    def __init__(self, next_out: int = 1, next_in: int = 1) -> None:
        self.next_out = int(next_out)
        self.next_in = int(next_in)

    def load(self) -> tuple[int, int]:
        return self.next_out, self.next_in

    def save(self, next_out: int, next_in: int) -> None:
        self.next_out = int(next_out)
        self.next_in = int(next_in)

    def reset(self) -> None:
        self.next_out = 1
        self.next_in = 1


class FileSeqStore:
    """JSON file per session: data/seqnums/<SENDER>-<TARGET>.json."""

    def __init__(self, directory: str, sender_comp_id: str,
                 target_comp_id: str, name: str | None = None) -> None:
        self.directory = directory
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        #: File name.  Defaults to the CompID pair, which is what a
        #: single-session config has always used.
        self.name = name or f"{sender_comp_id}-{target_comp_id}"
        self.path = os.path.join(directory, f"{self.name}.json")

    def load(self) -> tuple[int, int]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return 1, 1
        next_out = int(data.get("next_out", 1))
        next_in = int(data.get("next_in", 1))
        if next_out < 1:
            next_out = 1
        if next_in < 1:
            next_in = 1
        return next_out, next_in

    def save(self, next_out: int, next_in: int) -> None:
        os.makedirs(self.directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        payload = json.dumps({"next_out": int(next_out), "next_in": int(next_in)})
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)

    def reset(self) -> None:
        self.save(1, 1)
