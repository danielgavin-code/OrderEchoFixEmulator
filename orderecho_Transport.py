"""asyncio TCP acceptor.

Owns the socket, the codec buffer and the once-per-second timer tick; the
session owns every decision.  One session at a time: a second connection is
logged and closed immediately.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from orderecho_Clock import SystemClock
from orderecho_Codec import Codec, DiscardedFrame, FixMsg, format_time, to_pipe
from orderecho_Evidence import KIND_IN, KIND_OUT, EvidenceWriter, make_run_id
from orderecho_Logging import build_logs
from orderecho_FixVersion import profile_for
from orderecho_Logging import FixMessageLog
from orderecho_MessageStore import MessageStore
from orderecho_OrderBook import IdGenerator, OrderBook
from orderecho_Pricing import (
    build_price_source,
    quiet_third_party_logging,
    resolve_quote,
)
from orderecho_Session import (
    Disconnect,
    Evidence,
    Replay,
    RequestPrice,
    Send,
    Session,
    State,
)
from orderecho_SeqStore import FileSeqStore

READ_CHUNK = 65536
#: Fallback when a config predates engine.logon_timeout_sec.
ROUTE_TIMEOUT = 30.0
#: 100ms so fill delays in the hundreds of milliseconds are honoured.
TIMER_INTERVAL = 0.1

TAG_ORD_TYPE = 40
TAG_SYMBOL = 55
ORD_TYPE_MARKET = "1"
ORD_TYPE_LIMIT = "2"
MSG_NEW_ORDER_SINGLE = "D"
MSG_ORDER_CANCEL_REPLACE_REQUEST = "G"
MSG_LOGON = "A"
TAG_BEGIN_STRING = 8
TAG_SENDER_COMP_ID = 49
TAG_TARGET_COMP_ID = 56

#: Evidence events whose detail is already a finished human log line.
LIFECYCLE_EVENT = "order lifecycle"
#: Events whose detail is already a finished, column-aligned log line.
VERBATIM_LOG_EVENTS = (LIFECYCLE_EVENT, "price band", "resend summary")
#: Finished log lines that deserve a WARNING rather than an INFO.
VERBATIM_WARN_EVENTS = ("price band warning",)
#: Evidence events that carry an order snapshot; the lifecycle line says it
#: better, so these go to the engine log at DEBUG only.
SNAPSHOT_EVENTS = ("order", "cancel reject",
                   "price band pass", "price band reject",
                   "price band skipped")

#: How many in/out messages the /messages endpoint can look back over.
MESSAGE_RING_SIZE = 1000

FRAMING_TAGS = (8, 9, 10)
TAG_POSS_DUP_FLAG = 43
TAG_SENDING_TIME = 52
TAG_ORIG_SENDING_TIME = 122


class InjectionError(Exception):
    """An injection request that cannot be honoured."""


@dataclass
class Mutation:
    """A one-shot mutation queued against future outbound messages."""

    mutation_id: int
    msg_type: str | None = None
    set_fields: dict = field(default_factory=dict)
    remove_tags: list = field(default_factory=list)
    corrupt_checksum: bool = False
    count: int = 1
    remaining: int = 1

    def matches(self, msg_type: str) -> bool:
        return self.msg_type is None or self.msg_type == msg_type

    def describe(self) -> str:
        parts = []
        if self.set_fields:
            parts.append("set " + ",".join(
                f"{tag}={value}" for tag, value in self.set_fields.items()))
        if self.remove_tags:
            parts.append("remove " + ",".join(str(t) for t in self.remove_tags))
        if self.corrupt_checksum:
            parts.append("corrupt checksum")
        return "; ".join(parts) or "no-op"

    def as_dict(self) -> dict:
        return {
            "id": self.mutation_id,
            "msg_type": self.msg_type,
            "set": dict(self.set_fields),
            "remove": list(self.remove_tags),
            "corrupt_checksum": self.corrupt_checksum,
            "count": self.count,
            "remaining": self.remaining,
        }


class Injector:
    """Queue of deliberate mutations for the outbound path.

    Mischief is opt-in, one-shot and always recorded: nothing here changes
    what the emulator does unless a control API call asked for it.
    """

    def __init__(self) -> None:
        self.pending: list = []
        self._next_id = 0

    def queue(self, msg_type=None, set_fields=None, remove_tags=None,
              corrupt_checksum: bool = False, count: int = 1) -> Mutation:
        count = int(count)
        if count < 1:
            raise InjectionError("count must be >= 1")
        clean_set = {}
        for tag, value in (set_fields or {}).items():
            tag_number = _tag_number(tag)
            if tag_number in FRAMING_TAGS:
                raise InjectionError(
                    f"tag {tag_number} is framing and cannot be set; use "
                    f"corrupt_checksum to break the CheckSum"
                )
            clean_set[tag_number] = str(value)
        clean_remove = []
        for tag in (remove_tags or ()):
            tag_number = _tag_number(tag)
            if tag_number in FRAMING_TAGS:
                raise InjectionError(
                    f"tag {tag_number} is framing and cannot be removed"
                )
            clean_remove.append(tag_number)

        self._next_id += 1
        mutation = Mutation(
            mutation_id=self._next_id,
            msg_type=str(msg_type) if msg_type is not None else None,
            set_fields=clean_set,
            remove_tags=clean_remove,
            corrupt_checksum=bool(corrupt_checksum),
            count=count,
            remaining=count,
        )
        self.pending.append(mutation)
        return mutation

    def take(self, msg_type: str):
        """The first queued mutation that matches, consuming one use."""
        for mutation in list(self.pending):
            if mutation.matches(msg_type):
                mutation.remaining -= 1
                if mutation.remaining <= 0:
                    self.pending.remove(mutation)
                return mutation
        return None

    def clear(self) -> int:
        count = len(self.pending)
        self.pending = []
        return count

    def drain(self) -> list:
        """Clear and return what was pending, for reporting."""
        dropped, self.pending = self.pending, []
        return dropped

    def as_list(self) -> list:
        return [mutation.as_dict() for mutation in self.pending]


def _tag_number(tag) -> int:
    try:
        return int(tag)
    except (TypeError, ValueError):
        raise InjectionError(f"tag {tag!r} is not a number") from None


def apply_mutation(codec: Codec, raw: bytes, mutation: Mutation) -> bytes:
    """Return *raw* with the mutation applied and framing recomputed."""
    decoded = Codec(codec.fix_version).decode(raw)
    if not decoded or not isinstance(decoded[0], FixMsg):
        return raw
    pairs = list(decoded[0].pairs)

    if mutation.remove_tags:
        pairs = [(tag, value) for tag, value in pairs
                 if tag not in mutation.remove_tags]

    for tag, value in mutation.set_fields.items():
        replaced = False
        for index, (pair_tag, _pair_value) in enumerate(pairs):
            if pair_tag == tag:
                pairs[index] = (tag, value)
                replaced = True
                break
        if not replaced:
            # Append before the trailer; rebuild() drops 8/9/10 anyway.
            pairs.append((tag, value))

    return codec.rebuild(pairs, corrupt_checksum=mutation.corrupt_checksum)


class SessionRuntime:
    """Everything one FIX session owns.

    The engine owns pricing, evidence, the engine log and the ID generator;
    a runtime owns its own sequence numbers, message store, order book,
    injection queue, FIX log and connection.  Nothing here is shared, so two
    sessions cannot interfere with each other.
    """

    def __init__(self, engine, spec) -> None:
        self.engine = engine
        self.spec = spec
        self.id = spec.id
        self.config = spec.session          # this session's SessionConfig
        self.clock = engine.clock
        self.evidence = engine.evidence
        self.engine_log = engine.engine_log
        self.profile = profile_for(spec.fix_version)

        self.seq_store = engine.seq_store_for(spec)
        self.message_store = MessageStore(
            engine.config.storage.msgstore_dir,
            spec.sender_comp_id, spec.target_comp_id,
            clock=self.clock, name=spec.id,
        )
        self._check_stored_version()

        self.fix_log = engine.fix_log_for(spec)

        self.order_book = None
        if spec.rules is not None:
            self.order_book = OrderBook(
                spec.orders, spec.rules, self.clock, engine.run_id,
                price_band=spec.price_band, profile=self.profile,
                id_generator=engine.ids,
            )

        self.injector = Injector()
        self.messages: deque = deque(maxlen=MESSAGE_RING_SIZE)
        self.last_outbound_raw: bytes | None = None
        self.peer = None

        self.session: Session | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._codec: Codec | None = None
        self._busy = False
        self._closing = False
        self._timer_task: asyncio.Task | None = None
        self._price_tasks: set = set()
        self._session_done: asyncio.Event = asyncio.Event()
        self._session_done.set()

    # ------------------------------------------------------------ logging

    def log_info(self, message: str) -> None:
        self.engine_log.info(message, session=self.id)

    def log_warning(self, message: str) -> None:
        self.engine_log.warning(message, session=self.id)

    def log_debug(self, message: str) -> None:
        self.engine_log.debug(message, session=self.id)

    def log_exception(self, message: str) -> None:
        self.engine_log.exception(message, session=self.id)

    def record_event(self, event: str, detail: str = "", order=None,
                     injected: bool = False) -> None:
        self.evidence.event(event, detail, order=order, injected=injected,
                            session=self.id)

    def _check_stored_version(self) -> None:
        if not len(self.message_store):
            return
        stored_version = self.message_store.stored_version()
        if stored_version and stored_version != self.spec.fix_version:
            self.engine_log.warning(
                f"Message store holds {stored_version} messages but this "
                f"session is {self.spec.fix_version}; archiving so replay "
                f"never sends another version's bytes", session=self.id,
            )
            archived = self.message_store.archive()
            self.evidence.event(
                "store archived",
                f"{archived} (FIX version changed {stored_version} -> "
                f"{self.spec.fix_version})", session=self.id,
            )
        else:
            self.engine_log.info(
                f"Message store: {len(self.message_store)} outbound "
                f"message(s) loaded from {self.message_store.path}",
                session=self.id,
            )

    def close(self) -> None:
        self.fix_log.close()
        self.message_store.close()

    def archive_message_store(self, why: str) -> str | None:
        archived = self.message_store.archive()
        if archived is not None:
            self.log_info(f"Outbound message store archived to {archived} "
                          f"({why})")
            self.record_event("store archived", f"{archived} ({why})")
        return archived

    # --------------------------------------------------------- connection

    async def handle_connection(self, reader, writer, peer, pending, leftover,
                                expected_versions=None) -> None:
        """Take ownership of a routed connection and run it to the end."""
        self._busy = True
        self.peer = peer
        self._closing = False
        self._session_done = asyncio.Event()
        self._writer = writer
        self._codec = Codec(self.spec.fix_version)
        self.session = Session(self.config, self.seq_store, self.clock,
                               app=self.order_book,
                               message_store=self.message_store)
        if expected_versions:
            self.session.expected_versions = list(expected_versions)
        self.log_info(f"Connection accepted from {peer}")

        queue = list(pending)
        if leftover:
            queue.extend(self._codec.decode(leftover))

        try:
            self._run_actions(self.session.on_connect())
            self._log_state("DISCONNECTED", self.session.state.value)
            self._timer_task = asyncio.create_task(self._timer_loop())
            await self._read_loop(reader, queue)
        except Exception:
            self.log_exception("Unhandled error in session loop")
        finally:
            if self._timer_task is not None:
                self._timer_task.cancel()
                try:
                    await self._timer_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._timer_task = None
            previous = self.session.state.value if self.session else "?"
            if self.session is not None:
                self._run_actions(self.session.on_disconnect(),
                                  dispatch_only=True)
                self._log_state(previous, self.session.state.value)
            self._drop_pending_injections()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self.log_info(f"Connection closed, peer={peer}")
            self._writer = None
            self._codec = None
            self._busy = False
            self.peer = None
            self._session_done.set()

    def _drop_pending_injections(self) -> None:
        """Mischief never outlives the session that asked for it."""
        dropped = self.injector.drain()
        if not dropped:
            return
        detail = "; ".join(
            f"#{mutation.mutation_id} "
            f"{mutation.msg_type or 'any'}: {mutation.describe()} "
            f"({mutation.remaining} use(s) left)"
            for mutation in dropped
        )
        self.record_event("pending injections dropped on disconnect", detail)
        self.log_warning(f"pending injections dropped on disconnect: {detail}")

    async def _read_loop(self, reader, queue=None) -> None:
        queue = list(queue or ())
        while not self._closing:
            if not queue:
                try:
                    data = await reader.read(READ_CHUNK)
                except (ConnectionResetError, BrokenPipeError) as exc:
                    self.log_warning(f"Read failed: {exc}")
                    break
                if not data:
                    break
                queue = self._codec.decode(data)
                if not queue:
                    continue
            item = queue.pop(0)
            await self._handle_item(item)
            if self._closing:
                return

    async def _handle_item(self, item) -> None:
        if isinstance(item, DiscardedFrame):
            self.log_warning(f"Discarded frame: {item.reason}")
            self.fix_log.discarded(item)
            self.evidence.discarded(item, session=self.id)
            self._run_actions(self.session.on_discarded(item))
            return
        self.fix_log.inbound(item)
        self.evidence.message(KIND_IN, item, session=self.id)
        self._remember(KIND_IN, item.seq_num, item.msg_type, item.raw)
        market_price = await self._quote_for(item)
        before = self.session.state.value
        self._run_actions(self.session.on_message(item, market_price))
        self._log_state(before, self.session.state.value)

    async def _quote_for(self, msg):
        """Resolve the reference quote a limit order needs, if any."""
        if self.order_book is None:
            return None
        if msg.msg_type not in (MSG_NEW_ORDER_SINGLE,
                                MSG_ORDER_CANCEL_REPLACE_REQUEST):
            return None
        if msg.get(TAG_ORD_TYPE) != ORD_TYPE_LIMIT:
            return None
        band = self.spec.price_band
        if band is None or not band.enabled:
            return None
        symbol = msg.get(TAG_SYMBOL)
        if not symbol:
            return None
        return await resolve_quote(self.engine.price_source, symbol,
                                   band.lookup_timeout_sec)

    def _do_request_price(self, action: RequestPrice) -> None:
        task = asyncio.create_task(
            self._resolve_price_later(action.order_id, action.symbol)
        )
        self._price_tasks.add(task)
        task.add_done_callback(self._price_tasks.discard)

    async def _resolve_price_later(self, order_id: str, symbol: str) -> None:
        try:
            quote = await resolve_quote(
                self.engine.price_source, symbol,
                self.engine.config.pricing.timeout_sec,
            )
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:
            self.log_exception(f"Price lookup for {symbol} failed")
            return
        if self.session is None:
            return
        try:
            before = self.session.state.value
            self._run_actions(self.session.on_price(order_id, quote))
            self._log_state(before, self.session.state.value)
        except Exception:
            self.log_exception("Failed to apply a resolved price")

    async def _timer_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(TIMER_INTERVAL)
                if self._closing or self.session is None:
                    return
                before = self.session.state.value
                self._run_actions(self.session.on_timer())
                self._log_state(before, self.session.state.value)
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception:
            self.log_exception("Unhandled error in timer loop")

    async def wait_for_session_end(self, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(self._session_done.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def cancel_price_tasks(self) -> None:
        for task in list(self._price_tasks):
            task.cancel()
        self._price_tasks.clear()

    # ------------------------------------------------------------ actions

    def _run_actions(self, actions, dispatch_only: bool = False,
                     collect: list | None = None) -> None:
        for action in actions or ():
            self.log_debug(f"Action: {action}")
            if isinstance(action, Send):
                summary = self._do_send(action)
                if collect is not None and summary is not None:
                    collect.append(summary)
            elif isinstance(action, Evidence):
                self._do_evidence(action)
            elif isinstance(action, RequestPrice):
                self._do_request_price(action)
            elif isinstance(action, Replay):
                summary = self._do_replay(action)
                if collect is not None and summary is not None:
                    collect.append(summary)
            elif isinstance(action, Disconnect):
                if dispatch_only:
                    continue
                self._do_disconnect(action)
            else:  # pragma: no cover - defensive
                self.log_warning(f"Unknown action: {action!r}")

    def _do_send(self, action: Send):
        codec = self._codec or Codec(self.spec.fix_version)
        try:
            raw = codec.encode(
                action.msg_type,
                action.body_fields,
                sender_comp_id=self.spec.sender_comp_id,
                target_comp_id=self.spec.target_comp_id,
                seq_num=action.seq,
                sending_time=self.clock.now(),
                poss_dup=action.poss_dup,
                orig_sending_time=action.orig_sending_time,
            )
        except Exception:
            self.log_exception(f"Could not encode {action.msg_type}")
            return None

        injected_detail = None
        mutation = self.injector.take(action.msg_type)
        if mutation is not None:
            try:
                raw = apply_mutation(codec, raw, mutation)
                injected_detail = mutation.describe()
                self.log_warning(
                    f"Injected into outbound {action.msg_type} "
                    f"seq={action.seq}: {injected_detail}"
                )
            except Exception:
                self.log_exception("Could not apply injection")

        return self._write_raw(raw, action.msg_type, action.seq,
                               injected_detail=injected_detail,
                               note_resend=(action.msg_type == "2"),
                               store=action.seq_override is None)

    def _write_raw(self, raw: bytes, msg_type: str, seq,
                   injected_detail: str | None = None,
                   note_resend: bool = False, store: bool = True,
                   injected: bool | None = None,
                   evidence_detail: str | None = None,
                   log_comment: str | None = None):
        """Put bytes on the wire and record them everywhere they belong."""
        msg = FixMsg(pairs=[], raw=raw)
        decoded = Codec(self.spec.fix_version).decode(raw)
        if decoded and isinstance(decoded[0], FixMsg):
            msg = decoded[0]

        if self._writer is not None:
            try:
                self._writer.write(raw)
            except Exception:
                self.log_exception("Write to socket failed")

        if injected is None:
            injected = injected_detail is not None
        if log_comment is None and injected_detail:
            log_comment = (f"injected: {injected_detail}" if injected
                           else injected_detail)
        if evidence_detail is None and injected_detail:
            evidence_detail = log_comment
        self.fix_log.log("OUT", seq, msg_type, raw, log_comment)
        self.evidence.message(KIND_OUT, msg, seq=seq, detail=evidence_detail,
                              injected=injected, session=self.id)
        self._remember(KIND_OUT, seq, msg_type, raw, injected=injected)
        self.last_outbound_raw = raw
        if store:
            # Gap fills and replays reuse sequence numbers that already belong
            # to an earlier message, so they are never stored over it.
            self.message_store.append(
                seq, msg_type, raw, injected=injected,
                fix_version=self.spec.fix_version,
            )

        if note_resend:
            self.log_info(f"ResendRequest sent: 7={msg.get(7)} 16={msg.get(16)}")
        return {"seq": seq, "msg_type": msg_type, "raw": to_pipe(raw),
                "injected": injected}

    @staticmethod
    def _with_poss_dup(pairs: list, orig_sending_time, new_sending_time=None):
        """Insert 43=Y and 122 right after 52, per the Cook 1 field order."""
        pairs = [(tag, value) for tag, value in pairs
                 if tag not in (TAG_POSS_DUP_FLAG, TAG_ORIG_SENDING_TIME)]
        insert_at = len(pairs)
        for index, (tag, value) in enumerate(pairs):
            if tag == TAG_SENDING_TIME:
                if new_sending_time is not None:
                    pairs[index] = (tag, new_sending_time)
                insert_at = index + 1
                break
        extra = [(TAG_POSS_DUP_FLAG, "Y")]
        if orig_sending_time is not None:
            extra.append((TAG_ORIG_SENDING_TIME, orig_sending_time))
        pairs[insert_at:insert_at] = extra
        return pairs

    def _do_replay(self, action: Replay):
        """Resend a stored message on its original sequence number."""
        codec = self._codec or Codec(self.spec.fix_version)
        raw = action.raw.encode("latin-1") if isinstance(action.raw, str) \
            else action.raw
        decoded = Codec(self.spec.fix_version).decode(raw)
        if not decoded or not isinstance(decoded[0], FixMsg):
            self.log_warning(
                f"Cannot replay seq {action.seq}: stored message will not decode"
            )
            return None
        original = decoded[0]
        pairs = self._with_poss_dup(
            list(original.pairs),
            action.orig_sending_time or original.get(TAG_SENDING_TIME),
            new_sending_time=format_time(self.clock.now()),
        )
        rebuilt = codec.rebuild(pairs)
        detail = (f"replay of injected seq {action.seq}" if action.injected
                  else f"replay of seq {action.seq}")
        return self._write_raw(
            rebuilt, action.msg_type or original.msg_type, action.seq,
            store=False, injected=action.injected,
            evidence_detail=detail,
            log_comment=(f"injected: {detail}" if action.injected else detail),
        )

    def _remember(self, kind: str, seq, msg_type, raw, injected: bool = False):
        self.messages.append({
            "ts": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "kind": kind,
            "seq": int(seq) if seq is not None else None,
            "msg_type": msg_type,
            "raw": to_pipe(raw),
            "injected": injected,
        })

    def _do_evidence(self, action: Evidence) -> None:
        order = getattr(action, "order", None)
        self.record_event(action.event, action.detail, order=order)
        if action.event in VERBATIM_WARN_EVENTS:
            self.log_warning(action.detail)
            return
        if action.event in VERBATIM_LOG_EVENTS:
            # Already a finished, column-aligned log line; print it as-is.
            self.log_info(action.detail)
            return
        detail = (f"{action.event}: {action.detail}" if action.detail
                  else action.event)
        if action.event in SNAPSHOT_EVENTS:
            self.log_debug(detail)
        elif action.event in ("seq gap detected", "frame discarded",
                              "resend already outstanding", "possdup ignored",
                              "fill clamped", "fill skipped",
                              "pending injections dropped on disconnect",
                              "scheduled order events deferred until logon"):
            self.log_warning(detail)
        else:
            self.log_info(detail)

    def _do_disconnect(self, action: Disconnect) -> None:
        if self._closing:
            return
        self._closing = True
        self.log_info(f"Disconnecting: {action.reason}")
        self.record_event("disconnecting", action.reason)
        if self._writer is not None:
            try:
                self._writer.write(b"")
                self._writer.close()
            except Exception:  # pragma: no cover
                pass

    async def close_connection(self) -> None:
        if self._writer is not None:
            self._closing = True
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
        await self.wait_for_session_end(timeout=2.0)

    def _log_state(self, before: str, after: str) -> None:
        if before != after:
            self.log_info(f"State {before} -> {after}")

    # -------------------------------------------------- control API hooks

    @property
    def session_active(self) -> bool:
        return self.session is not None and self.session.state is State.ACTIVE

    def run_app_actions(self, app_actions) -> list:
        if self.session is None:
            return []
        sent: list = []
        before = self.session.state.value
        self._run_actions(self.session._run_app(app_actions), collect=sent)
        self._log_state(before, self.session.state.value)
        return sent

    def run_session_actions(self, actions) -> list:
        sent: list = []
        before = self.session.state.value if self.session else "?"
        self._run_actions(actions, collect=sent)
        if self.session is not None:
            self._log_state(before, self.session.state.value)
        return sent

    def send_test_request(self) -> list:
        session = self.session
        session.test_req_counter += 1
        test_req_id = f"TEST-{session.test_req_counter}"
        session.pending_test_req_id = test_req_id
        session.pending_test_req_at = self.clock.now()
        action = session._send("1", [(112, test_req_id)])
        return self.run_session_actions([action])

    def force_disconnect(self,
                         reason: str = "Disconnected via control API") -> None:
        self.log_warning(f"Injected disconnect: {reason}")
        self.record_event("injected disconnect", reason, injected=True)
        self._closing = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # pragma: no cover
                pass

    def inject_seq_gap(self, skip: int) -> int:
        if skip < 1:
            raise InjectionError("skip must be >= 1")
        session = self.session
        before = session.next_out
        session.next_out += int(skip)
        session._persist()
        detail = (f"outbound MsgSeqNum advanced {before} -> {session.next_out} "
                  f"without sending {skip} message(s)")
        self.log_warning(f"Injected sequence gap: {detail}")
        self.record_event("injected seq gap", detail, injected=True)
        return session.next_out

    def duplicate_last(self, poss_dup: bool = False) -> list:
        last_raw = self.last_outbound_raw
        if last_raw is None and self.session is not None:
            last_raw = self.message_store.raw_bytes(self.session.next_out - 1)
        if last_raw is None:
            raise InjectionError("nothing has been sent yet on this connection")
        codec = self._codec or Codec(self.spec.fix_version)
        decoded = Codec(self.spec.fix_version).decode(last_raw)
        if not decoded or not isinstance(decoded[0], FixMsg):
            raise InjectionError("the last outbound message cannot be decoded")
        original = decoded[0]
        pairs = list(original.pairs)
        if poss_dup:
            pairs = self._with_poss_dup(pairs, original.get(TAG_SENDING_TIME))

        raw = codec.rebuild(pairs)
        detail = ("duplicate of the last outbound message"
                  + (" with 43=Y and 122" if poss_dup else ""))
        summary = self._write_raw(raw, original.msg_type, original.seq_num,
                                  injected_detail=detail, store=False)
        return [summary] if summary is not None else []

    def recent_messages(self, limit: int = 50, direction: str = "all") -> list:
        rows = list(self.messages)
        if direction in (KIND_IN, KIND_OUT):
            rows = [row for row in rows if row["kind"] == direction]
        if limit is not None and limit > 0:
            rows = rows[-limit:]
        return rows


class Transport:
    """The engine: acceptors, routing, and the sessions themselves.

    One acceptor per distinct port.  A Logon is routed by the FIX identity
    triple -- BeginString and both CompIDs -- so two sessions can share a port
    and CompIDs as long as their versions differ.

    For a single-session config every legacy attribute still points at that
    one session, so single-session callers keep working unchanged.
    """

    def __init__(self, config, clock=None, seq_store=None, evidence=None,
                 fix_log=None, engine_log=None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        # Engine-wide log and evidence lines belong to no single session.
        # A one-session engine keeps the old name so its files and log lines
        # are unchanged.
        self.session_name = (config.session.name if len(config.sessions) == 1
                             else "engine")
        self.run_id = make_run_id(self.clock.now())

        self.evidence = evidence or EvidenceWriter(
            config.storage.evidence_dir, self.session_name, self.clock,
            run_id=self.run_id,
        )
        if fix_log is None or engine_log is None:
            built_fix, built_engine = build_logs(config, self.session_name,
                                                 self.clock)
            fix_log = fix_log or built_fix
            engine_log = engine_log or built_engine
        self.engine_log = engine_log

        # Injected overrides only make sense for a one-session engine.
        self._seq_store_override = seq_store
        self._fix_log_override = fix_log

        self.price_source = build_price_source(
            config, self.clock, engine_log=self.engine_log
        )
        # Keep third-party chatter off the terminal and in the engine log.
        quiet_third_party_logging(self.engine_log)

        #: OrderIDs and ExecIDs are unique across every session.
        self.ids = IdGenerator(self.run_id)

        self.runtimes: dict = {}
        for spec in config.sessions:
            self.runtimes[spec.id] = SessionRuntime(self, spec)

        self.servers: dict = {}
        self.port: int | None = None

    # ----------------------------------------------------- session lookup

    def seq_store_for(self, spec):
        if self._seq_store_override is not None and len(self.config.sessions) == 1:
            return self._seq_store_override
        return FileSeqStore(self.config.storage.seqnum_dir,
                            spec.sender_comp_id, spec.target_comp_id,
                            name=spec.id)

    def fix_log_for(self, spec):
        if self._fix_log_override is not None and len(self.config.sessions) == 1:
            return self._fix_log_override
        return FixMessageLog(
            self.config.logging.log_dir, spec.id, self.clock,
            delimiter=self.config.logging.fix_delimiter,
            console=self.config.logging.console,
        )

    @property
    def session_ids(self) -> list:
        return list(self.runtimes)

    def runtime(self, session_id: str):
        return self.runtimes.get(session_id)

    @property
    def default_runtime(self):
        """The session the unscoped API routes act on, or None if ambiguous."""
        if len(self.runtimes) == 1:
            return next(iter(self.runtimes.values()))
        wanted = self.config.engine.default_session
        if wanted:
            return self.runtimes.get(wanted)
        return None

    def runtime_for_order(self, order_id: str):
        """Which session owns an order.  OrderIDs are unique engine-wide."""
        for runtime in self.runtimes.values():
            book = runtime.order_book
            if book is not None and book.get_order(order_id) is not None:
                return runtime
        return None

    # --------------------------------------------------------------- server

    async def start(self, port: int | None = None) -> asyncio.AbstractServer:
        """Bind one acceptor per distinct port."""
        if port is not None and len(self.config.sessions) == 1:
            self.config.sessions[0].session.port = port
            self.config.session.port = port

        host = self.config.engine.host or self.config.session.host
        first = None
        for wanted_port in self.config.ports:
            specs = [spec for spec in self.config.sessions
                     if spec.port == wanted_port]
            server = await asyncio.start_server(
                self._client_handler(wanted_port), host, wanted_port
            )
            bound = (server.sockets[0].getsockname()[1] if server.sockets
                     else wanted_port)
            # Port 0 means "any free port"; remember what we actually got so
            # routing and /sessions report the truth.
            for spec in specs:
                spec.session.port = bound
            if self.config.session.port == wanted_port:
                self.config.session.port = bound
            self.servers[bound] = server
            if first is None:
                first = server
                self.port = bound
            self.engine_log.info(
                f"Acceptor listening on {host}:{bound} for "
                f"{', '.join(spec.id for spec in specs)} (run_id={self.run_id})"
            )
        return first

    def _client_handler(self, port: int):
        async def handler(reader, writer):
            await self._handle_client(reader, writer, port)
        return handler

    async def serve_forever(self) -> None:
        if not self.servers:
            await self.start()
        await asyncio.gather(*(server.serve_forever()
                               for server in self.servers.values()))

    async def stop(self) -> None:
        for runtime in self.runtimes.values():
            runtime.cancel_price_tasks()
        for server in list(self.servers.values()):
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # pragma: no cover - platform dependent
                pass
        self.servers = {}
        for runtime in self.runtimes.values():
            await runtime.close_connection()
        self.engine_log.info("Acceptor stopped")

    def close_logs(self) -> None:
        self.evidence.close()
        for runtime in self.runtimes.values():
            runtime.close()
        if self._fix_log_override is not None:
            self._fix_log_override.close()
        self.engine_log.close()

    # -------------------------------------------------------------- routing

    async def _handle_client(self, reader, writer, port: int) -> None:
        peer = writer.get_extra_info("peername")
        try:
            routed = await self._route(reader, writer, peer, port)
        except Exception:
            self.engine_log.exception(f"Routing failed for {peer}")
            routed = None
        if routed is None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass

    async def _route(self, reader, writer, peer, port: int):
        """Read the first message and hand the connection to its session."""
        specs = [spec for spec in self.config.sessions if spec.port == port]
        if not specs:                 # pragma: no cover - defensive
            return None

        # If every session on this port is already connected, no Logon could
        # be accepted, so say no now rather than make them introduce
        # themselves first.
        if all(self.runtimes[spec.id]._busy for spec in specs):
            busy = ", ".join(spec.id for spec in specs)
            self.engine_log.warning(
                f"Refused second connection from {peer}: a session is "
                f"already active ({busy})"
            )
            self.evidence.event("connection refused", f"peer={peer}")
            return None

        scratch = Codec(specs[0].fix_version)
        first = None
        pending: list = []
        logon_timeout = getattr(self.config.engine, "logon_timeout_sec",
                                ROUTE_TIMEOUT)
        deadline = asyncio.get_running_loop().time() + logon_timeout
        while first is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.engine_log.warning(
                    f"No Logon from {peer} within {logon_timeout:g}s; "
                    f"dropping the connection"
                )
                return None
            try:
                data = await asyncio.wait_for(reader.read(READ_CHUNK),
                                              remaining)
            except (ConnectionResetError, BrokenPipeError,
                    asyncio.TimeoutError):
                return None
            if not data:
                return None
            for item in scratch.decode(data):
                if first is None and isinstance(item, FixMsg):
                    first = item
                else:
                    pending.append(item)
            if first is None and len(scratch.buffer) > READ_CHUNK * 16:
                return None            # nothing usable is coming

        if first.msg_type != MSG_LOGON:
            self.engine_log.warning(
                f"First message not Logon from {peer}: 35={first.msg_type}; "
                f"dropping the connection"
            )
            self.evidence.event(
                "first message not logon",
                f"peer={peer} 35={first.msg_type}",
            )
            return None

        begin_string = first.get(TAG_BEGIN_STRING)
        their_target = first.get(TAG_TARGET_COMP_ID)    # our SenderCompID
        their_sender = first.get(TAG_SENDER_COMP_ID)    # our TargetCompID

        candidates = [
            spec for spec in specs
            if spec.sender_comp_id == their_target
            and spec.target_comp_id == their_sender
        ]
        if not candidates:
            detail = (f"8={begin_string} 49={their_sender} "
                      f"56={their_target} from {peer}")
            self.engine_log.warning(f"unknown session: {detail}")
            self.evidence.event("unknown session", detail)
            return None

        matching = [spec for spec in candidates
                    if spec.fix_version == begin_string]
        expected_versions = None
        if matching:
            spec = matching[0]
        else:
            # The CompIDs are ours but the dialect is not; the session tells
            # them so, naming every version it could have accepted.
            spec = candidates[0]
            expected_versions = [candidate.fix_version
                                 for candidate in candidates]

        runtime = self.runtimes[spec.id]
        if runtime._busy:
            self.engine_log.warning(
                f"Refused second connection from {peer}: a session is "
                f"already active ({spec.id})", session=spec.id,
            )
            self.evidence.event("connection refused", f"peer={peer}",
                                session=spec.id)
            return None

        await runtime.handle_connection(
            reader, writer, peer, [first] + pending, scratch.buffer,
            expected_versions=expected_versions,
        )
        return runtime

    # ------------------------------------------------- single-session views
    #
    # Everything below keeps a one-session engine looking exactly as it did
    # when it was the only one.

    def _only(self):
        runtime = self.default_runtime
        if runtime is None:
            raise LookupError(
                "this engine has several sessions; name one "
                f"({', '.join(self.session_ids)})"
            )
        return runtime

    @property
    def session(self):
        runtime = self.default_runtime
        return runtime.session if runtime else None

    @property
    def order_book(self):
        runtime = self.default_runtime
        return runtime.order_book if runtime else None

    @property
    def message_store(self):
        return self._only().message_store

    @property
    def seq_store(self):
        return self._only().seq_store

    @property
    def fix_log(self):
        return self._only().fix_log

    @property
    def injector(self):
        return self._only().injector

    @property
    def messages(self):
        return self._only().messages

    @property
    def profile(self):
        return self._only().profile

    @property
    def peer(self):
        runtime = self.default_runtime
        return runtime.peer if runtime else None

    @property
    def _busy(self):
        runtime = self.default_runtime
        return bool(runtime and runtime._busy)

    @property
    def last_outbound_raw(self):
        return self._only().last_outbound_raw

    @property
    def session_active(self) -> bool:
        runtime = self.default_runtime
        return bool(runtime and runtime.session_active)

    async def wait_for_session_end(self, timeout: float | None = None) -> None:
        runtime = self.default_runtime
        if runtime is not None:
            await runtime.wait_for_session_end(timeout)
            return
        await asyncio.gather(*(rt.wait_for_session_end(timeout)
                               for rt in self.runtimes.values()))

    def run_app_actions(self, app_actions) -> list:
        return self._only().run_app_actions(app_actions)

    def run_session_actions(self, actions) -> list:
        return self._only().run_session_actions(actions)

    def send_test_request(self) -> list:
        return self._only().send_test_request()

    def force_disconnect(self,
                         reason: str = "Disconnected via control API") -> None:
        self._only().force_disconnect(reason)

    def inject_seq_gap(self, skip: int) -> int:
        return self._only().inject_seq_gap(skip)

    def duplicate_last(self, poss_dup: bool = False) -> list:
        return self._only().duplicate_last(poss_dup)

    def recent_messages(self, limit: int = 50, direction: str = "all") -> list:
        return self._only().recent_messages(limit, direction)

    def archive_message_store(self, why: str) -> str | None:
        return self._only().archive_message_store(why)

    def _run_actions(self, actions, dispatch_only: bool = False,
                     collect: list | None = None) -> None:
        self._only()._run_actions(actions, dispatch_only=dispatch_only,
                                  collect=collect)

    # ------------------------------------------------------------ shutdown

    async def shutdown_gracefully(self, text: str) -> None:
        """Ctrl+C: log every live session out at once, then stop."""
        live = [runtime for runtime in self.runtimes.values()
                if runtime.session_active and runtime._busy]
        if live:
            self.engine_log.info(
                f"Initiating logout on {len(live)} session(s): "
                f"{', '.join(rt.id for rt in live)}"
            )
            for runtime in live:
                runtime.run_session_actions(
                    runtime.session.initiate_logout(text)
                )
            await asyncio.gather(*(
                runtime.wait_for_session_end(
                    runtime.config.logout_timeout_sec
                ) for runtime in live
            ))
        await self.stop()
