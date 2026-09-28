# OrderEchoFixEmulator — Cook 1 Build Spec
**Session layer: codec, pure session core, asyncio acceptor, evidence log, logs, tests**

---

## 0. Ground rules for Claude Code

- Do **not** run any git commands (no init, add, commit, push).
- Do **not** make product decisions. If anything in this spec is ambiguous or contradictory, **stop and explain** — do not guess.
- Build only what is in scope (§2). Do not add features from later cooks.
- When finished, report back using the format in §14.

---

## 1. Context

OrderEchoFixEmulator is a local FIX 4.2 counterparty emulator. It acts as a **FIX acceptor** (the broker/exchange side). The OrderEcho agent (Go, separate repo, built later) connects to it as the initiator.

**The emulator is one possible target, not a dependency.** The agent must work against any real FIX engine, so nothing here may assume the agent is the only client, and the agent will never rely on emulator internals except through the optional control API (Cook 3).

Core principle: **the session logic is pure and deterministic.** It never touches sockets or the real clock. Inputs in → actions out. Everything is testable without a network.

---

## 2. Scope

**In scope (Cook 1)**
- FIX message codec (on top of `simplefix`)
- Pure session core: Logon, Heartbeat, TestRequest, sequence numbers, ResendRequest, SequenceReset, Logout, session Reject
- asyncio TCP acceptor (transport)
- Persistent sequence number store
- JSONL evidence log
- Human-readable FIX message log + engine log
- Minimal YAML config (session settings only)
- pytest unit tests + one end-to-end integration test with a test initiator

**Out of scope (later cooks — do not build)**
- Order handling (NewOrderSingle, fills, cancels) — Cook 2
- Symbol fill rules, pricing / yfinance — Cook 2
- FastAPI control API — Cook 3
- Documentation site — Cook 4
- Replaying application messages on resend (we gap-fill everything)
- TLS, multiple concurrent sessions, session schedules

---

## 3. Environment

- Repo: `~/Dropbox/code/GitHub/OrderEchoFixEmulator`
- Python **3.11 or newer**. Run `python3 --version` first. If lower than 3.11, **stop and report**.
- Create a venv at `.venv` inside the repo.
- Dependencies (`requirements.txt`): `simplefix`, `pyyaml`, `pytest`, `pytest-asyncio`
- Ports: FIX acceptor on **9878**. (Do not use 5000 or 5001 — both are taken on this machine.)

---

## 4. File layout

Naming convention: app files use the `orderecho_` prefix + CamelCase.

```
OrderEchoFixEmulator/
├── README.md
├── requirements.txt
├── config/
│   └── orderecho.yaml
├── orderecho_Main.py          # entry point
├── orderecho_Config.py        # load + validate YAML
├── orderecho_Codec.py         # encode/decode/validate framing
├── orderecho_Session.py       # PURE session core (no I/O)
├── orderecho_SeqStore.py      # MemorySeqStore + FileSeqStore
├── orderecho_Clock.py         # SystemClock + FakeClock
├── orderecho_Evidence.py      # JSONL evidence writer
├── orderecho_Logging.py       # FIX message log + engine log
├── orderecho_Transport.py     # asyncio acceptor
├── data/                      # created at runtime (gitignored)
│   ├── seqnums/
│   └── evidence/
├── logs/                      # created at runtime (gitignored)
│   ├── fix/                   # FIX messages only, one per line
│   └── engine/                # engine events, state changes, errors
└── tests/
    ├── fix_TestClient.py      # minimal asyncio FIX initiator for tests
    ├── test_Codec.py
    ├── test_Session.py
    ├── test_SeqStore.py
    ├── test_Logging.py
    └── test_Integration.py
```

Also create `.gitignore` containing `.venv/`, `data/`, `logs/`, `__pycache__/`, `.pytest_cache/`.

---

## 5. Config — `config/orderecho.yaml`

```yaml
session:
  fix_version: FIX.4.2
  sender_comp_id: ORDERECHO     # us
  target_comp_id: AGENT         # the counterparty we expect
  host: 127.0.0.1
  port: 9878
  heartbeat_grace_pct: 20       # TestRequest after HeartBtInt * 1.2 of silence
  logout_timeout_sec: 10        # wait this long for Logout reply before disconnecting

storage:
  seqnum_dir: data/seqnums
  evidence_dir: data/evidence

logging:
  log_dir: logs
  fix_delimiter: "|"            # "|" for readability; "SOH" writes real \x01
  engine_level: INFO            # DEBUG | INFO | WARNING | ERROR
  console: true                 # also echo to terminal
```

`orderecho_Config.py` loads this, validates required keys and types, and raises a clear error naming any missing/invalid key.

---

## 6. Codec — `orderecho_Codec.py`

Wrap `simplefix`. Responsibilities:

**Decode**
- Accept raw bytes incrementally; return zero or more complete messages.
- For every framed message, **verify BodyLength (9) and CheckSum (10) ourselves** (do not assume `simplefix` validates them).
- A message failing 9 or 10 validation is **discarded** (not rejected — per FIX spec) and returned as a `DiscardedFrame` with the reason, so the evidence log can record it.
- Decoded messages expose: `msg_type`, `get(tag)`, ordered list of `(tag, value)` pairs, and `raw` bytes.

**Encode**
- Input: msg_type + list of body `(tag, value)` pairs + header values (49, 56, 34, 52, and optional 43, 122).
- Output: bytes with correct field order: `8, 9, 35, 49, 56, 34, 52, [43, 122], body..., 10`.
- SendingTime (52) and OrigSendingTime (122) format: `YYYYMMDD-HH:MM:SS.sss` in UTC.

**Display helper**
- `to_pipe(raw) -> str` replaces SOH with `|` for logs and evidence.

---

## 7. Session core — `orderecho_Session.py` (PURE)

### 7.1 Interface

```python
class Session:
    def __init__(self, config, seq_store, clock): ...

    def on_message(self, msg) -> list[Action]
    def on_discarded(self, frame) -> list[Action]
    def on_timer(self) -> list[Action]          # called ~1/sec by transport
    def on_connect(self) -> list[Action]
    def on_disconnect(self) -> list[Action]
    def initiate_logout(self, text: str) -> list[Action]
```

**Action types** (plain dataclasses):
- `Send(msg_type, body_fields, poss_dup=False, orig_sending_time=None, seq_override=None)`
- `Disconnect(reason)`
- `Evidence(event, detail)` — a non-message event for the log (e.g. "seq gap detected")

Rules:
- The session **owns sequence numbers.** It assigns MsgSeqNum (34) for every outbound `Send` (except `seq_override`, used only for gap fills — see 7.6) and persists changes via `seq_store`.
- The session reads time **only** from the injected `clock`.
- No sockets, no `datetime.now()`, no randomness. TestReqIDs come from a counter: `TEST-1`, `TEST-2`, …

### 7.2 States

`DISCONNECTED → AWAITING_LOGON → ACTIVE → LOGOUT_SENT → DISCONNECTED`

- `on_connect` → `AWAITING_LOGON`
- `on_disconnect` → `DISCONNECTED` (seq numbers persist)

### 7.3 Logon (35=A) in AWAITING_LOGON

1. If the first message is **not** Logon → `Disconnect("First message not Logon")`, no reply.
2. Validate SenderCompID (49) == configured `target_comp_id` and TargetCompID (56) == configured `sender_comp_id`. Mismatch → `Send Logout` with 58 explaining, then `Disconnect`.
3. EncryptMethod (98) must be `0`. HeartBtInt (108) must be a positive int. Otherwise → Logout + Disconnect.
4. If ResetSeqNumFlag (141=Y): reset inbound expected to 1 and outbound next to 1 **before** the seq check.
5. Seq check on the Logon (34):
   - equal to expected → accept.
   - higher than expected → accept Logon, reply Logon, **then** send ResendRequest (see 7.4).
   - lower than expected (and no 141=Y) → Logout with 58 `MsgSeqNum too low, expecting X but received Y`, then Disconnect.
6. Reply Logon: `98=0`, `108=<their HeartBtInt>`, and `141=Y` if they sent it.
7. Store HeartBtInt; state → `ACTIVE`.

### 7.4 Sequence checks in ACTIVE (every inbound message)

- **equal** → process, increment expected.
- **higher** (gap) → send ResendRequest `7=<expected>`, `16=0`. Do **not** process the message and do not advance expected. Only one outstanding ResendRequest at a time; do not re-send one while it's open.
- **lower with 43=Y** → ignore; `Evidence("possdup ignored")`.
- **lower without 43=Y** → Logout `MsgSeqNum too low, expecting X but received Y`, then Disconnect.

Exception: SequenceReset in **Reset mode** (see 7.6) skips the seq check.

### 7.5 Message handling in ACTIVE

| Inbound | Behavior |
|---|---|
| Heartbeat (0) | Nothing. If it carries 112 matching our pending TestRequest, clear the pending flag. |
| TestRequest (1) | Reply Heartbeat with the same `112`. |
| ResendRequest (2) | Reply one SequenceReset-GapFill (see 7.6) covering the requested range. `16=0` means "to infinity". |
| Reject (3) | Log only. |
| SequenceReset (4) | See 7.6. |
| Logout (5) | If state is ACTIVE: reply Logout, then Disconnect. If state is LOGOUT_SENT: Disconnect. |
| Logon (A) in ACTIVE | Session Reject (35=3), `45=<seq>`, `58=Logon received while already logged on`. |
| Any application message (e.g. D, F, G) | Reply BusinessMessageReject (35=j) with `45=<seq>`, `372=<msgtype>`, `380=3` (Unsupported Message Type), `58=Not supported in this build`. Increment expected normally. |
| Missing required header field (35, 34, 49, 56, 52) | If 34 is missing → Logout + Disconnect. Otherwise session Reject (35=3) with `45`, `373=1` (Required tag missing), `58`. |
| Wrong CompIDs in ACTIVE | Session Reject (373=9, CompID problem), then Logout + Disconnect. |

### 7.6 SequenceReset and gap fills

**Inbound SequenceReset (35=4)**
- Gap-fill mode (`123=Y`): subject to normal seq check. If it passes, set expected = NewSeqNo (36).
- Reset mode (`123` absent or `N`): ignore 34, set expected = 36.
- If 36 is lower than current expected → session Reject (35=3), `373=5` (Value incorrect), `58=NewSeqNo too low`. Do not change expected.
- Clears any outstanding ResendRequest if expected now covers the gap.

**Outbound gap fill (our reply to a ResendRequest)**
- Send 35=4 with `34=<BeginSeqNo from their request>` (via `seq_override`), `43=Y`, `122=<now>`, `123=Y`, `36=<our current next outbound seq>`.
- A gap-fill Send does **not** consume or increment our outbound counter.

### 7.7 Timers — `on_timer()`

Only in ACTIVE or LOGOUT_SENT:
- If `now - last_sent >= HeartBtInt` → send Heartbeat.
- If `now - last_received >= HeartBtInt * (1 + grace_pct/100)` and no TestRequest pending → send TestRequest `112=TEST-<n>`, mark pending with timestamp.
- If a TestRequest has been pending for `>= HeartBtInt` with no inbound traffic → `Disconnect("TestRequest timeout")`.
- In LOGOUT_SENT: if `now - logout_sent_at >= logout_timeout_sec` → `Disconnect("Logout timeout")`.

Any inbound message updates `last_received`. Any outbound Send updates `last_sent`.

### 7.8 Initiated logout

`initiate_logout(text)` → Send Logout with `58=text`, state → `LOGOUT_SENT`, record `logout_sent_at`.

---

## 8. Sequence store — `orderecho_SeqStore.py`

- Interface: `load() -> (next_out, next_in)`, `save(next_out, next_in)`, `reset()`
- `MemorySeqStore` for tests.
- `FileSeqStore`: file `data/seqnums/<SENDER>-<TARGET>.json` → `{"next_out": n, "next_in": m}`.
  - Missing file → start at `(1, 1)`.
  - Writes are atomic: write temp file, then `os.replace`.
  - Write-through on every change.

---

## 9. Clock — `orderecho_Clock.py`

- `SystemClock.now()` → timezone-aware UTC datetime.
- `FakeClock(start)` with `now()` and `advance(seconds)` for tests.

---

## 10. Evidence — `orderecho_Evidence.py`

- One JSONL file per engine run: `data/evidence/<run_id>.jsonl`, where `run_id` = `YYYYMMDD-HHMMSS` UTC at startup.
- One JSON object per line. Fields:

```json
{
  "ts": "2026-09-26T14:03:22.114Z",
  "run_id": "20260926-140301",
  "kind": "in | out | discarded | event",
  "session": "ORDERECHO-AGENT",
  "seq": 5,
  "msg_type": "1",
  "raw": "8=FIX.4.2|9=...|35=1|...|10=123|",
  "fields": {"35": "1", "112": "TEST-1"},
  "detail": "free text for events and discards",
  "injected": false
}
```

- `raw` uses `|` instead of SOH. `fields` preserves tag order. `seq`, `msg_type`, `raw`, `fields` are null for pure events.
- `injected` is always `false` in this cook (reserved for deliberate mischief later).
- Flush after every line.
- Console output is handled by §10A.

---

## 10A. Logs — `orderecho_Logging.py`

Evidence (§10) is for machines and test reports. **Logs are for humans** — grep, tail, and paste into a decoder. Two separate streams:

### FIX message log — `logs/fix/<SENDER>-<TARGET>_<YYYYMMDD>.log`

- **Every FIX message in and out, including discarded frames. Nothing else.**
- One message per line, fixed format:

```
20260926-14:03:22.114 IN   seq=5    35=1  8=FIX.4.2|9=62|35=1|49=AGENT|56=ORDERECHO|34=5|52=20260926-14:03:22.110|112=TEST-1|10=201|
20260926-14:03:22.115 OUT  seq=6    35=0  8=FIX.4.2|9=64|35=0|49=ORDERECHO|56=AGENT|34=6|52=20260926-14:03:22.115|112=TEST-1|10=044|
20260926-14:03:25.002 DISC -        -     8=FIX.4.2|9=99|35=D|...|10=000|   # bad checksum
```

- Columns: UTC timestamp (ms), direction (`IN` / `OUT` / `DISC`), seq, MsgType, then the raw message.
- Columns are padded so they align. **The raw message is always last**, so a line can be cut and pasted straight into a FIX decoder.
- DISC lines append `  # <reason>`.
- Delimiter per config (`|` default, or real SOH).
- Timestamp is when the emulator read or wrote the bytes, not tag 52.

### Engine log — `logs/engine/orderecho_<YYYYMMDD>.log`

- Standard Python `logging`, format:
  `20260926-14:03:22.114 INFO    session  ORDERECHO-AGENT  Logon accepted, HeartBtInt=30, next_in=2 next_out=2`
- Records: startup/shutdown (with config summary), connect/disconnect with peer address, every session state transition, seq gaps, resend requests sent/received, TestRequest timeouts, refused second connections, discarded frames (reason), and every exception with stack trace.
- Level from config. DEBUG additionally logs every Action the session returns.

### Rules for both

- Append-only, never truncated. New file when the UTC date changes (check on each write).
- Flush after every line — a crash must not lose the last messages.
- Directories created automatically.
- If `console: true`, echo the FIX log lines and INFO+ engine lines to the terminal.
- Logging failures (disk full, permissions) must never crash the session — write the error to stderr and keep running.

---

## 11. Transport & entry point

**`orderecho_Transport.py`**
- `asyncio.start_server` on configured host/port.
- **One session at a time.** If a second connection arrives while one is active, log an event and close the new connection immediately.
- Feeds bytes → Codec → `session.on_message` / `session.on_discarded`.
- Executes returned actions: encode + write for `Send`, close for `Disconnect`, write to evidence and logs for everything.
- Runs `session.on_timer()` once per second while connected.
- On socket close/error → `session.on_disconnect()`.

**`orderecho_Main.py`**
- `python orderecho_Main.py [--config PATH] [--reset-seqnums]`
- Loads config, starts transport, prints a startup banner (version, port, CompIDs, evidence file path, FIX log path, engine log path).
- Ctrl+C: if a session is ACTIVE, `initiate_logout("OrderEcho shutting down")`, wait up to `logout_timeout_sec` for the reply, then exit cleanly.

---

## 12. Tests

**`test_Codec.py`**
- Encode → decode round trip preserves fields and order.
- BodyLength and CheckSum computed correctly (check against a hand-computed known message).
- Bad checksum → `DiscardedFrame`. Bad body length → `DiscardedFrame`.
- Two messages in one chunk → both decoded. One message split across three chunks → decoded once complete.

**`test_Session.py`** (FakeClock + MemorySeqStore, no sockets)
1. Valid Logon → Logon reply, state ACTIVE, seq numbers advance.
2. First message not Logon → Disconnect, no reply.
3. Wrong CompID on Logon → Logout + Disconnect.
4. Logon with 141=Y after prior traffic → both seq numbers reset.
5. Logon seq too low → Logout with correct text + Disconnect.
6. Logon seq too high → Logon reply then ResendRequest with correct 7/16.
7. Inbound TestRequest → Heartbeat echoing 112.
8. Silence past HeartBtInt → we send Heartbeat.
9. Inbound silence past grace → we send TestRequest `TEST-1`; no reply for HeartBtInt → Disconnect.
10. Inbound gap in ACTIVE → one ResendRequest; second gapped message doesn't trigger another.
11. Inbound seq too low without PossDup → Logout + Disconnect. With PossDup → ignored.
12. Inbound ResendRequest → one SequenceReset-GapFill with correct 34, 43, 122, 123, 36; outbound counter unchanged.
13. Inbound SequenceReset gap-fill → expected jumps to 36. Reset mode ignores 34. NewSeqNo lower than expected → Reject 373=5.
14. Application message (35=D) → BusinessMessageReject (35=j) with 45, 372, 380=3.
15. Counterparty Logout → our Logout reply + Disconnect.
16. `initiate_logout` → Logout sent, LOGOUT_SENT; no reply within timeout → Disconnect.

**`test_Logging.py`**
- FIX log line format matches §10A exactly (columns, padding, raw last), for IN, OUT, and DISC.
- `fix_delimiter: SOH` writes real `\x01`.
- Date rollover (FakeClock across midnight UTC) creates a new file for both streams.
- An unwritable log dir does not raise out of the logger.

**`test_SeqStore.py`**
- File store round trip; missing file starts at (1,1); reset works.

**`tests/fix_TestClient.py`**
- Minimal asyncio initiator using `orderecho_Codec`: connect, send arbitrary messages with arbitrary seq numbers, read replies. It must be able to send deliberately wrong seq numbers (tests need that).

**`test_Integration.py`** (real sockets, localhost, random free port)
- Start the acceptor, connect the test client.
- Logon → TestRequest → receive Heartbeat with echoed 112 → Logout → receive Logout → connection closed.
- Assert the evidence JSONL contains every in/out message in order with correct seq numbers and `injected: false`.
- Assert the seqnum file reflects final state.
- Assert the FIX log contains exactly the same messages as the evidence file, same order, one line each.
- Assert the engine log contains the Logon accepted, Logout, and disconnect entries.
- A second simultaneous connection is refused while the first is active.

All tests must pass with `pytest -q`.

---

## 13. README.md

Short: what OrderEchoFixEmulator is (one paragraph), setup (venv + pip install), how to run, how to run tests, where evidence, seqnum, and log files go. Plain Markdown — the styled docs come in Cook 4.

---

## 14. Report back

When done, report:
1. Python version found.
2. Files created (list).
3. Full output of `pytest -q`.
4. Output of a manual run: start `orderecho_Main.py`, then Ctrl+C — paste the console output.
5. The contents of the FIX log and engine log produced by the integration test.
6. Anything in this spec you found ambiguous, and what you did about it (you should have stopped and asked for anything significant).
