"""Open-order persistence .

One JSONL file per session, `<orders_dir>/<session_id>.jsonl`.  Every change
to an order appends one line -- the order's whole state, or a note that it
closed -- so a crash loses at most the line being written.  Loading replays
the file, keeps the latest record per OrderID and drops closed orders; it then
rewrites the file with just the open orders (compaction), and so does every
`compact_every` appends.

Nothing here touches the disk unless something is saved: a session with
`orders.persist: false` never creates the directory.
"""

from __future__ import annotations

import json
import os

FORMAT_VERSION = 1
OP_UPSERT = "upsert"
OP_CLOSE = "close"
DEFAULT_COMPACT_EVERY = 200


class OrderStore:
    """Append-only order records with periodic compaction."""

    def __init__(self, directory: str, session_id: str,
                 compact_every: int = DEFAULT_COMPACT_EVERY) -> None:
        self.directory = directory
        self.session_id = session_id
        self.path = os.path.join(directory, f"{session_id}.jsonl")
        self.compact_every = int(compact_every)
        self._appends = 0
        #: The latest record per open OrderID, as last written.
        self._open: dict = {}

    # ------------------------------------------------------------- writing

    def _append(self, line: dict) -> None:
        os.makedirs(self.directory, exist_ok=True)
        text = json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n"
        # One write per line; flushed and synced so a restart finds it.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        self._appends += 1
        if self.compact_every and self._appends >= self.compact_every:
            self.compact()

    def upsert(self, record: dict) -> None:
        self._open[record["order_id"]] = record
        self._append({"v": FORMAT_VERSION, "op": OP_UPSERT, "order": record})

    def close_order(self, order_id: str) -> None:
        if self._open.pop(order_id, None) is not None:
            self._append({"v": FORMAT_VERSION, "op": OP_CLOSE,
                          "order_id": order_id})

    def compact(self) -> None:
        """Rewrite the file with only the open orders (atomic rename)."""
        self._appends = 0
        if not os.path.exists(self.path) and not self._open:
            return
        os.makedirs(self.directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for record in self._open.values():
                handle.write(json.dumps(
                    {"v": FORMAT_VERSION, "op": OP_UPSERT, "order": record},
                    separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- reading

    def load(self) -> list:
        """Open orders as last written, oldest first; compacts the file.

        A torn last line (a crash mid-write) is skipped, never fatal.
        """
        self._open = {}
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if line.get("op") == OP_UPSERT and isinstance(
                        line.get("order"), dict) and line["order"].get("order_id"):
                    self._open[line["order"]["order_id"]] = line["order"]
                elif line.get("op") == OP_CLOSE:
                    self._open.pop(line.get("order_id"), None)
        records = list(self._open.values())
        self.compact()
        return records
