"""Minimal asyncio FIX initiator for the integration tests.

Deliberately dumb: it will send any sequence number you ask it to, including
wrong ones, because the tests need that.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from orderecho_Codec import Codec, DiscardedFrame, FixMsg


class FixTestClient:
    def __init__(self, sender_comp_id: str = "AGENT",
                 target_comp_id: str = "ORDERECHO",
                 fix_version: str = "FIX.4.2") -> None:
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.fix_version = fix_version
        self.codec = Codec(fix_version)
        self.next_out = 1
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list = []
        self._pending: list = []

    async def connect(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)

    async def send(self, msg_type: str, body_fields=None, seq: int | None = None,
                   poss_dup: bool = False, orig_sending_time=None,
                   sending_time=None) -> bytes:
        """Send a message.  *seq* overrides the client's own counter."""
        if seq is None:
            seq = self.next_out
            self.next_out += 1
        raw = self.codec.encode(
            msg_type,
            body_fields or [],
            sender_comp_id=self.sender_comp_id,
            target_comp_id=self.target_comp_id,
            seq_num=seq,
            sending_time=sending_time or datetime.now(timezone.utc),
            poss_dup=poss_dup,
            orig_sending_time=orig_sending_time,
        )
        self.writer.write(raw)
        await self.writer.drain()
        return raw

    async def send_raw(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def logon(self, heart_bt_int: int = 30, reset: bool = False,
                    seq: int | None = None) -> bytes:
        body = [(98, "0"), (108, str(heart_bt_int))]
        if reset:
            body.append((141, "Y"))
            # Rewind the counter and let send() allocate from it, so the
            # message after the Logon gets 2 rather than reusing 1.
            self.next_out = 1
        return await self.send("A", body, seq=seq)

    async def recv(self, timeout: float = 5.0):
        """Return the next decoded message (or DiscardedFrame), or None on EOF."""
        while not self._pending:
            try:
                data = await asyncio.wait_for(self.reader.read(65536), timeout)
            except asyncio.TimeoutError:
                return None
            if not data:
                return None
            self._pending.extend(self.codec.decode(data))
        item = self._pending.pop(0)
        self.received.append(item)
        return item

    async def recv_until(self, msg_type: str, timeout: float = 5.0):
        """Read until a message of *msg_type* arrives (or we run dry)."""
        while True:
            msg = await self.recv(timeout)
            if msg is None:
                return None
            if isinstance(msg, FixMsg) and msg.msg_type == msg_type:
                return msg

    async def wait_closed(self, timeout: float = 5.0) -> bool:
        """True if the peer closed the connection within *timeout*."""
        try:
            while True:
                data = await asyncio.wait_for(self.reader.read(65536), timeout)
                if not data:
                    return True
                self._pending.extend(self.codec.decode(data))
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
