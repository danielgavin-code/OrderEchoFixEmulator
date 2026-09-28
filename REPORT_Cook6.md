# OrderEchoFixEmulator — Cook 6 build report

Built end to end from `SPEC_Cook6.md`. `pytest -q` passes fully: **495 passed**,
up from Cook 5's 453. No git commands were run. Cook 7 was not started.

**The 4.4 golden fixtures were captured first**, from untouched Cook 5 code,
before the multi-session refactor began. Both sets are byte-identical to their
captures:

```
4.2  39ee14e903d0b587894c8e8e61b7be622d84cdfe3e2f3c6a7bdabbd09487706e   (Cook 5 capture, unchanged)
4.4  4b85f864a797a2b4c5e533d2c47c053bafec41d870efaa963ef96c0764807e83   (captured before this refactor)
```

Background shell: nothing was running this time — the Ctrl+C fix in §3.1 is
what stops a `session`-mode client outliving its engine, and that is now tested.

---

## 1. Files created and changed

**New (3)**

| File | What it is |
|---|---|
| `config/orderecho_multi.yaml` | The three sessions from §4: two sharing a port, one with its own port, rules and band. |
| `tests/golden/fix44/*.txt` | Ten frozen 4.4 fixtures. |
| `tests/test_MultiSession.py` | 37 tests — routing, isolation, per-session files, the API, config validation, shutdown. |

**Changed (12)**

| File | Change |
|---|---|
| `orderecho_Transport.py` | Split into `SessionRuntime` (everything one session owns) and `Transport` (the engine: acceptors, routing, pricing, ID generator). One acceptor per port; Logons routed by the identity triple; shutdown logs every session out at once. Legacy single-session views keep the old surface. |
| `orderecho_Config.py` | `EngineConfig`, `SessionSpec`, the `sessions:` loader with defaults-merging, and §4's validation. A legacy `session:` block still loads as one session with its old id. |
| `orderecho_ControlApi.py` | `/sessions` and `/sessions/{id}/…`; order routes made engine-wide; legacy routes act on the default session or answer `409 ambiguous_session`; `/health` reports the session count. |
| `orderecho_OrderBook.py` | `IdGenerator` so OrderIDs and ExecIDs are unique across sessions. |
| `orderecho_Session.py` | `expected_versions`, so a wrong-dialect Logon can be told every version those CompIDs could have used. |
| `orderecho_Evidence.py`, `orderecho_Logging.py` | Per-record / per-line session, so one evidence file and one engine log can carry several sessions. |
| `orderecho_SeqStore.py`, `orderecho_MessageStore.py` | Files named by session id (defaulting to the CompID pair, so legacy names do not move). |
| `orderecho_Main.py` | Session table in the banner; `--reset-seqnums` covers every session (see decision 12); startup line lists them. |
| `orderecho_DemoClient.py` | `--session`, globals before or after the subcommand, asyncio stdin, Ctrl+C logs out cleanly. |
| `tests/isolation.py` | `isolated_multi_config`. |
| `tests/capture_golden.py`, `tests/test_Golden.py` | Both versions. |
| `README.md` | Sessions, routing, scoped API, `--session`. |

## 2. Test results

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 14%]
........................................................................ [ 29%]
........................................................................ [ 43%]
........................................................................ [ 58%]
........................................................................ [ 72%]
........................................................................ [ 87%]
...............................................................          [100%]
495 passed in 61.59s (0:01:01)
```

| File | Tests |
|---|---|
| `tests/test_FixVersion.py` | 64 |
| `tests/test_OrderBook.py` | 60 |
| `tests/test_ControlApi.py` | 53 |
| `tests/test_Session.py` | 50 |
| `tests/test_MultiSession.py` | 37 (new) |
| `tests/test_Rules.py` | 36 |
| `tests/test_PriceBand.py` | 31 |
| `tests/test_Golden.py` | 27 (13 → 27: both versions) |
| `tests/test_DemoClient.py` | 27 |
| `tests/test_Pricing.py` | 26 |
| `tests/test_OrderIntegration.py` | 16 |
| `tests/test_CarryOvers.py` | 15 |
| `tests/test_Replay.py` | 14 |
| `tests/test_Codec.py` | 12 |
| `tests/test_Logging.py` | 9 |
| `tests/test_Isolation.py` | 7 |
| `tests/test_Integration.py` | 6 |
| `tests/test_SeqStore.py` | 5 |
| **Total** | **495** |

Three consecutive runs: 61.70s, 62.01s, 61.59s, all passing. The isolation
guard is green — nothing wrote to the repo's own `data/` or `logs/`.

## 3. A real run (§10.3)

`config/orderecho_multi.yaml`, live pricing, three demo clients at once.

### 3.1 Startup

```
OrderEchoFixEmulator 0.5.0 (cook5) - 3 FIX sessions
  config         : config/orderecho_multi.yaml
  evidence file  : data/evidence/20260927-085411.jsonl

  session          version  route                      port   rules  band
  ---------------- -------- -------------------------- ------ ------ ----
  agent42          FIX.4.2  ORDERECHO -> AGENT         9878   8      10%
  agent44          FIX.4.4  ORDERECHO -> AGENT         9878   8      10%
  strict-broker    FIX.4.2  STRICTBRK -> AGENT         9879   1      2%

  control api    : http://127.0.0.1:8090  (docs at /docs)
```

```
08:54:11.240 INFO  engine  Acceptor listening on 127.0.0.1:9878 for agent42, agent44 (run_id=20260927-085411)
08:54:11.241 INFO  engine  Acceptor listening on 127.0.0.1:9879 for strict-broker (run_id=20260927-085411)
08:54:11.373 INFO  engine  Startup: version=0.5.0 build=cook5 config=config/orderecho_multi.yaml sessions=[agent42=FIX.4.2@127.0.0.1:9878; agent44=FIX.4.4@127.0.0.1:9878; strict-broker=FIX.4.2@127.0.0.1:9879] evidence=data/evidence/20260927-085411.jsonl
```

### 3.2 `curl /sessions` with all three connected

```
$ curl -s http://127.0.0.1:8090/sessions
      "id": "agent42",        "state": "ACTIVE",  "fix_version": "FIX.4.2",  "port": 9878,  "peer": "127.0.0.1:50540"
      "id": "agent44",        "state": "ACTIVE",  "fix_version": "FIX.4.4",  "port": 9878,  "peer": "127.0.0.1:50541"
      "id": "strict-broker",  "state": "ACTIVE",  "fix_version": "FIX.4.2",  "port": 9879,  "peer": "127.0.0.1:50539"
```

(abridged to the identifying fields; each entry also carries next_in/out,
HeartBtInt, open orders and its own `fix_log_path`.)

### 3.3 Three clients, three answers, one engine

```
$ orderecho_DemoClient.py --config config/orderecho_multi.yaml --session agent42 order AAPL 1000 buy mkt   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT [FIX.4.2] session=agent42
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700

$ orderecho_DemoClient.py --config config/orderecho_multi.yaml --session agent44 order AAPL 1000 buy mkt   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT [FIX.4.4] session=agent44
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000
<- ExecutionReport       AAPL exec=TRADE status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700

$ orderecho_DemoClient.py --config config/orderecho_multi.yaml --session strict-broker order AAPL 1000 buy mkt   [exit 0]
Connecting to 127.0.0.1:9879 as AGENT [FIX.4.2] session=strict-broker
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  "Strict broker rejects everything"
```

Same engine, same AAPL price, three different answers: 4.2 says `exec=FILL`,
4.4 says `exec=TRADE`, and the strict broker's own rule set rejects everything.
They ran concurrently — the engine log interleaves them inside three
milliseconds:

```
08:54:19.279 INFO  agent42        ORDER O-20260927-085411-1 AAPL BUY 1000 MKT px=? (pending) rule=a-to-d-full -> NEW
08:54:19.283 INFO  strict-broker  ORDER O-20260927-085411-2 AAPL BUY 1000 MKT px=? (pending) rule=reject-all -> REJECTED
08:54:19.285 INFO  agent44        ORDER O-20260927-085411-3 AAPL BUY 1000 MKT px=? (pending) rule=a-to-d-full -> NEW
08:54:19.286 INFO  agent42        price resolved: AAPL 341.07 (live:yfinance), 0.007s after the ack
08:54:19.286 INFO  agent44        price resolved: AAPL 341.07 (live:yfinance), 0.002s after the ack
08:54:19.787 INFO  agent44        FILL  O-20260927-085411-3 1000 @ 341.07 cum=1000 leaves=0 avg=341.0700 -> FILLED
08:54:19.790 INFO  agent42        FILL  O-20260927-085411-1 1000 @ 341.07 cum=1000 leaves=0 avg=341.0700 -> FILLED
```

OrderIDs 1, 2 and 3 come from one engine-wide generator, so no two sessions can
mint the same one. The price was fetched once and served from the shared cache.

### 3.4 Filling an agent44 order by hand

```
$ curl -s http://127.0.0.1:8090/orders?status=open
{ "orders": [ { "order_id": "O-20260927-085411-4", "symbol": "ZWZZT",
                "leaves_qty": "1000", "session": "agent44" } ] }

$ curl -s -X POST http://127.0.0.1:8090/orders/O-20260927-085411-4/fill -H 'content-type: application/json' -d '{"qty": 400}'
{
  "session": "agent44",
  "order": { "cum_qty": "400", "leaves_qty": "600", "avg_px": "24.5000",
             "ord_status": "1", "price_source": "live:yfinance" },
  "sent": [{ "seq": 3, "msg_type": "8",
             "raw": "8=FIX.4.4|9=231|35=8|49=ORDERECHO|56=AGENT|34=3|52=...|37=O-20260927-085411-4|17=E-20260927-085411-7|150=F|39=1|55=ZWZZT|54=1|38=1000|40=1|32=400|31=24.50|151=600|14=400|6=24.5000|60=...|10=168|" }]
}
```

The order route is engine-wide: it found agent44 from the OrderID alone, and the
report went out in **that session's** dialect — `150=F`, no tag 20.

### 3.5 A stranger at the door

```
sent   : 8=FIX.4.2|9=68|35=A|49=NOBODY|56=WHOEVER|34=1|52=...|98=0|108=30|10=252|
got    : b''  (empty means dropped without a reply)
```

```
08:54:30.088 WARNING engine  unknown session: 8=FIX.4.2 49=NOBODY 56=WHOEVER from ('127.0.0.1', 50550)
```

### 3.6 The three FIX logs

Each session's log holds only its own traffic — the two sharing port 9878 never
appear in each other's file.

`logs/fix/agent42_20260927.log`:
```
08:54:19.100 IN   seq=1  35=A  8=FIX.4.2|...|49=AGENT|56=ORDERECHO|34=1|...|141=Y|10=070|
08:54:19.101 OUT  seq=1  35=A  8=FIX.4.2|...|49=ORDERECHO|56=AGENT|34=1|...|141=Y|10=064|
08:54:19.279 IN   seq=2  35=D  8=FIX.4.2|...|11=DEMO-...-1|21=1|55=AAPL|54=1|38=1000|40=1|...
08:54:19.280 OUT  seq=2  35=8  8=FIX.4.2|...|34=2|...|37=O-20260927-085411-1|...|20=0|150=0|39=0|...
08:54:19.790 OUT  seq=3  35=8  8=FIX.4.2|...|34=3|...|20=0|150=2|39=2|...|31=341.07|...
```

`logs/fix/agent44_20260927.log`:
```
08:54:19.181 IN   seq=1  35=A  8=FIX.4.4|...|34=1|...|141=Y|10=072|
08:54:19.285 OUT  seq=2  35=8  8=FIX.4.4|...|34=2|...|37=O-20260927-085411-3|...|150=0|39=0|...
08:54:19.787 OUT  seq=3  35=8  8=FIX.4.4|...|34=3|...|150=F|39=2|...|31=341.07|...
08:54:23.957 IN   seq=2  35=D  8=FIX.4.4|...|55=ZWZZT|...          (second connection, reset to 1)
08:54:27.992 OUT  seq=3  35=8  8=FIX.4.4|...|150=F|39=1|32=400|31=24.50|151=600|14=400|...
```

`logs/fix/strict-broker_20260927.log`:
```
08:54:19.101 IN   seq=1  35=A  8=FIX.4.2|...|49=AGENT|56=STRICTBRK|34=1|...
08:54:19.283 OUT  seq=2  35=8  8=FIX.4.2|...|49=STRICTBRK|56=AGENT|34=2|...|20=0|150=8|39=8|...
```

Each session numbers itself: all three were at `34=1` for their Logon at the
same moment. Shutdown was clean and both ports plus 8090 are free.

## 4. Decisions I made

### Capturing 4.4 first

**1. The 4.4 fixtures reuse the Cook 5 scenario driver unchanged.** It was
already version-aware, so capturing was `capture_golden.py FIX.4.4` against
untouched code. `test_the_two_versions_are_not_accidentally_identical` asserts
all ten files differ between versions — otherwise the fixtures would be proving
nothing.

**2. The capture script takes a version argument** and still refuses to
overwrite without `--force`, so capturing 4.4 could not disturb the frozen 4.2
set.

### Shape of the refactor

**3. `SessionRuntime` holds everything one session owns; `Transport` stays the
engine.** Keeping the class name meant every existing caller and test kept
working; what changed is that its per-session attributes are now properties
delegating to the one runtime. A multi-session engine raises a clear
`LookupError` if code reaches for `transport.message_store` without naming a
session, rather than silently picking one.

**4. Routing reads the Logon, then hands over the socket.** The engine frames
the first message with a scratch codec (framing has been version-agnostic since
Cook 5), decides which session it belongs to, and passes the already-decoded
message plus any leftover buffered bytes to that runtime. Nothing is re-parsed
and nothing is lost.

**5. A wrong-dialect Logon is answered by a real session, not by the engine.**
The engine routes it to the first session whose CompIDs match and tells that
session which versions were on offer; the session's own BeginString check then
produces the Logout with its own sequence number. §5 wants several versions
listed when several match, and this gets that without the engine having to
forge a message outside any session's numbering.

**6. If every session on a port is busy, the connection is refused before the
Logon.** Otherwise a silent second connection would sit in the router until it
timed out, and Cook 1's "refuses a second connection" behavior would have
changed. A connection that says nothing at all is dropped after 30 seconds
(`ROUTE_TIMEOUT`) rather than holding a task forever.

**7. Engine-wide log and evidence lines say `engine`, not a CompID pair.** A
one-session engine keeps its old `ORDERECHO-AGENT` label so nothing in the
existing output moves; with several sessions, lines that belong to no session —
acceptors binding, an unknown-CompID warning, API calls — are labelled
`engine`.

### Config

**8. `Config` keeps its old fields and gains `sessions`.** The legacy fields
now describe the *default* session, and `__post_init__` synthesizes a one-entry
`sessions` list when none is given. That is what lets every test that builds a
`Config` by hand keep working, and what makes a legacy YAML file load with its
old session id — and therefore its old file names.

**9. A config may use `session:` or `sessions:`, not both.** Silently
preferring one would make the other look ignored.

**10. Per-session scalars fall back to `defaults:` then to the built-in
default.** `orders` and `price_band` merge key by key so `price_band: {pct: 2}`
keeps the inherited mode and enforcement; `rules` replaces outright, because a
half-merged rule list is not something anyone can reason about.

**11. Validation names the session in every message** — duplicate id, duplicate
triple on a port (naming *both* sessions), a FIX port equal to the control API
port, an unknown `default_session`, a bad id character, an unknown FIX version.

**12. `--reset-seqnums` resets every session, which the real run caught.** It
was still resetting the single legacy file, so on a multi config it silently
reset a file no session used. It now walks `config.sessions`, resets each store
and archives each message store, and a test asserts all of it.

### Control API

**13. The order routes gained a `session` field; the legacy POST bodies did
not.** §7.2 makes order routes engine-wide, so saying which session owns the
order is new and useful information. The session POST helpers are shared
between the legacy and scoped routes, and the scoped URL already names the
session — so those keep their Cook 3 response shapes exactly, and the Cook 3
tests still assert them unchanged.

**14. `/health` gained `"sessions"`** (§7.4), so its two existing assertions
were updated. That is the only response shape this cook changes.

**15. Ambiguity is an error, not a guess.** With several sessions and no
`engine.default_session`, the unscoped routes answer `409 ambiguous_session`
and list the ids.

### Demo client

**16. Stdin is read through the event loop, not a worker thread.** That is what
makes Ctrl+C work: a thread blocked in `readline()` cannot be cancelled and
would hold up the exit — which is exactly how a leftover client survived its
engine at the start of Cook 5. There is still a thread fallback for stdin that
has no usable file descriptor, which is what the scripted tests use.

**17. Global options are declared twice:** once on the main parser with real
defaults, once on a parent with `argparse.SUPPRESS` that every subparser
inherits. Suppressing means an option given *before* the subcommand is not
wiped out by the same option not being given after it.

**18. `--session` and `--fix` are mutually exclusive,** because a session
already says which version it speaks; saying both invites them to disagree.

### Tests

**19. `isolated_multi_config` extends the Cook 5 helper** rather than
duplicating it, so every new multi-session test inherits the isolation guard's
protection automatically.

**20. Three of my own new tests were wrong and I fixed the tests.** A FIX-log
path asserted before anything had been written to it (the log opens lazily, so
the path was still None — the test now sends traffic first); a missing `os`
import; and the golden-fixture check requiring `SEND ` in a file that only
contains session rejects, carried over from Cook 5. No spec-derived assertion
was weakened.

**21. The `version`/`build` constants still say `0.5.0` / `cook5`.** Cook 5 §3.3
set those literal values and Cook 6 does not mention them, so changing them
would mean editing a spec-derived assertion on my own judgement. It does mean a
Cook 6 build introduces itself as `cook5` — question 1.

## 5. Questions for you

1. **Should `ORDERECHO_VERSION` / `ORDERECHO_BUILD` be bumped to `0.6.0` /
   `cook6`?** I left them because Cook 5 specified the literals and Cook 6 is
   silent, but the banner and `/health` now under-report the build. Two lines
   whenever you say.

2. **Should a session be able to run without an order book?** Today a session
   with no `rules` (possible only by hand — the loader always gives one from
   `defaults`) would business-reject every application message. If that is a
   shape you want — a pure session-layer session for testing heartbeats — it
   deserves a config switch rather than an accident.

3. **`/sessions/{id}/logout` returns the Cook 3 body** (decision 13), so a
   caller driving several sessions has to remember which id it asked about. Add
   `"session"` to the scoped responses only, or leave both shapes identical?

4. **Routing refuses a connection when every session on the port is busy
   (decision 6), before the Logon.** That keeps Cook 1's behavior, but it means
   a client whose session is free cannot get in while a *different* session on
   that port is occupied... which cannot happen today, because "all busy" is
   only checked when it is true of every session. Worth confirming you want
   refuse-early rather than always-wait-for-the-Logon.

5. **Carried over and still open:** working orders are not drop-copied on Logon
   (Cook 2), `orders.send_pending_acks` / `replace_ack_ordstatus` are
   config-only (Cook 3), `price_band.mode` is global per session rather than
   per rule (Cook 4), and 4.4 busts (`150=H`/`150=G`) are unspecified (Cook 5).
