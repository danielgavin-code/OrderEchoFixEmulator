"""Sequence number stores."""

import json
import os

from orderecho_SeqStore import FileSeqStore, MemorySeqStore


def test_memory_store_round_trip():
    store = MemorySeqStore()
    assert store.load() == (1, 1)
    store.save(4, 9)
    assert store.load() == (4, 9)
    store.reset()
    assert store.load() == (1, 1)


def test_file_store_missing_file_starts_at_one(tmp_path):
    store = FileSeqStore(str(tmp_path / "seqnums"), "ORDERECHO", "AGENT")
    assert store.load() == (1, 1)
    assert not os.path.exists(store.path)


def test_file_store_round_trip(tmp_path):
    directory = str(tmp_path / "seqnums")
    store = FileSeqStore(directory, "ORDERECHO", "AGENT")
    store.save(12, 34)
    assert store.path.endswith(os.path.join("seqnums", "ORDERECHO-AGENT.json"))
    with open(store.path, encoding="utf-8") as handle:
        assert json.load(handle) == {"next_out": 12, "next_in": 34}

    reopened = FileSeqStore(directory, "ORDERECHO", "AGENT")
    assert reopened.load() == (12, 34)


def test_file_store_reset(tmp_path):
    store = FileSeqStore(str(tmp_path / "seqnums"), "ORDERECHO", "AGENT")
    store.save(7, 8)
    store.reset()
    assert store.load() == (1, 1)


def test_file_store_write_is_atomic(tmp_path):
    """No .tmp file is left behind, and the target is replaced whole."""
    directory = str(tmp_path / "seqnums")
    store = FileSeqStore(directory, "ORDERECHO", "AGENT")
    store.save(2, 3)
    store.save(4, 5)
    assert sorted(os.listdir(directory)) == ["ORDERECHO-AGENT.json"]
    assert store.load() == (4, 5)
