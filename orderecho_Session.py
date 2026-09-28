"""Pure FIX 4.2 session core.

No sockets, no real clock, no randomness.  Inputs in -> Actions out.  The
transport is responsible for turning Actions into bytes and for calling back
in with what arrives.

The session owns the sequence numbers and persists every change through the
injected seq_store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from orderecho_Codec import format_time
from orderecho_FixVersion import profile_for

# --------------------------------------------------------------------- tags

TAG_BEGIN_STRING = 8
TAG_BEGIN_SEQ_NO = 7
TAG_BODY_LENGTH = 9
TAG_MSG_SEQ_NUM = 34
TAG_MSG_TYPE = 35
TAG_NEW_SEQ_NO = 36
TAG_POSS_DUP_FLAG = 43
TAG_REF_SEQ_NUM = 45
TAG_SENDER_COMP_ID = 49
TAG_SENDING_TIME = 52
TAG_TARGET_COMP_ID = 56
TAG_TEXT = 58
TAG_END_SEQ_NO = 16
TAG_ENCRYPT_METHOD = 98
TAG_HEART_BT_INT = 108
TAG_TEST_REQ_ID = 112
TAG_GAP_FILL_FLAG = 123
TAG_RESET_SEQ_NUM_FLAG = 141
TAG_ORIG_SENDING_TIME = 122
TAG_REF_TAG_ID = 371
TAG_REF_MSG_TYPE = 372
TAG_SESSION_REJECT_REASON = 373
TAG_BUSINESS_REJECT_REASON = 380

# Session-level message types we handle ourselves; anything else is an
# application message.
MSG_HEARTBEAT = "0"
MSG_TEST_REQUEST = "1"
MSG_RESEND_REQUEST = "2"
MSG_REJECT = "3"
MSG_SEQUENCE_RESET = "4"
MSG_LOGOUT = "5"
MSG_LOGON = "A"
MSG_BUSINESS_REJECT = "j"

SESSION_MSG_TYPES = frozenset(
    {
        MSG_HEARTBEAT,
        MSG_TEST_REQUEST,
        MSG_RESEND_REQUEST,
        MSG_REJECT,
        MSG_SEQUENCE_RESET,
        MSG_LOGOUT,
        MSG_LOGON,
    }
)

REQUIRED_HEADER_TAGS = (TAG_MSG_TYPE, TAG_MSG_SEQ_NUM, TAG_SENDER_COMP_ID,
                        TAG_TARGET_COMP_ID, TAG_SENDING_TIME)

# Application messages the order book handles when an app is attached.
APP_MSG_TYPES = frozenset({"D", "F", "G"})

# SessionRejectReason (373) values used here.
REJECT_REQUIRED_TAG_MISSING = "1"
REJECT_VALUE_INCORRECT = "5"
REJECT_INCORRECT_DATA_FORMAT = "6"
REJECT_COMPID_PROBLEM = "9"

# BusinessRejectReason (380).
BUSINESS_REJECT_UNSUPPORTED_MSG_TYPE = "3"


# ------------------------------------------------------------------ actions


@dataclass
class Send:
    """Emit a FIX message.

    ``seq`` is filled in by the session with the MsgSeqNum the transport must
    put in tag 34.  ``seq_override`` is only set by gap fills, which reuse a
    number without consuming one.
    """

    msg_type: str
    body_fields: list = field(default_factory=list)
    poss_dup: bool = False
    orig_sending_time: str | None = None
    seq_override: int | None = None
    seq: int | None = None


@dataclass
class Disconnect:
    reason: str


@dataclass
class Evidence:
    """A non-message event worth recording (gaps, ignored dups, ...).

    ``order`` carries an order snapshot for application events; it is None for
    everything session-level.
    """

    event: str
    detail: str = ""
    order: dict | None = None


# --------------------------------------------------------------- app actions
#
# What an application (the order book) may ask the session to do.  The session
# owns sequence numbers and headers, so the app never sees either.


@dataclass
class AppSend:
    """Send an application message; the session wraps it in a Send."""

    msg_type: str
    body_fields: list = field(default_factory=list)


@dataclass
class SessionReject:
    """Ask for a session-level Reject (35=3) about an application message."""

    ref_seq: int | None
    ref_tag: int | None
    reason_code: str | None
    text: str | None


@dataclass
class RequestPrice:
    """Ask the transport to fetch a quote and bring it back via on_price."""

    order_id: str
    symbol: str


@dataclass
class Replay:
    """Resend a stored outbound message on its original sequence number.

    The transport rebuilds the bytes: same fields, 43=Y, 122 set to the
    original SendingTime and 52 to now, with 9 and 10 recomputed.
    """

    seq: int
    msg_type: str
    raw: str
    orig_sending_time: str | None = None
    injected: bool = False


# ------------------------------------------------------------------- states


class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    AWAITING_LOGON = "AWAITING_LOGON"
    ACTIVE = "ACTIVE"
    LOGOUT_SENT = "LOGOUT_SENT"


def _field_from_raw(raw: str, tag: int):
    """Pull one field out of a stored raw message without a full decode."""
    if raw is None:
        return None
    needle = f"\x01{tag}="
    index = raw.find(needle)
    if index == -1:
        prefix = f"{tag}="
        if raw.startswith(prefix):
            index, needle = -1, prefix
            start = len(prefix)
        else:
            return None
    else:
        start = index + len(needle)
    finish = raw.find("\x01", start)
    return raw[start:finish] if finish != -1 else raw[start:]


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Session:
    """The pure session core."""

    def __init__(self, config, seq_store, clock, app=None,
                 message_store=None) -> None:
        # Accept either the whole Config or just its session section.
        self.config = getattr(config, "session", config)
        self.seq_store = seq_store
        self.clock = clock
        self.app = app
        self.message_store = message_store
        self.resend_mode = getattr(self.config, "resend_mode", "replay")
        self.profile = profile_for(
            getattr(self.config, "fix_version", "FIX.4.2")
        )
        #: When several sessions share our CompIDs on one port, a Logon on the
        #: wrong dialect is told every version it could have used.
        self.expected_versions: list | None = None

        self.sender_comp_id = self.config.sender_comp_id
        self.target_comp_id = self.config.target_comp_id
        self.heartbeat_grace_pct = float(self.config.heartbeat_grace_pct)
        self.logout_timeout_sec = float(self.config.logout_timeout_sec)

        next_out, next_in = seq_store.load()
        self.next_out = int(next_out)
        self.expected_in = int(next_in)

        self.state = State.DISCONNECTED
        self.heart_bt_int: int | None = None

        self.last_sent = clock.now()
        self.last_received = clock.now()

        self.test_req_counter = 0
        self.pending_test_req_id: str | None = None
        self.pending_test_req_at = None

        self.resend_outstanding = False
        self.resend_gap_high: int | None = None

        self.logout_sent_at = None

    # ------------------------------------------------------------- helpers

    @property
    def name(self) -> str:
        return f"{self.sender_comp_id}-{self.target_comp_id}"

    def _persist(self) -> None:
        self.seq_store.save(self.next_out, self.expected_in)

    def _send(self, msg_type, body_fields=None, poss_dup=False,
              orig_sending_time=None, seq_override=None) -> Send:
        if seq_override is None:
            seq = self.next_out
            self.next_out += 1
            self._persist()
        else:
            seq = int(seq_override)
        self.last_sent = self.clock.now()
        return Send(
            msg_type=msg_type,
            body_fields=list(body_fields or []),
            poss_dup=poss_dup,
            orig_sending_time=orig_sending_time,
            seq_override=seq_override,
            seq=seq,
        )

    def _logout_and_disconnect(self, text: str) -> list:
        actions = [self._send(MSG_LOGOUT, [(TAG_TEXT, text)])]
        self.state = State.LOGOUT_SENT
        self.logout_sent_at = self.clock.now()
        actions.append(Disconnect(text))
        return actions

    def _reject(self, ref_seq, reason_code=None, text=None) -> Send:
        body = []
        if ref_seq is not None:
            body.append((TAG_REF_SEQ_NUM, str(ref_seq)))
        if reason_code is not None:
            body.append((TAG_SESSION_REJECT_REASON, reason_code))
        if text is not None:
            body.append((TAG_TEXT, text))
        return self._send(MSG_REJECT, body)

    def _business_reject(self, ref_seq, msg_type, reason, text) -> Send:
        body = []
        if ref_seq is not None:
            body.append((TAG_REF_SEQ_NUM, str(ref_seq)))
        body.append((TAG_REF_MSG_TYPE, msg_type))
        body.append((TAG_BUSINESS_REJECT_REASON, reason))
        body.append((TAG_TEXT, text))
        return self._send(MSG_BUSINESS_REJECT, body)

    def _archive_message_store(self, why: str) -> list:
        """Sequence numbers are about to mean different messages."""
        if self.message_store is None:
            return []
        archived = self.message_store.archive()
        if archived is None:
            return []
        return [
            Evidence("store archived",
                     f"outbound message store archived to {archived} ({why})")
        ]

    def _clear_resend_if_covered(self) -> None:
        if not self.resend_outstanding:
            return
        if self.resend_gap_high is None or self.expected_in > self.resend_gap_high:
            self.resend_outstanding = False
            self.resend_gap_high = None

    # ----------------------------------------------------- connection events

    def on_connect(self) -> list:
        self.state = State.AWAITING_LOGON
        now = self.clock.now()
        self.last_sent = now
        self.last_received = now
        self.pending_test_req_id = None
        self.pending_test_req_at = None
        self.resend_outstanding = False
        self.resend_gap_high = None
        self.logout_sent_at = None
        self.deferral_recorded = False
        return [Evidence("connected", "awaiting Logon")]

    def on_disconnect(self) -> list:
        self.state = State.DISCONNECTED
        self.heart_bt_int = None
        self.pending_test_req_id = None
        self.pending_test_req_at = None
        self.logout_sent_at = None
        self._persist()
        actions = [Evidence("disconnected",
                            f"next_out={self.next_out} next_in={self.expected_in}")]
        actions.extend(self._note_deferred_events())
        return actions

    def _note_deferred_events(self) -> list:
        """Say once per disconnect that scheduled order events are waiting.

        They are not lost: on_timer() simply does not run while we are down,
        so anything due fires on the first tick after the next Logon.
        """
        if self.app is None or self.deferral_recorded:
            return []
        pending = getattr(self.app, "has_pending_events", None)
        if not callable(pending) or not pending():
            return []
        self.deferral_recorded = True
        return [
            Evidence(
                "scheduled order events deferred until logon",
                "order events remain due and will fire after the next Logon",
            )
        ]

    def on_discarded(self, frame) -> list:
        """A frame that failed BodyLength/CheckSum: discard, never reject."""
        return [Evidence("frame discarded", getattr(frame, "reason", "unknown"))]

    # ------------------------------------------------------------- inbound

    def on_message(self, msg, market_price=None) -> list:
        self.last_received = self.clock.now()

        # A counterparty on the wrong FIX version gets a Logout and the door,
        # never a session Reject -- there is no shared dialect to reject in.
        begin_string = msg.get(TAG_BEGIN_STRING)
        if begin_string is not None and \
                begin_string != self.profile.begin_string:
            expected = self.profile.begin_string
            if self.expected_versions and len(self.expected_versions) > 1:
                expected = " or ".join(self.expected_versions)
            actions = [
                Evidence(
                    "begin string mismatch",
                    f"received {begin_string}, expected {expected}",
                )
            ]
            actions.extend(self._logout_and_disconnect(
                f"Incorrect BeginString, expected {expected}"
            ))
            return actions

        if self.state is State.DISCONNECTED:
            return [Evidence("message while disconnected",
                             f"msg_type={msg.msg_type}")]
        if self.state is State.AWAITING_LOGON:
            return self._on_logon_expected(msg)
        return self._on_active(msg, market_price)

    # ---------------------------------------------------------- logon phase

    def _on_logon_expected(self, msg) -> list:
        if msg.msg_type != MSG_LOGON:
            return [
                Evidence("first message not Logon", f"msg_type={msg.msg_type}"),
                Disconnect("First message not Logon"),
            ]

        sender = msg.get(TAG_SENDER_COMP_ID)
        target = msg.get(TAG_TARGET_COMP_ID)
        if sender != self.target_comp_id or target != self.sender_comp_id:
            return self._logout_and_disconnect(
                f"CompID problem: expecting 49={self.target_comp_id} "
                f"56={self.sender_comp_id}, received 49={sender} 56={target}"
            )

        if msg.get(TAG_ENCRYPT_METHOD) != "0":
            return self._logout_and_disconnect(
                f"EncryptMethod must be 0, received {msg.get(TAG_ENCRYPT_METHOD)}"
            )

        heart_bt_int = _as_int(msg.get(TAG_HEART_BT_INT))
        if heart_bt_int is None or heart_bt_int <= 0:
            return self._logout_and_disconnect(
                f"HeartBtInt must be a positive integer, "
                f"received {msg.get(TAG_HEART_BT_INT)}"
            )

        seq = msg.seq_num
        if seq is None:
            return self._logout_and_disconnect(
                "Required tag missing: MsgSeqNum (34)"
            )

        actions: list = []
        reset_requested = msg.get(TAG_RESET_SEQ_NUM_FLAG) == "Y"
        if reset_requested:
            self.expected_in = 1
            self.next_out = 1
            self._persist()
            actions.append(Evidence("seqnums reset", "Logon carried 141=Y"))
            actions.extend(self._archive_message_store("Logon carried 141=Y"))

        expected = self.expected_in
        gap = False
        if seq == expected:
            self.expected_in = seq + 1
            self._persist()
        elif seq > expected:
            gap = True
            actions.append(
                Evidence(
                    "seq gap detected",
                    f"Logon MsgSeqNum {seq}, expecting {expected}",
                )
            )
        else:
            return actions + self._logout_and_disconnect(
                f"MsgSeqNum too low, expecting {expected} but received {seq}"
            )

        logon_body = [(TAG_ENCRYPT_METHOD, "0"),
                      (TAG_HEART_BT_INT, str(heart_bt_int))]
        if reset_requested:
            logon_body.append((TAG_RESET_SEQ_NUM_FLAG, "Y"))
        actions.append(self._send(MSG_LOGON, logon_body))

        self.heart_bt_int = heart_bt_int
        self.state = State.ACTIVE
        actions.append(
            Evidence(
                "logon accepted",
                f"HeartBtInt={heart_bt_int}, next_in={self.expected_in} "
                f"next_out={self.next_out}",
            )
        )

        if gap:
            actions.extend(self._request_resend(expected, seq))
        return actions

    def _request_resend(self, begin_seq: int, gap_high: int | None) -> list:
        if self.resend_outstanding:
            return []
        self.resend_outstanding = True
        self.resend_gap_high = gap_high
        return [
            self._send(
                MSG_RESEND_REQUEST,
                [(TAG_BEGIN_SEQ_NO, str(begin_seq)), (TAG_END_SEQ_NO, "0")],
            )
        ]

    # --------------------------------------------------------- active phase

    def _on_active(self, msg, market_price=None) -> list:
        msg_type = msg.msg_type
        seq = msg.seq_num

        # MsgSeqNum missing (or unusable): we cannot sequence the session.
        if seq is None:
            return self._logout_and_disconnect(
                "Required tag missing: MsgSeqNum (34)"
            )

        # CompIDs present but wrong -> Reject, then Logout + Disconnect.
        sender = msg.get(TAG_SENDER_COMP_ID)
        target = msg.get(TAG_TARGET_COMP_ID)
        if (sender is not None and sender != self.target_comp_id) or (
            target is not None and target != self.sender_comp_id
        ):
            actions = [
                self._reject(
                    seq,
                    REJECT_COMPID_PROBLEM,
                    f"CompID problem: expecting 49={self.target_comp_id} "
                    f"56={self.sender_comp_id}, received 49={sender} 56={target}",
                )
            ]
            actions.extend(self._logout_and_disconnect("CompID problem"))
            return actions

        # SequenceReset in Reset mode skips the sequence check entirely.
        if msg_type == MSG_SEQUENCE_RESET and msg.get(TAG_GAP_FILL_FLAG) != "Y":
            return self._sequence_reset(msg, seq, gap_fill=False)

        seq_actions = self._sequence_check(msg, seq)
        if seq_actions is not None:
            return seq_actions

        # In sequence from here on: the message has been consumed.
        missing = [
            tag for tag in REQUIRED_HEADER_TAGS if msg.get(tag) is None
        ]
        if missing:
            names = ", ".join(str(tag) for tag in missing)
            return [
                self._reject(
                    seq,
                    REJECT_REQUIRED_TAG_MISSING,
                    f"Required tag missing: {names}",
                )
            ]

        return self._dispatch(msg, msg_type, seq, market_price)

    def _sequence_check(self, msg, seq: int):
        """Return a list of actions if the message must not be processed."""
        expected = self.expected_in
        if seq == expected:
            self.expected_in = seq + 1
            self._persist()
            self._clear_resend_if_covered()
            return None

        if seq > expected:
            actions = [
                Evidence(
                    "seq gap detected",
                    f"received MsgSeqNum {seq}, expecting {expected}",
                )
            ]
            if self.resend_outstanding:
                actions.append(
                    Evidence(
                        "resend already outstanding",
                        f"no second ResendRequest for MsgSeqNum {seq}",
                    )
                )
                return actions
            actions.extend(self._request_resend(expected, seq))
            return actions

        if msg.get(TAG_POSS_DUP_FLAG) == "Y":
            return [
                Evidence(
                    "possdup ignored",
                    f"MsgSeqNum {seq} below expected {expected}, 43=Y",
                )
            ]

        return self._logout_and_disconnect(
            f"MsgSeqNum too low, expecting {expected} but received {seq}"
        )

    def _dispatch(self, msg, msg_type: str, seq: int, market_price=None) -> list:
        if msg_type == MSG_HEARTBEAT:
            test_req_id = msg.get(TAG_TEST_REQ_ID)
            if test_req_id is not None and test_req_id == self.pending_test_req_id:
                self.pending_test_req_id = None
                self.pending_test_req_at = None
                return [Evidence("testrequest answered", f"112={test_req_id}")]
            return []

        if msg_type == MSG_TEST_REQUEST:
            test_req_id = msg.get(TAG_TEST_REQ_ID)
            if test_req_id is None:
                return [
                    self._reject(
                        seq,
                        REJECT_REQUIRED_TAG_MISSING,
                        "Required tag missing: TestReqID (112)",
                    )
                ]
            return [self._send(MSG_HEARTBEAT, [(TAG_TEST_REQ_ID, test_req_id)])]

        if msg_type == MSG_RESEND_REQUEST:
            return self._on_resend_request(msg, seq)

        if msg_type == MSG_REJECT:
            return [
                Evidence(
                    "reject received",
                    f"45={msg.get(TAG_REF_SEQ_NUM)} 373={msg.get(373)} "
                    f"58={msg.get(TAG_TEXT)}",
                )
            ]

        if msg_type == MSG_SEQUENCE_RESET:
            return self._sequence_reset(msg, seq, gap_fill=True)

        if msg_type == MSG_LOGOUT:
            if self.state is State.LOGOUT_SENT:
                return [
                    Evidence("logout confirmed", msg.get(TAG_TEXT) or ""),
                    Disconnect("Logout confirmed"),
                ]
            # A logout we did not initiate: reply and go straight down.
            # LOGOUT_SENT is only ever for logouts we started ourselves.
            actions = [
                self._send(MSG_LOGOUT, [(TAG_TEXT, "Logout acknowledged")])
            ]
            self.state = State.DISCONNECTED
            self.logout_sent_at = None
            actions.append(Disconnect("Logout requested by counterparty"))
            return actions

        if msg_type == MSG_LOGON:
            return [
                self._reject(seq, None, "Logon received while already logged on")
            ]

        if self.app is not None and msg_type in APP_MSG_TYPES:
            return self._run_app(self.app.on_app_message(msg, market_price))

        # Anything else is an application message; unsupported in this build.
        return [
            self._business_reject(
                seq,
                msg_type or "",
                BUSINESS_REJECT_UNSUPPORTED_MSG_TYPE,
                "Not supported in this build",
            )
        ]

    def on_price(self, order_id: str, quote) -> list:
        """A background price lookup finished (spec 4)."""
        if self.app is None:
            return []
        on_price = getattr(self.app, "on_price", None)
        if not callable(on_price):
            return []
        # Record the price whatever state we are in, but only let it set off
        # fills while the session is live.
        return self._run_app(
            on_price(order_id, quote, fire_due=self.state is State.ACTIVE)
        )

    def _run_app(self, app_actions) -> list:
        """Turn AppActions into session Actions."""
        actions: list = []
        for action in app_actions or ():
            if isinstance(action, AppSend):
                actions.append(self._send(action.msg_type, action.body_fields))
            elif isinstance(action, SessionReject):
                actions.append(
                    self._app_reject(action.ref_seq, action.ref_tag,
                                     action.reason_code, action.text)
                )
            else:
                actions.append(action)
        return actions

    def _app_reject(self, ref_seq, ref_tag, reason_code, text) -> Send:
        body = []
        if ref_seq is not None:
            body.append((TAG_REF_SEQ_NUM, str(ref_seq)))
        if ref_tag is not None:
            body.append((TAG_REF_TAG_ID, str(ref_tag)))
        if reason_code is not None:
            body.append((TAG_SESSION_REJECT_REASON, reason_code))
        if text is not None:
            body.append((TAG_TEXT, text))
        return self._send(MSG_REJECT, body)

    def _on_resend_request(self, msg, seq: int) -> list:
        begin = _as_int(msg.get(TAG_BEGIN_SEQ_NO))
        if begin is None:
            return [
                self._reject(
                    seq,
                    REJECT_REQUIRED_TAG_MISSING,
                    "Required tag missing: BeginSeqNo (7)",
                )
            ]
        if begin < 1:
            return [
                self._reject(
                    seq, REJECT_VALUE_INCORRECT, "BeginSeqNo must be >= 1"
                )
            ]
        end = _as_int(msg.get(TAG_END_SEQ_NO))
        if end is None:
            return [
                self._reject(
                    seq,
                    REJECT_REQUIRED_TAG_MISSING,
                    "Required tag missing: EndSeqNo (16)",
                )
            ]

        last_sent = self.next_out - 1
        end_seq = last_sent if end == 0 else min(end, last_sent)
        if begin > end_seq:
            return [
                Evidence(
                    "resend request for future seqnums ignored",
                    f"7={begin} is at or beyond next_out={self.next_out}",
                )
            ]

        if self.resend_mode == "gapfill" or self.message_store is None:
            return self._gapfill_everything(begin, end, end_seq)
        return self._replay(begin, end, end_seq)

    def _gapfill_everything(self, begin: int, end: int, end_seq: int) -> list:
        """The Cook 3 answer: one SequenceReset-GapFill over the whole range."""
        new_seq_no = end_seq + 1
        gap_fill = self._make_gap_fill(begin, new_seq_no)
        return [
            Evidence(
                "resend request received",
                f"7={begin} 16={end}; gap filling to {new_seq_no}",
            ),
            gap_fill,
        ]

    def _make_gap_fill(self, begin_seq: int, new_seq_no: int) -> Send:
        return self._send(
            MSG_SEQUENCE_RESET,
            [(TAG_GAP_FILL_FLAG, "Y"), (TAG_NEW_SEQ_NO, str(new_seq_no))],
            poss_dup=True,
            orig_sending_time=format_time(self.clock.now()),
            seq_override=begin_seq,
        )

    def _replay(self, begin: int, end: int, end_seq: int) -> list:
        """Resend the real application messages; gap-fill everything else.

        Admin messages and sequence numbers we no longer hold are worthless to
        the counterparty, so consecutive runs of them collapse into a single
        SequenceReset-GapFill.
        """
        actions: list = [
            Evidence(
                "resend request received",
                f"7={begin} 16={end}; replaying {begin}..{end_seq}",
            )
        ]
        replayed = 0
        gap_runs = 0
        current = begin

        while current <= end_seq:
            if self.message_store.is_admin(current):
                run_start = current
                while current <= end_seq and self.message_store.is_admin(current):
                    current += 1
                actions.append(self._make_gap_fill(run_start, current))
                gap_runs += 1
                continue

            record = self.message_store.get(current)
            raw = record["raw"]
            actions.append(
                Replay(
                    seq=current,
                    msg_type=record.get("msg_type"),
                    raw=raw,
                    orig_sending_time=_field_from_raw(raw, TAG_SENDING_TIME),
                    injected=bool(record.get("injected")),
                )
            )
            replayed += 1
            current += 1

        actions.append(
            Evidence(
                "resend summary",
                f"{'RESEND':<5} {begin}..{end_seq} -> replayed {replayed}, "
                f"gap-filled {gap_runs} run(s)",
            )
        )
        return actions

    def _sequence_reset(self, msg, seq: int, gap_fill: bool) -> list:
        new_seq = _as_int(msg.get(TAG_NEW_SEQ_NO))
        if msg.get(TAG_NEW_SEQ_NO) is None:
            return [
                self._reject(
                    seq,
                    REJECT_REQUIRED_TAG_MISSING,
                    "Required tag missing: NewSeqNo (36)",
                )
            ]
        if new_seq is None:
            return [
                self._reject(
                    seq,
                    REJECT_INCORRECT_DATA_FORMAT,
                    "NewSeqNo (36) is not a number",
                )
            ]

        if new_seq < self.expected_in:
            return [
                self._reject(seq, REJECT_VALUE_INCORRECT, "NewSeqNo too low")
            ]

        mode = "gap fill" if gap_fill else "reset"
        previous = self.expected_in
        self.expected_in = new_seq
        self._persist()
        self._clear_resend_if_covered()
        return [
            Evidence(
                "sequence reset applied",
                f"{mode}: next_in {previous} -> {new_seq}",
            )
        ]

    # --------------------------------------------------------------- timers

    def on_timer(self) -> list:
        if self.state not in (State.ACTIVE, State.LOGOUT_SENT):
            return []

        now = self.clock.now()
        actions: list = []

        if self.state is State.LOGOUT_SENT and self.logout_sent_at is not None:
            if (now - self.logout_sent_at).total_seconds() >= self.logout_timeout_sec:
                return [Disconnect("Logout timeout")]

        if self.heart_bt_int is None:
            return actions

        if self.pending_test_req_id is not None and self.pending_test_req_at is not None:
            if (now - self.pending_test_req_at).total_seconds() >= self.heart_bt_int:
                return [Disconnect("TestRequest timeout")]

        if (now - self.last_sent).total_seconds() >= self.heart_bt_int:
            actions.append(self._send(MSG_HEARTBEAT))

        grace = self.heart_bt_int * (1 + self.heartbeat_grace_pct / 100.0)
        if (now - self.last_received).total_seconds() >= grace and \
                self.pending_test_req_id is None:
            self.test_req_counter += 1
            test_req_id = f"TEST-{self.test_req_counter}"
            self.pending_test_req_id = test_req_id
            self.pending_test_req_at = now
            actions.append(
                self._send(MSG_TEST_REQUEST, [(TAG_TEST_REQ_ID, test_req_id)])
            )

        # Scheduled order events only run on a live session.
        if self.app is not None and self.state is State.ACTIVE:
            actions.extend(self._run_app(self.app.on_timer()))

        return actions

    # ------------------------------------------------------------- outbound

    def initiate_logout(self, text: str) -> list:
        actions = [self._send(MSG_LOGOUT, [(TAG_TEXT, text)])]
        self.state = State.LOGOUT_SENT
        self.logout_sent_at = self.clock.now()
        return actions
