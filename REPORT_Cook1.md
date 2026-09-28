# OrderEchoFixEmulator — Cook 1 build report

Built end to end from `SPEC_Cook1.md` in one overnight run. `pytest -q` passes
fully (64 tests). No git commands were run.

---

## 1. Python version found

```
$ python3 --version
Python 3.12.1
```

3.12.1 ≥ 3.11, so the build proceeded. A venv was created at `.venv` and all
four dependencies installed from `requirements.txt`:

```
PyYAML 6.0.3   pytest 9.1.1   pytest-asyncio 1.4.0   simplefix 1.0.17
```

## 2. Files created

```
OrderEchoFixEmulator/
├── .gitignore                  .venv/, data/, logs/, __pycache__/, .pytest_cache/
├── README.md
├── REPORT_Cook1.md             (this file)
├── requirements.txt
├── pytest.ini                  see Decision 2
├── conftest.py                 see Decision 2
├── config/
│   └── orderecho.yaml
├── orderecho_Main.py           entry point, banner, Ctrl+C handling
├── orderecho_Config.py         YAML load + validation
├── orderecho_Codec.py          framing, 9/10 validation, encode, to_pipe
├── orderecho_Session.py        PURE session core
├── orderecho_SeqStore.py       MemorySeqStore + FileSeqStore
├── orderecho_Clock.py          SystemClock + FakeClock
├── orderecho_Evidence.py       JSONL evidence writer
├── orderecho_Logging.py        FIX message log + engine log
├── orderecho_Transport.py      asyncio acceptor
└── tests/
    ├── fix_TestClient.py       minimal asyncio FIX initiator
    ├── test_Codec.py           12 tests
    ├── test_Session.py         32 tests
    ├── test_SeqStore.py         5 tests
    ├── test_Logging.py          9 tests
    └── test_Integration.py      6 tests
```

`data/seqnums`, `data/evidence`, `logs/fix` and `logs/engine` are created at
runtime and gitignored.

## 3. Full output of `pytest -q`

```
$ .venv/bin/python -m pytest -q
................................................................         [100%]
64 passed in 0.74s
```

Test by test (`pytest -v`):

```
tests/test_Codec.py::test_sending_time_format PASSED
tests/test_Codec.py::test_round_trip_preserves_fields_and_order PASSED
tests/test_Codec.py::test_header_field_order_with_possdup PASSED
tests/test_Codec.py::test_body_length_and_checksum_against_hand_computed_message PASSED
tests/test_Codec.py::test_bad_checksum_is_discarded PASSED
tests/test_Codec.py::test_bad_body_length_is_discarded PASSED
tests/test_Codec.py::test_short_body_length_is_discarded PASSED
tests/test_Codec.py::test_discarded_frame_does_not_break_the_stream PASSED
tests/test_Codec.py::test_two_messages_in_one_chunk PASSED
tests/test_Codec.py::test_one_message_split_across_three_chunks PASSED
tests/test_Codec.py::test_to_pipe_replaces_soh PASSED
tests/test_Codec.py::test_leading_garbage_is_discarded PASSED
tests/test_Integration.py::test_logon_testrequest_logout_end_to_end PASSED
tests/test_Integration.py::test_second_connection_is_refused_while_active PASSED
tests/test_Integration.py::test_bad_checksum_frame_is_discarded_and_logged PASSED
tests/test_Integration.py::test_application_message_gets_business_reject_over_the_wire PASSED
tests/test_Integration.py::test_wrong_seq_number_triggers_resend_request PASSED
tests/test_Integration.py::test_first_message_not_logon_is_disconnected PASSED
tests/test_Logging.py::test_fix_log_line_format_in_out_and_disc PASSED
tests/test_Logging.py::test_fix_log_path_and_directory PASSED
tests/test_Logging.py::test_soh_delimiter_writes_real_soh PASSED
tests/test_Logging.py::test_fix_log_rolls_over_at_utc_midnight PASSED
tests/test_Logging.py::test_engine_log_format_and_rollover PASSED
tests/test_Logging.py::test_engine_log_respects_level PASSED
tests/test_Logging.py::test_engine_log_records_exceptions_with_stack_trace PASSED
tests/test_Logging.py::test_unwritable_log_dir_does_not_raise PASSED
tests/test_Logging.py::test_console_echo PASSED
tests/test_SeqStore.py::test_memory_store_round_trip PASSED
tests/test_SeqStore.py::test_file_store_missing_file_starts_at_one PASSED
tests/test_SeqStore.py::test_file_store_round_trip PASSED
tests/test_SeqStore.py::test_file_store_reset PASSED
tests/test_SeqStore.py::test_file_store_write_is_atomic PASSED
tests/test_Session.py::test_valid_logon_replies_and_activates PASSED
tests/test_Session.py::test_first_message_not_logon_disconnects_without_reply PASSED
tests/test_Session.py::test_wrong_compid_on_logon_logs_out_and_disconnects PASSED
tests/test_Session.py::test_bad_encrypt_method_logs_out PASSED
tests/test_Session.py::test_bad_heartbtint_logs_out PASSED
tests/test_Session.py::test_logon_with_reset_flag_resets_both_counters PASSED
tests/test_Session.py::test_logon_seq_too_low_logs_out_with_text PASSED
tests/test_Session.py::test_logon_seq_too_high_replies_then_requests_resend PASSED
tests/test_Session.py::test_test_request_is_answered_with_heartbeat PASSED
tests/test_Session.py::test_silence_past_heartbtint_sends_heartbeat PASSED
tests/test_Session.py::test_inbound_silence_sends_test_request_then_disconnects PASSED
tests/test_Session.py::test_heartbeat_answering_test_request_clears_it PASSED
tests/test_Session.py::test_gap_triggers_one_resend_request_only PASSED
tests/test_Session.py::test_seq_too_low_without_possdup_logs_out PASSED
tests/test_Session.py::test_seq_too_low_with_possdup_is_ignored PASSED
tests/test_Session.py::test_resend_request_is_answered_with_one_gap_fill PASSED
tests/test_Session.py::test_sequence_reset_gap_fill_advances_expected PASSED
tests/test_Session.py::test_sequence_reset_in_reset_mode_ignores_seq_num PASSED
tests/test_Session.py::test_sequence_reset_with_new_seq_no_too_low_is_rejected PASSED
tests/test_Session.py::test_gap_fill_clears_outstanding_resend_request PASSED
tests/test_Session.py::test_application_message_gets_business_reject PASSED
tests/test_Session.py::test_logon_while_active_is_session_rejected PASSED
tests/test_Session.py::test_missing_header_field_is_session_rejected PASSED
tests/test_Session.py::test_missing_seq_num_logs_out PASSED
tests/test_Session.py::test_wrong_compid_in_active_rejects_then_logs_out PASSED
tests/test_Session.py::test_inbound_reject_is_logged_only PASSED
tests/test_Session.py::test_counterparty_logout_is_answered_then_disconnected PASSED
tests/test_Session.py::test_initiated_logout_times_out_into_disconnect PASSED
tests/test_Session.py::test_logout_reply_while_logout_sent_disconnects PASSED
tests/test_Session.py::test_discarded_frame_produces_evidence_only PASSED
tests/test_Session.py::test_disconnect_persists_sequence_numbers PASSED
tests/test_Session.py::test_timer_is_idle_unless_active PASSED
============================== 64 passed in 0.72s ==============================
```

The suite was run three times in a row to check for flakiness in the socket
tests: 64 passed each time.

## 4. Manual run: start, then Ctrl+C

### 4a. Ctrl+C with no session connected

```
$ .venv/bin/python orderecho_Main.py
20260926-03:04:45.250 INFO    session  ORDERECHO-AGENT  Acceptor listening on 127.0.0.1:9878 (sender=ORDERECHO target=AGENT run_id=20260926-030445)
OrderEchoFixEmulator (Cook 1) - FIX 4.2 acceptor
  listening      : 127.0.0.1:9878
  comp ids       : sender=ORDERECHO target=AGENT
  config         : config/orderecho.yaml
  evidence file  : data/evidence/20260926-030445.jsonl
  fix log        : logs/fix
  engine log     : logs/engine
  seqnum file    : data/seqnums/ORDERECHO-AGENT.json
  Ctrl+C to shut down.
20260926-03:04:45.250 INFO    session  ORDERECHO-AGENT  Startup: config=config/orderecho.yaml host=127.0.0.1 port=9878 heartbeat_grace_pct=20.0 logout_timeout_sec=10.0 evidence=data/evidence/20260926-030445.jsonl
^C
Shutting down...
20260926-03:04:45.743 INFO    session  ORDERECHO-AGENT  Acceptor stopped
20260926-03:04:45.743 INFO    session  ORDERECHO-AGENT  Shutdown complete
[exit code 0]
```

### 4b. Ctrl+C with a session ACTIVE (the §11 logout path)

A test initiator logged on first, then Ctrl+C. The emulator sent Logout, the
client replied, and the emulator disconnected without waiting out the timeout.

```
$ .venv/bin/python orderecho_Main.py --reset-seqnums
Sequence numbers reset to 1/1 (data/seqnums/ORDERECHO-AGENT.json)
20260926-03:05:00.004 INFO    session  ORDERECHO-AGENT  Acceptor listening on 127.0.0.1:9878 (sender=ORDERECHO target=AGENT run_id=20260926-030500)
OrderEchoFixEmulator (Cook 1) - FIX 4.2 acceptor
  listening      : 127.0.0.1:9878
  comp ids       : sender=ORDERECHO target=AGENT
  config         : config/orderecho.yaml
  evidence file  : data/evidence/20260926-030500.jsonl
  fix log        : logs/fix
  engine log     : logs/engine
  seqnum file    : data/seqnums/ORDERECHO-AGENT.json
  Ctrl+C to shut down.
20260926-03:05:00.005 INFO    session  ORDERECHO-AGENT  Startup: config=config/orderecho.yaml host=127.0.0.1 port=9878 heartbeat_grace_pct=20.0 logout_timeout_sec=10.0 evidence=data/evidence/20260926-030500.jsonl
20260926-03:05:00.051 INFO    session  ORDERECHO-AGENT  Connection accepted from ('127.0.0.1', 52002)
20260926-03:05:00.051 INFO    session  ORDERECHO-AGENT  connected: awaiting Logon
20260926-03:05:00.051 INFO    session  ORDERECHO-AGENT  State DISCONNECTED -> AWAITING_LOGON
20260926-03:05:00.052 IN   seq=1    35=A  8=FIX.4.2|9=69|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260926-03:05:00.050|98=0|108=30|10=005|
20260926-03:05:00.055 OUT  seq=1    35=A  8=FIX.4.2|9=69|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260926-03:05:00.054|98=0|108=30|10=009|
20260926-03:05:00.055 INFO    session  ORDERECHO-AGENT  logon accepted: HeartBtInt=30, next_in=2 next_out=2
20260926-03:05:00.055 INFO    session  ORDERECHO-AGENT  State AWAITING_LOGON -> ACTIVE
^C
Shutting down...
20260926-03:05:00.055 INFO    session  ORDERECHO-AGENT  Initiating logout: OrderEcho shutting down
20260926-03:05:00.057 OUT  seq=2    35=5  8=FIX.4.2|9=84|35=5|49=ORDERECHO|56=AGENT|34=2|52=20260926-03:05:00.056|58=OrderEcho shutting down|10=120|
20260926-03:05:00.057 IN   seq=2    35=5  8=FIX.4.2|9=73|35=5|49=AGENT|56=ORDERECHO|34=2|52=20260926-03:05:00.057|58=acknowledged|10=118|
20260926-03:05:00.058 INFO    session  ORDERECHO-AGENT  logout confirmed: acknowledged
20260926-03:05:00.058 INFO    session  ORDERECHO-AGENT  Disconnecting: Logout confirmed
20260926-03:05:00.060 INFO    session  ORDERECHO-AGENT  disconnected: next_out=3 next_in=3
20260926-03:05:00.060 INFO    session  ORDERECHO-AGENT  State LOGOUT_SENT -> DISCONNECTED
20260926-03:05:00.060 INFO    session  ORDERECHO-AGENT  Connection closed, peer=('127.0.0.1', 52002)
20260926-03:05:00.060 INFO    session  ORDERECHO-AGENT  Acceptor stopped
20260926-03:05:00.061 INFO    session  ORDERECHO-AGENT  Shutdown complete
[exit code 0]
```

(Both runs were driven by a helper that launches the process and sends it a
real SIGINT, since the build ran non-interactively; `^C` above marks where the
signal arrived.)

## 5. Logs produced by the integration test

From `test_logon_testrequest_logout_end_to_end` (run with `--basetemp` so the
files could be read back; the test itself uses a tmp dir and `engine_level:
DEBUG`).

### `logs/fix/ORDERECHO-AGENT_20260926.log`

```
20260926-03:04:25.730 IN   seq=1    35=A  8=FIX.4.2|9=69|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260926-03:04:25.729|98=0|108=30|10=024|
20260926-03:04:25.732 OUT  seq=1    35=A  8=FIX.4.2|9=69|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260926-03:04:25.732|98=0|108=30|10=018|
20260926-03:04:25.733 IN   seq=2    35=1  8=FIX.4.2|9=69|35=1|49=AGENT|56=ORDERECHO|34=2|52=20260926-03:04:25.732|112=TEST-42|10=143|
20260926-03:04:25.734 OUT  seq=2    35=0  8=FIX.4.2|9=69|35=0|49=ORDERECHO|56=AGENT|34=2|52=20260926-03:04:25.734|112=TEST-42|10=144|
20260926-03:04:25.735 IN   seq=3    35=5  8=FIX.4.2|9=65|35=5|49=AGENT|56=ORDERECHO|34=3|52=20260926-03:04:25.734|58=done|10=062|
20260926-03:04:25.736 OUT  seq=3    35=5  8=FIX.4.2|9=80|35=5|49=ORDERECHO|56=AGENT|34=3|52=20260926-03:04:25.736|58=Logout acknowledged|10=025|
```

### `logs/engine/orderecho_20260926.log`

```
20260926-03:04:25.727 INFO    session  ORDERECHO-AGENT  Acceptor listening on 127.0.0.1:51993 (sender=ORDERECHO target=AGENT run_id=20260926-030425)
20260926-03:04:25.729 INFO    session  ORDERECHO-AGENT  Connection accepted from ('127.0.0.1', 51994)
20260926-03:04:25.729 DEBUG   session  ORDERECHO-AGENT  Action: Evidence(event='connected', detail='awaiting Logon')
20260926-03:04:25.730 INFO    session  ORDERECHO-AGENT  connected: awaiting Logon
20260926-03:04:25.730 INFO    session  ORDERECHO-AGENT  State DISCONNECTED -> AWAITING_LOGON
20260926-03:04:25.732 DEBUG   session  ORDERECHO-AGENT  Action: Send(msg_type='A', body_fields=[(98, '0'), (108, '30')], poss_dup=False, orig_sending_time=None, seq_override=None, seq=1)
20260926-03:04:25.732 DEBUG   session  ORDERECHO-AGENT  Action: Evidence(event='logon accepted', detail='HeartBtInt=30, next_in=2 next_out=2')
20260926-03:04:25.732 INFO    session  ORDERECHO-AGENT  logon accepted: HeartBtInt=30, next_in=2 next_out=2
20260926-03:04:25.732 INFO    session  ORDERECHO-AGENT  State AWAITING_LOGON -> ACTIVE
20260926-03:04:25.734 DEBUG   session  ORDERECHO-AGENT  Action: Send(msg_type='0', body_fields=[(112, 'TEST-42')], poss_dup=False, orig_sending_time=None, seq_override=None, seq=2)
20260926-03:04:25.736 DEBUG   session  ORDERECHO-AGENT  Action: Send(msg_type='5', body_fields=[(58, 'Logout acknowledged')], poss_dup=False, orig_sending_time=None, seq_override=None, seq=3)
20260926-03:04:25.736 DEBUG   session  ORDERECHO-AGENT  Action: Disconnect(reason='Logout requested by counterparty')
20260926-03:04:25.736 INFO    session  ORDERECHO-AGENT  Disconnecting: Logout requested by counterparty
20260926-03:04:25.736 INFO    session  ORDERECHO-AGENT  State ACTIVE -> LOGOUT_SENT
20260926-03:04:25.737 DEBUG   session  ORDERECHO-AGENT  Action: Evidence(event='disconnected', detail='next_out=4 next_in=4')
20260926-03:04:25.737 INFO    session  ORDERECHO-AGENT  disconnected: next_out=4 next_in=4
20260926-03:04:25.737 INFO    session  ORDERECHO-AGENT  State LOGOUT_SENT -> DISCONNECTED
20260926-03:04:25.737 INFO    session  ORDERECHO-AGENT  Connection closed, peer=('127.0.0.1', 51994)
20260926-03:04:25.739 INFO    session  ORDERECHO-AGENT  Acceptor stopped
```

### Matching evidence and seqnum file

`data/seqnums/ORDERECHO-AGENT.json`:

```json
{"next_out": 4, "next_in": 4}
```

`data/evidence/20260926-030425.jsonl` (the six message records; four `event`
records for connect/logon/disconnecting/disconnected are interleaved and
omitted here for length — the test asserts on all of them):

```json
{"ts": "2026-09-26T03:04:25.730Z", "run_id": "20260926-030425", "kind": "in", "session": "ORDERECHO-AGENT", "seq": 1, "msg_type": "A", "raw": "8=FIX.4.2|9=69|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260926-03:04:25.729|98=0|108=30|10=024|", "fields": {"8": "FIX.4.2", "9": "69", "35": "A", "49": "AGENT", "56": "ORDERECHO", "34": "1", "52": "20260926-03:04:25.729", "98": "0", "108": "30", "10": "024"}, "detail": null, "injected": false}
{"ts": "2026-09-26T03:04:25.732Z", "run_id": "20260926-030425", "kind": "out", "session": "ORDERECHO-AGENT", "seq": 1, "msg_type": "A", "raw": "8=FIX.4.2|9=69|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260926-03:04:25.732|98=0|108=30|10=018|", "fields": {"8": "FIX.4.2", "9": "69", "35": "A", "49": "ORDERECHO", "56": "AGENT", "34": "1", "52": "20260926-03:04:25.732", "98": "0", "108": "30", "10": "018"}, "detail": null, "injected": false}
{"ts": "2026-09-26T03:04:25.733Z", "run_id": "20260926-030425", "kind": "in", "session": "ORDERECHO-AGENT", "seq": 2, "msg_type": "1", "raw": "8=FIX.4.2|9=69|35=1|49=AGENT|56=ORDERECHO|34=2|52=20260926-03:04:25.732|112=TEST-42|10=143|", "fields": {"8": "FIX.4.2", "9": "69", "35": "1", "49": "AGENT", "56": "ORDERECHO", "34": "2", "52": "20260926-03:04:25.732", "112": "TEST-42", "10": "143"}, "detail": null, "injected": false}
{"ts": "2026-09-26T03:04:25.734Z", "run_id": "20260926-030425", "kind": "out", "session": "ORDERECHO-AGENT", "seq": 2, "msg_type": "0", "raw": "8=FIX.4.2|9=69|35=0|49=ORDERECHO|56=AGENT|34=2|52=20260926-03:04:25.734|112=TEST-42|10=144|", "fields": {"8": "FIX.4.2", "9": "69", "35": "0", "49": "ORDERECHO", "56": "AGENT", "34": "2", "52": "20260926-03:04:25.734", "112": "TEST-42", "10": "144"}, "detail": null, "injected": false}
{"ts": "2026-09-26T03:04:25.735Z", "run_id": "20260926-030425", "kind": "in", "session": "ORDERECHO-AGENT", "seq": 3, "msg_type": "5", "raw": "8=FIX.4.2|9=65|35=5|49=AGENT|56=ORDERECHO|34=3|52=20260926-03:04:25.734|58=done|10=062|", "fields": {"8": "FIX.4.2", "9": "65", "35": "5", "49": "AGENT", "56": "ORDERECHO", "34": "3", "52": "20260926-03:04:25.734", "58": "done", "10": "062"}, "detail": null, "injected": false}
{"ts": "2026-09-26T03:04:25.736Z", "run_id": "20260926-030425", "kind": "out", "session": "ORDERECHO-AGENT", "seq": 3, "msg_type": "5", "raw": "8=FIX.4.2|9=80|35=5|49=ORDERECHO|56=AGENT|34=3|52=20260926-03:04:25.736|58=Logout acknowledged|10=025|", "fields": {"8": "FIX.4.2", "9": "80", "35": "5", "49": "ORDERECHO", "56": "AGENT", "34": "3", "52": "20260926-03:04:25.736", "58": "Logout acknowledged", "10": "025"}, "detail": null, "injected": false}
```

## 6. Ambiguities

The overnight instruction replaced "stop and ask" with "choose the most
conservative, spec-consistent option and log it". Everything below is a choice
I made rather than a question I asked; the three worth a second opinion are
flagged **Worth confirming** in the next section.

---

# Decisions I made

Every ambiguity I hit, and what I chose. Nothing here changed the shape of what
§2 scopes; where the spec was explicit I followed it literally even where
general FIX practice differs (Decisions 9 and 12).

**1. `Send` needs to carry the assigned sequence number.**
§7.1 fixes the `Send` signature and §7.1's rules say the session assigns 34 for
every outbound message, but nothing in the signature tells the transport *what*
number was assigned. I added one extra field, `Send.seq`, which the session
fills in (the freshly consumed number, or the `seq_override` for a gap fill).
The spec'd constructor arguments are unchanged; `seq_override` still means only
"gap fill, do not consume a number".

**2. Added `conftest.py` and `pytest.ini`, which are not in the §4 layout.**
The empty root `conftest.py` is what makes pytest put the repo root on
`sys.path`, so `tests/*` can `import orderecho_Codec`. `pytest.ini` sets
`asyncio_mode = auto` (pytest-asyncio 1.4 needs it to run the async integration
tests) and `testpaths = tests`. Neither adds behaviour; without them
`pytest -q` cannot run at all.

**3. `Session(config, …)` accepts either the whole `Config` or just its session
section.** §7.1 says "config" without saying which. It reads
`getattr(config, "session", config)`, so the transport can hand it the whole
`Config` and unit tests can hand it a bare `SessionConfig`.

**4. Evidence records for events.** The §10 schema has no field for the event
*name*, but the `Evidence` action in §7.1 has both `event` and `detail`. Rather
than add a key the spec does not list, the event name is written at the front of
`detail` (`"seq gap detected: received MsgSeqNum 5, expecting 2"`). The
documented field set is exactly as §10 specifies.

**5. Logon reply echoes `141=Y` only when the inbound Logon carried `141=Y`.**
§7.3.6 says "`141=Y` if they sent it". An inbound `141=N` is treated as not
sent, and nothing is echoed.

**6. A Logon with a too-high MsgSeqNum does not advance the expected inbound
number.** §7.3.5 says accept, reply, then ResendRequest; §7.4 says a gap must
not advance expected. So after a Logon at 9 when 5 was expected, expected stays
at 5, the ResendRequest asks for `7=5, 16=0`, and the counterparty's gap fill
moves it past the Logon. The alternative (consuming the Logon's number) would
have left a hole that nothing closes.

**7. Order of the checks on an inbound message in ACTIVE.** §7.4 and §7.5 each
describe checks without fixing their order. I used, in this order:
(a) 34 missing → Logout + Disconnect; (b) CompIDs *present but wrong* → Reject
`373=9` then Logout + Disconnect; (c) SequenceReset in Reset mode → skip the
sequence check; (d) sequence check; (e) 35/49/56/52 missing → Reject `373=1`;
(f) dispatch. A message from the wrong counterparty is rejected before it can
touch our sequence numbers, and a gap is handled before anything about the
message body is trusted.

**8. Missing CompID vs. wrong CompID.** §7.5 asks for `373=1` for a missing
required header field and `373=9` for wrong CompIDs; 49/56 are both. Missing →
`373=1` (required tag missing, Reject only). Present but not matching the
configured pair → `373=9` (CompID problem, Reject + Logout + Disconnect).

**9. The gap fill's `36` when a ResendRequest has a finite EndSeqNo.**
§7.6 says the reply carries `36=<our current next outbound seq>` and §7.5 says
`16=0` means "to infinity". I followed §7.6 literally: `36` is always our next
outbound number, whatever `16` said. **Worth confirming** — strict FIX would
answer `7=5, 16=8` with a gap fill to `36=9` only, then wait for a further
request. Since this cook gap-fills everything and never replays application
messages, the literal reading loses nothing, but it is a visible difference if
the agent ever asks for a bounded range.

**10. TestRequest with no `112`.** §7.5 says reply with "the same 112", which
does not exist here. Chose session Reject `373=1` (required tag missing) rather
than inventing a TestReqID or replying with a bare Heartbeat.

**11. SequenceReset with a bad `36`.** §7.6 covers `36` lower than expected
(`373=5`) but not `36` missing or non-numeric. Missing → Reject `373=1`
(required tag missing); present but not a number → Reject `373=6` (incorrect
data format for value). Neither changes the expected number.

**12. The Reject for a Logon received while already logged on carries no
`373`.** §7.5 lists exactly `45=<seq>` and `58=Logon received while already
logged on` for this case, while giving a 373 for every other Reject in the
table. Followed literally. **Worth confirming** — most engines would send
`373=9` or `373=11` here.

**13. Our reply to a counterparty Logout carries `58=Logout acknowledged`.**
§7.5 says "reply Logout" without giving text. A human reading the log wants to
know which Logout is which, and 58 is optional, so it is filled in.

**14. "Any application message" means any MsgType outside the session set.**
The session set is `0, 1, 2, 3, 4, 5, A`; everything else (D, F, G, 8, j, …)
gets the BusinessMessageReject from §7.5. Unknown/garbage MsgTypes take the same
path, with `372` echoing whatever arrived.

**15. Framing is done in this repo, not by simplefix.** §6 requires that we
verify 9 and 10 ourselves and hand back the failing bytes intact.
`simplefix.FixParser` frames on the checksum field and would not give us the
original bytes of a bad frame, so `orderecho_Codec` locates frames itself
(BeginString → BodyLength → trailer) and uses simplefix for field parsing inside
a validated frame and for encoding (`FixMessage.encode()` computes 9 and 10 and
fixes the position of 8/9/35/10). The result is still "on top of simplefix" as
§6 asks.

**16. Bytes before a BeginString, and unframeable junk, become DiscardedFrames.**
§6 only names 9 and 10 failures. Leading garbage, a BodyLength that is not a
number, and a BodyLength with no trailer where it should be are all returned as
`DiscardedFrame` with a reason, so nothing vanishes from the evidence log. A
buffer that grows past 1 MB without yielding a frame is discarded the same way,
so a wedged peer cannot grow memory without bound.

**17. Ctrl+C is implemented with asyncio signal handlers, not
`except KeyboardInterrupt`.** §11 requires a Logout and a bounded wait on
Ctrl+C. Catching `KeyboardInterrupt` around `asyncio.run` races with the loop's
own teardown; `loop.add_signal_handler(SIGINT, …)` (and SIGTERM, same handler)
makes the shutdown path deterministic — see §4b above, where the Logout goes out
and the reply is processed before exit.

**18. Evidence `fields` is a JSON object keyed by tag.** §10's example shows an
object, and Python dicts preserve insertion order, so tag order survives — but a
repeated tag would keep only its last value. No session-layer message in this
cook repeats a tag; if Cook 2 brings repeating groups this needs revisiting.

**19. Console echo is `print()` to stdout.** §10A says `console: true` echoes
FIX log lines and INFO+ engine lines to the terminal; both streams do exactly
that and nothing else goes to stdout except the startup banner.

**20. Engine log `%(name)s` is always `session`.** The §10A format has a
`session` column ("`INFO    session  ORDERECHO-AGENT`"), which reads as a fixed
logger name followed by the session identity. Implemented that way; each
`EngineLog` instance gets its own underlying logger so parallel tests do not
share handlers, but the rendered name stays `session`.

**21. The integration test runs with `engine_level: DEBUG`** so that the "DEBUG
additionally logs every Action" requirement from §10A is exercised end to end.
The shipped `config/orderecho.yaml` keeps `INFO` as the spec specifies.

**22. One test assertion of mine was wrong and I fixed the test, not the
code.** In `test_fix_log_line_format_in_out_and_disc` I wrote a column-alignment
assertion with the wrong arithmetic (`index("8=FIX.4.2") == 36`; the correct
offset is 42: 21 + 1 + 4 + 1 + 8 + 1 + 5 + 1). The exact full-line equality
assertions in the same test — which pin the §10A format character for character,
including the spec's own sample lines — were already passing and are unchanged.
No spec-derived assertion was weakened anywhere in the suite.

---

## Questions for you (nothing was blocked by these)

1. Decision 9 — should a ResendRequest with a finite `16=N` be answered with a
   gap fill to `N+1` instead of to our next outbound number?
2. Decision 12 — should the "Logon while already logged on" Reject carry a
   `373`, and if so which value?
3. Decision 5 — should an inbound `141=N` be echoed back as `141=N`, or is
   silence right?

## Out of scope, confirmed not built

No order handling, no fill rules or pricing, no FastAPI control API, no docs
site, no application-message replay on resend (everything is gap-filled), no
TLS, no concurrent sessions, no session schedules. Cook 2 was not started.
