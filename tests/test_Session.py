"""Session core: pure, FakeClock + MemorySeqStore, no sockets."""

from datetime import datetime, timezone

import pytest

from orderecho_Clock import FakeClock
from orderecho_Codec import Codec
from orderecho_Config import SessionConfig
from orderecho_SeqStore import MemorySeqStore
from orderecho_Session import (
    Disconnect,
    Evidence,
    Send,
    Session,
    State,
)

START = datetime(2026, 9, 26, 14, 0, 0, tzinfo=timezone.utc)
HEARTBEAT = 30


def make_config(**overrides):
    values = dict(
        fix_version="FIX.4.2",
        sender_comp_id="ORDERECHO",
        target_comp_id="AGENT",
        host="127.0.0.1",
        port=9878,
        heartbeat_grace_pct=20,
        logout_timeout_sec=10,
    )
    values.update(overrides)
    return SessionConfig(**values)


def make_session(next_out=1, next_in=1, **config_overrides):
    clock = FakeClock(START)
    store = MemorySeqStore(next_out, next_in)
    session = Session(make_config(**config_overrides), store, clock)
    return session, clock, store


def inbound(msg_type, body=None, seq=1, sender="AGENT", target="ORDERECHO",
            poss_dup=False, orig_sending_time=None, when=START, omit=()):
    """Build a decoded inbound message the way the transport would."""
    codec = Codec("FIX.4.2")
    raw = codec.encode(
        msg_type,
        body or [],
        sender_comp_id=sender,
        target_comp_id=target,
        seq_num=seq,
        sending_time=when,
        poss_dup=poss_dup,
        orig_sending_time=orig_sending_time,
    )
    msg = codec.decode(raw)[0]
    if omit:
        msg.pairs = [(tag, value) for tag, value in msg.pairs if tag not in omit]
    return msg


def logon_msg(seq=1, heart_bt_int=HEARTBEAT, reset=False, **kwargs):
    body = [(98, "0"), (108, str(heart_bt_int))]
    if reset:
        body.append((141, "Y"))
    return inbound("A", body, seq=seq, **kwargs)


def sends(actions):
    return [a for a in actions if isinstance(a, Send)]


def disconnects(actions):
    return [a for a in actions if isinstance(a, Disconnect)]


def evidences(actions):
    return [a for a in actions if isinstance(a, Evidence)]


def field(send, tag):
    for pair_tag, value in send.body_fields:
        if int(pair_tag) == tag:
            return value
    return None


def do_logon(session, seq=1, **kwargs):
    session.on_connect()
    return session.on_message(logon_msg(seq=seq, **kwargs))


# 1 -------------------------------------------------------------------------

def test_valid_logon_replies_and_activates():
    session, _clock, store = make_session()
    actions = do_logon(session)
    out = sends(actions)
    assert len(out) == 1
    assert out[0].msg_type == "A"
    assert out[0].seq == 1
    assert field(out[0], 98) == "0"
    assert field(out[0], 108) == str(HEARTBEAT)
    assert field(out[0], 141) is None
    assert session.state is State.ACTIVE
    assert session.expected_in == 2
    assert session.next_out == 2
    assert store.load() == (2, 2)
    assert not disconnects(actions)


# 2 -------------------------------------------------------------------------

def test_first_message_not_logon_disconnects_without_reply():
    session, _clock, _store = make_session()
    session.on_connect()
    actions = session.on_message(inbound("0", seq=1))
    assert sends(actions) == []
    assert [d.reason for d in disconnects(actions)] == ["First message not Logon"]


# 3 -------------------------------------------------------------------------

def test_wrong_compid_on_logon_logs_out_and_disconnects():
    session, _clock, _store = make_session()
    session.on_connect()
    actions = session.on_message(logon_msg(sender="SOMEONE"))
    out = sends(actions)
    assert len(out) == 1 and out[0].msg_type == "5"
    assert "CompID" in field(out[0], 58)
    assert disconnects(actions)
    assert session.state is not State.ACTIVE


def test_bad_encrypt_method_logs_out():
    session, _clock, _store = make_session()
    session.on_connect()
    msg = inbound("A", [(98, "1"), (108, "30")], seq=1)
    actions = session.on_message(msg)
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert "EncryptMethod" in field(out[0], 58)
    assert disconnects(actions)


def test_bad_heartbtint_logs_out():
    session, _clock, _store = make_session()
    session.on_connect()
    actions = session.on_message(inbound("A", [(98, "0"), (108, "0")], seq=1))
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert "HeartBtInt" in field(out[0], 58)
    assert disconnects(actions)


# 4 -------------------------------------------------------------------------

def test_logon_with_reset_flag_resets_both_counters():
    session, _clock, store = make_session(next_out=17, next_in=42)
    actions = do_logon(session, seq=1, reset=True)
    out = sends(actions)
    assert out[0].msg_type == "A"
    assert out[0].seq == 1
    assert field(out[0], 141) == "Y"
    assert session.expected_in == 2
    assert session.next_out == 2
    assert store.load() == (2, 2)


# 5 -------------------------------------------------------------------------

def test_logon_seq_too_low_logs_out_with_text():
    session, _clock, _store = make_session(next_out=5, next_in=10)
    session.on_connect()
    actions = session.on_message(logon_msg(seq=4))
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert field(out[0], 58) == "MsgSeqNum too low, expecting 10 but received 4"
    assert disconnects(actions)


# 6 -------------------------------------------------------------------------

def test_logon_seq_too_high_replies_then_requests_resend():
    session, _clock, _store = make_session(next_out=1, next_in=5)
    session.on_connect()
    actions = session.on_message(logon_msg(seq=9))
    out = sends(actions)
    assert [s.msg_type for s in out] == ["A", "2"]
    assert field(out[1], 7) == "5"
    assert field(out[1], 16) == "0"
    assert session.state is State.ACTIVE
    assert session.expected_in == 5          # gap not yet filled
    assert session.resend_outstanding


# 7 -------------------------------------------------------------------------

def test_test_request_is_answered_with_heartbeat():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("1", [(112, "ABC-9")], seq=2))
    out = sends(actions)
    assert len(out) == 1
    assert out[0].msg_type == "0"
    assert field(out[0], 112) == "ABC-9"
    assert out[0].seq == 2


# 8 -------------------------------------------------------------------------

def test_silence_past_heartbtint_sends_heartbeat():
    session, clock, _store = make_session()
    do_logon(session)
    clock.advance(HEARTBEAT - 1)
    assert sends(session.on_timer()) == []
    clock.advance(1)
    out = sends(session.on_timer())
    assert [s.msg_type for s in out] == ["0"]
    assert field(out[0], 112) is None


# 9 -------------------------------------------------------------------------

def test_inbound_silence_sends_test_request_then_disconnects():
    session, clock, _store = make_session()
    do_logon(session)
    clock.advance(HEARTBEAT * 1.2)
    actions = session.on_timer()
    test_requests = [s for s in sends(actions) if s.msg_type == "1"]
    assert len(test_requests) == 1
    assert field(test_requests[0], 112) == "TEST-1"
    assert session.pending_test_req_id == "TEST-1"

    clock.advance(HEARTBEAT - 1)
    assert disconnects(session.on_timer()) == []
    clock.advance(1)
    assert [d.reason for d in disconnects(session.on_timer())] == \
        ["TestRequest timeout"]


def test_heartbeat_answering_test_request_clears_it():
    session, clock, _store = make_session()
    do_logon(session)
    clock.advance(HEARTBEAT * 1.2)
    session.on_timer()
    assert session.pending_test_req_id == "TEST-1"
    session.on_message(inbound("0", [(112, "TEST-1")], seq=2))
    assert session.pending_test_req_id is None


# 10 ------------------------------------------------------------------------

def test_gap_triggers_one_resend_request_only():
    session, _clock, _store = make_session()
    do_logon(session)
    first = session.on_message(inbound("0", seq=5))
    out = sends(first)
    assert [s.msg_type for s in out] == ["2"]
    assert field(out[0], 7) == "2"
    assert field(out[0], 16) == "0"

    second = session.on_message(inbound("0", seq=6))
    assert sends(second) == []
    assert session.expected_in == 2


# 11 ------------------------------------------------------------------------

def test_seq_too_low_without_possdup_logs_out():
    session, _clock, _store = make_session()
    do_logon(session)
    session.on_message(inbound("0", seq=2))
    actions = session.on_message(inbound("0", seq=2))
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert field(out[0], 58) == "MsgSeqNum too low, expecting 3 but received 2"
    assert disconnects(actions)


def test_seq_too_low_with_possdup_is_ignored():
    session, _clock, _store = make_session()
    do_logon(session)
    session.on_message(inbound("0", seq=2))
    actions = session.on_message(inbound("0", seq=2, poss_dup=True,
                                         orig_sending_time=START))
    assert sends(actions) == []
    assert disconnects(actions) == []
    assert [e.event for e in evidences(actions)] == ["possdup ignored"]
    assert session.expected_in == 3


# 12 ------------------------------------------------------------------------

def test_resend_request_is_answered_with_one_gap_fill():
    session, clock, _store = make_session()
    do_logon(session)
    next_out_before = session.next_out
    actions = session.on_message(inbound("2", [(7, "1"), (16, "0")], seq=2))
    out = sends(actions)
    assert len(out) == 1
    gap_fill = out[0]
    assert gap_fill.msg_type == "4"
    assert gap_fill.seq_override == 1
    assert gap_fill.seq == 1
    assert gap_fill.poss_dup is True
    assert gap_fill.orig_sending_time == "20260926-14:00:00.000"
    assert field(gap_fill, 123) == "Y"
    assert field(gap_fill, 36) == str(next_out_before)
    assert session.next_out == next_out_before   # counter untouched


# 13 ------------------------------------------------------------------------

def test_sequence_reset_gap_fill_advances_expected():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(
        inbound("4", [(123, "Y"), (36, "9")], seq=2)
    )
    assert sends(actions) == []
    assert session.expected_in == 9


def test_sequence_reset_in_reset_mode_ignores_seq_num():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("4", [(36, "20")], seq=999))
    assert sends(actions) == []
    assert session.expected_in == 20
    # and a wildly low 34 is equally ignored
    actions = session.on_message(inbound("4", [(36, "25")], seq=1))
    assert sends(actions) == []
    assert session.expected_in == 25


def test_sequence_reset_with_new_seq_no_too_low_is_rejected():
    session, _clock, _store = make_session()
    do_logon(session)
    session.on_message(inbound("4", [(36, "20")], seq=2))
    actions = session.on_message(inbound("4", [(36, "10")], seq=20))
    out = sends(actions)
    assert out[0].msg_type == "3"
    assert field(out[0], 373) == "5"
    assert field(out[0], 58) == "NewSeqNo too low"
    assert session.expected_in == 20


def test_gap_fill_clears_outstanding_resend_request():
    session, _clock, _store = make_session()
    do_logon(session)
    session.on_message(inbound("0", seq=5))
    assert session.resend_outstanding
    session.on_message(inbound("4", [(123, "Y"), (36, "6")], seq=2))
    assert session.expected_in == 6
    assert not session.resend_outstanding


# 14 ------------------------------------------------------------------------

def test_application_message_gets_business_reject():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(
        inbound("D", [(11, "ORD-1"), (55, "AAPL")], seq=2)
    )
    out = sends(actions)
    assert len(out) == 1
    assert out[0].msg_type == "j"
    assert field(out[0], 45) == "2"
    assert field(out[0], 372) == "D"
    assert field(out[0], 380) == "3"
    assert field(out[0], 58) == "Not supported in this build"
    assert session.expected_in == 3


def test_logon_while_active_is_session_rejected():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(logon_msg(seq=2))
    out = sends(actions)
    assert out[0].msg_type == "3"
    assert field(out[0], 45) == "2"
    assert field(out[0], 58) == "Logon received while already logged on"
    assert session.state is State.ACTIVE


def test_missing_header_field_is_session_rejected():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("0", seq=2, omit=(52,)))
    out = sends(actions)
    assert out[0].msg_type == "3"
    assert field(out[0], 45) == "2"
    assert field(out[0], 373) == "1"
    assert "52" in field(out[0], 58)


def test_missing_seq_num_logs_out():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("0", seq=2, omit=(34,)))
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert "MsgSeqNum" in field(out[0], 58)
    assert disconnects(actions)


def test_wrong_compid_in_active_rejects_then_logs_out():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("0", seq=2, sender="WRONG"))
    out = sends(actions)
    assert [s.msg_type for s in out] == ["3", "5"]
    assert field(out[0], 373) == "9"
    assert disconnects(actions)


def test_inbound_reject_is_logged_only():
    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_message(inbound("3", [(45, "1"), (58, "nope")], seq=2))
    assert sends(actions) == []
    assert [e.event for e in evidences(actions)] == ["reject received"]


# 15 ------------------------------------------------------------------------

def test_counterparty_logout_is_answered_then_disconnected():
    """A logout we did not initiate: ACTIVE -> DISCONNECTED, never LOGOUT_SENT."""
    session, _clock, _store = make_session()
    do_logon(session)
    assert session.state is State.ACTIVE

    actions = session.on_message(inbound("5", [(58, "bye")], seq=2))
    out = sends(actions)
    assert [s.msg_type for s in out] == ["5"]
    assert disconnects(actions)
    assert session.state is State.DISCONNECTED
    assert session.logout_sent_at is None


# 16 ------------------------------------------------------------------------

def test_initiated_logout_times_out_into_disconnect():
    session, clock, _store = make_session()
    do_logon(session)
    actions = session.initiate_logout("OrderEcho shutting down")
    out = sends(actions)
    assert out[0].msg_type == "5"
    assert field(out[0], 58) == "OrderEcho shutting down"
    assert session.state is State.LOGOUT_SENT

    clock.advance(9)
    assert disconnects(session.on_timer()) == []
    clock.advance(1)
    assert [d.reason for d in disconnects(session.on_timer())] == ["Logout timeout"]


def test_logout_reply_while_logout_sent_disconnects():
    session, _clock, _store = make_session()
    do_logon(session)
    session.initiate_logout("bye")
    actions = session.on_message(inbound("5", seq=2))
    assert sends(actions) == []
    assert [d.reason for d in disconnects(actions)] == ["Logout confirmed"]


# misc ----------------------------------------------------------------------

def test_discarded_frame_produces_evidence_only():
    from orderecho_Codec import DiscardedFrame

    session, _clock, _store = make_session()
    do_logon(session)
    actions = session.on_discarded(DiscardedFrame(b"junk", "Bad CheckSum (10)"))
    assert sends(actions) == []
    assert disconnects(actions) == []
    assert evidences(actions)[0].event == "frame discarded"


def test_disconnect_persists_sequence_numbers():
    session, _clock, store = make_session()
    do_logon(session)
    session.on_disconnect()
    assert session.state is State.DISCONNECTED
    assert store.load() == (2, 2)


def test_timer_is_idle_unless_active():
    session, clock, _store = make_session()
    assert session.on_timer() == []
    session.on_connect()
    clock.advance(1000)
    assert session.on_timer() == []


# ===========================================================================
# Cook 2 additions
# ===========================================================================

from orderecho_Config import OrdersConfig            # noqa: E402
from orderecho_OrderBook import OrderBook            # noqa: E402
from orderecho_Rules import load_rules               # noqa: E402
from orderecho_Session import AppSend, SessionReject  # noqa: E402


class StubApp:
    """Records what the session routes to it and replays canned actions."""

    def __init__(self, actions=None, pending=False):
        self.actions = actions or []
        self.pending = pending
        self.messages = []
        self.timer_calls = 0

    def on_app_message(self, msg, market_price=None):
        self.messages.append((msg.msg_type, market_price))
        return list(self.actions)

    def on_timer(self):
        self.timer_calls += 1
        return []

    def has_pending_events(self):
        return self.pending


def make_session_with_app(app, next_out=1, next_in=1, **config_overrides):
    clock = FakeClock(START)
    store = MemorySeqStore(next_out, next_in)
    session = Session(make_config(**config_overrides), store, clock, app=app)
    return session, clock, store


def app_order(cl_ord_id="C1", symbol="AAPL", seq=2):
    return inbound(
        "D",
        [(11, cl_ord_id), (21, "1"), (55, symbol), (54, "1"),
         (60, "20260926-14:00:00.000"), (38, "1000"), (40, "2"), (44, "10.00")],
        seq=seq,
    )


# --- §4 application hook ---------------------------------------------------

@pytest.mark.parametrize("msg_type", ["D", "F", "G"])
def test_app_message_types_are_routed_to_the_app(msg_type):
    app = StubApp()
    session, _clock, _store = make_session_with_app(app)
    session.on_connect()
    session.on_message(logon_msg(seq=1))

    session.on_message(inbound(msg_type, [(11, "C1")], seq=2))
    assert [seen[0] for seen in app.messages] == [msg_type]


def test_market_price_is_passed_through_to_the_app():
    app = StubApp()
    session, _clock, _store = make_session_with_app(app)
    do_logon_with(session)
    quote = object()
    session.on_message(app_order(), market_price=quote)
    assert app.messages == [("D", quote)]


def do_logon_with(session, seq=1):
    session.on_connect()
    return session.on_message(logon_msg(seq=seq))


def test_other_application_messages_still_get_business_reject():
    app = StubApp()
    session, _clock, _store = make_session_with_app(app)
    do_logon_with(session)

    actions = session.on_message(inbound("8", [(37, "X")], seq=2))
    out = sends(actions)
    assert out[0].msg_type == "j"
    assert field(out[0], 372) == "8"
    assert app.messages == []


def test_without_an_app_new_order_single_gets_business_reject():
    session, _clock, _store = make_session()      # app=None: Cook 1 behavior
    do_logon(session)
    actions = session.on_message(app_order())
    out = sends(actions)
    assert out[0].msg_type == "j"
    assert field(out[0], 45) == "2"
    assert field(out[0], 372) == "D"
    assert field(out[0], 380) == "3"


def test_app_send_is_wrapped_in_a_session_send():
    app = StubApp([AppSend("8", [(37, "O-1"), (150, "0")])])
    session, _clock, _store = make_session_with_app(app)
    do_logon_with(session)

    actions = session.on_message(app_order())
    out = sends(actions)
    assert len(out) == 1
    assert out[0].msg_type == "8"
    assert out[0].seq == 2                      # the session assigned it
    assert field(out[0], 37) == "O-1"
    assert session.next_out == 3


def test_app_session_reject_becomes_a_35_3():
    app = StubApp([SessionReject(7, 38, "1", "Required tag missing: 38")])
    session, _clock, _store = make_session_with_app(app)
    do_logon_with(session)

    out = sends(session.on_message(app_order()))
    assert out[0].msg_type == "3"
    assert field(out[0], 45) == "7"
    assert field(out[0], 371) == "38"
    assert field(out[0], 373) == "1"
    assert field(out[0], 58) == "Required tag missing: 38"


def test_app_evidence_is_passed_straight_through():
    app = StubApp([Evidence("rule matched", "AAPL -> default", order={"a": "b"})])
    session, _clock, _store = make_session_with_app(app)
    do_logon_with(session)

    actions = session.on_message(app_order())
    assert evidences(actions)[0].event == "rule matched"
    assert evidences(actions)[0].order == {"a": "b"}


def test_app_timer_runs_only_while_active():
    app = StubApp()
    session, clock, _store = make_session_with_app(app)

    clock.advance(1)
    session.on_timer()                          # DISCONNECTED
    assert app.timer_calls == 0

    session.on_connect()
    clock.advance(1)
    session.on_timer()                          # AWAITING_LOGON
    assert app.timer_calls == 0

    session.on_message(logon_msg(seq=1))
    clock.advance(1)
    session.on_timer()                          # ACTIVE
    assert app.timer_calls == 1

    session.initiate_logout("bye")
    clock.advance(1)
    session.on_timer()                          # LOGOUT_SENT
    assert app.timer_calls == 1


# --- scheduled events survive a disconnect ---------------------------------

def make_order_book_session():
    clock = FakeClock(START)
    store = MemorySeqStore(1, 1)
    rules = load_rules(
        [{"name": "default", "match": {"any": True}, "behavior": "full_fill"}],
        500,
    )
    book = OrderBook(OrdersConfig(), rules, clock, "RUN")
    session = Session(make_config(), store, clock, app=book)
    return session, clock, book


def test_scheduled_events_wait_for_the_next_logon():
    session, clock, book = make_order_book_session()
    do_logon_with(session)
    session.on_message(app_order(seq=2))
    assert book.has_pending_events()

    # Go down before the fill is due.
    actions = session.on_disconnect()
    assert any(e.event == "scheduled order events deferred until logon"
               for e in evidences(actions))

    # Time passes while we are down; nothing fires.
    clock.advance(60)
    assert session.on_timer() == []

    # Reconnect, log on again, and the overdue fill goes out on the first tick.
    session.on_connect()
    session.on_message(logon_msg(seq=1, reset=True))
    fills = [s for s in sends(session.on_timer()) if s.msg_type == "8"]
    assert len(fills) == 1
    assert field(fills[0], 150) == "2"


def test_deferral_evidence_is_recorded_once_per_disconnect():
    session, _clock, book = make_order_book_session()
    do_logon_with(session)
    session.on_message(app_order(seq=2))

    first = session.on_disconnect()
    second = session.on_disconnect()
    assert any(e.event == "scheduled order events deferred until logon"
               for e in evidences(first))
    assert not any(e.event == "scheduled order events deferred until logon"
                   for e in evidences(second))


def test_no_deferral_evidence_when_nothing_is_pending():
    session, _clock, _book = make_order_book_session()
    do_logon_with(session)
    actions = session.on_disconnect()
    assert not any(e.event == "scheduled order events deferred until logon"
                   for e in evidences(actions))


# --- §3.1 bounded ResendRequest --------------------------------------------

def logged_on_at(next_out):
    session, clock, store = make_session(next_out=next_out, next_in=1)
    session.on_connect()
    session.on_message(logon_msg(seq=1))
    return session, clock, store


def test_resend_request_with_finite_end_seq_no_is_bounded():
    session, _clock, _store = logged_on_at(10)
    assert session.next_out == 11

    actions = session.on_message(inbound("2", [(7, "2"), (16, "5")], seq=2))
    gap_fill = sends(actions)[0]
    assert gap_fill.msg_type == "4"
    assert gap_fill.seq_override == 2
    assert field(gap_fill, 36) == "6"           # min(5 + 1, 11)
    assert session.next_out == 11               # counter untouched


def test_resend_request_end_seq_no_is_never_past_next_out():
    session, _clock, _store = logged_on_at(10)
    actions = session.on_message(inbound("2", [(7, "2"), (16, "99")], seq=2))
    assert field(sends(actions)[0], 36) == "11"


def test_resend_request_with_zero_end_seq_no_is_unchanged():
    session, _clock, _store = logged_on_at(10)
    actions = session.on_message(inbound("2", [(7, "2"), (16, "0")], seq=2))
    assert field(sends(actions)[0], 36) == "11"


def test_resend_request_for_future_seqnums_is_ignored():
    session, _clock, _store = logged_on_at(10)
    assert session.next_out == 11

    # The request itself arrives in sequence; it is tag 7 that points past
    # anything we have sent.
    for offset, begin in enumerate(("11", "50")):
        actions = session.on_message(
            inbound("2", [(7, begin), (16, "0")], seq=2 + offset)
        )
        assert sends(actions) == []
        assert [e.event for e in evidences(actions)] == [
            "resend request for future seqnums ignored"
        ]


def test_resend_request_without_end_seq_no_is_rejected():
    session, _clock, _store = logged_on_at(10)
    actions = session.on_message(inbound("2", [(7, "2")], seq=2))
    out = sends(actions)
    assert out[0].msg_type == "3"
    assert field(out[0], 373) == "1"
