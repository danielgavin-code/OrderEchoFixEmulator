# OrderEchoFixEmulator — Cook 4 build report

Built end to end from `SPEC_Cook4.md`. `pytest -q` passes fully: **328 passed**,
up from Cook 3's 261. No git commands were run. Cook 5 was not started.

There was a leftover `orderecho_Main.py` holding both 9878 and 8090 (PID 6557,
up 7m49s, with control-API calls in its log — you had been driving it). SIGINT,
clean shutdown, both ports free before anything else started.

One thing found while building, not in the spec: the test suite had been
sharing a single message store in the repo, because the new `msgstore_dir`
defaults to a relative path and no test config set it. Fixed, and the repo's
`data/msgstore` cleaned out — details in decision 14.

---

## 1. Files created and changed

**New (3)**

| File | What it is |
|---|---|
| `orderecho_MessageStore.py` | Outbound messages stored as sent, keyed by seq, JSONL, loaded at startup, archived on reset (§6.1). |
| `tests/test_PriceBand.py` | 31 tests — the collar, pure and over the wire. |
| `tests/test_Replay.py` | 14 tests — replay, gap-fill collapsing, restart, archiving, `gapfill` mode. |

**Changed (12)**

| File | Change |
|---|---|
| `orderecho_OrderBook.py` | `evaluate_band` and the band check on `D`/`G` (§5); market orders ack as `price_source="pending"` with `on_price` delivering the quote and firing anything due (§4); `precheck` so order facts can be reported without a session (§3.2); `_fire_due` skips pending orders; `manual_fill` raises `price_pending`. |
| `orderecho_Session.py` | `RequestPrice` and `Replay` actions; `on_price`; the §6.3 replay algorithm with admin-run collapsing; `resend_mode`; archives the store on Logon `141=Y`. |
| `orderecho_Transport.py` | Writes every outbound message to the store (never gap fills or replays); rebuilds and sends replays; background price lookups; band reference lookup bounded by `lookup_timeout_ms`; drops pending injections on disconnect (§3.1); routes third-party logging. |
| `orderecho_Pricing.py` | `quiet_third_party_logging` and `muted_stdout` (§3.3); the live lookup runs inside the mute. |
| `orderecho_Config.py` | `session.resend_mode`, `storage.msgstore_dir`, the whole `price_band` section. |
| `orderecho_Rules.py` | `price_band_pct: <number> | off` per-rule override, validated. |
| `orderecho_ControlApi.py` | §3.2 check order; `price_pending` → 409; `/rules` reports the effective band; `reset-seqnums` archives the store. |
| `orderecho_DemoClient.py` | `session` mode with its command loop and async stdin, `replace`/`resend`/`status` commands, `--no-exit`, per-order tracking. |
| `orderecho_Main.py` | `--reset-seqnums` archives the store; banner shows the store; Cook 4. |
| `config/orderecho.yaml` | `resend_mode`, `msgstore_dir`, `price_band`. |
| `README.md` | Price band, resend replay, session mode, instant acks, quieting. |
| Tests | `test_OrderBook.py` (+6, and 6 updated for §4), `test_ControlApi.py` (+7), `test_Pricing.py` (+7), `test_DemoClient.py` (+5), `test_OrderIntegration.py` (+2), `fix_TestClient.py` unchanged, plus `msgstore_dir` in every integration config. |

## 2. Test results

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
........................................                                 [100%]
328 passed in 30.38s
```

| File | Tests |
|---|---|
| `tests/test_OrderBook.py` | 60 |
| `tests/test_ControlApi.py` | 53 |
| `tests/test_Session.py` | 50 |
| `tests/test_Rules.py` | 36 |
| `tests/test_PriceBand.py` | 31 (new) |
| `tests/test_Pricing.py` | 26 |
| `tests/test_DemoClient.py` | 18 |
| `tests/test_Replay.py` | 14 (new) |
| `tests/test_Codec.py` | 12 |
| `tests/test_Logging.py` | 9 |
| `tests/test_OrderIntegration.py` | 8 |
| `tests/test_Integration.py` | 6 |
| `tests/test_SeqStore.py` | 5 |
| **Total** | **328** |

Three consecutive runs: 30.41s, 30.72s, 30.13s, all passing. No test touches
the network — live pricing is exercised with a fake `yfinance` in `sys.modules`,
and every socket test binds a random free port on localhost.

## 3. A real run (§11.3)

Engine on the shipped config (live pricing, band on at ±10%, control API up),
driven by the demo client in `session` mode.

### 3.1 Demo client

```
$ .venv/bin/python orderecho_DemoClient.py session          [exit 0]
Connecting to 127.0.0.1:9878 as AGENT
<- Logon                 HeartBtInt=30
Connected. Inbound messages print as they arrive.
commands:
  order <SYM> <QTY> <buy|sell> <mkt|lmt> [PX]   send a NewOrderSingle
  cancel <ClOrdID>                              cancel one of your orders
  replace <ClOrdID> <QTY> [PX]                  replace qty (and price)
  resend <begin> [end]                          send a ResendRequest
  status                                        orders this client has sent
  help                                          this text
  quit                                          log out and exit
> order AAPL 1000 buy mkt
-> NewOrderSingle      AAPL 1000 BUY MKT  11=DEMO-1790479725227
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790479725227 37=O-20260927-032840-1
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700  11=DEMO-1790479725227 37=O-20260927-032840-1
> order AAPL 100 buy lmt 500.00
-> NewOrderSingle      AAPL 100 BUY LMT 500.00  11=DEMO-1790479733839
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1790479733839 37=O-20260927-032840-2  103=3  "Limit 500.00 outside 10% band of ref 341.07 (live:yfinance)"
> order AAPL 100 buy lmt 230.00
-> NewOrderSingle      AAPL 100 BUY LMT 230.00  11=DEMO-1790479736841
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1790479736841 37=O-20260927-032840-3
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=100@230.00  cum=100 leaves=0 avg=230.0000  11=DEMO-1790479736841 37=O-20260927-032840-3
> resend 1 0
-> ResendRequest       7=1 16=0
<- SequenceReset
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790479725227 37=O-20260927-032840-1
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700  11=DEMO-1790479725227 37=O-20260927-032840-1
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1790479733839 37=O-20260927-032840-2  103=3  "Limit 500.00 outside 10% band of ref 341.07 (live:yfinance)"
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1790479736841 37=O-20260927-032840-3
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=100@230.00  cum=100 leaves=0 avg=230.0000  11=DEMO-1790479736841 37=O-20260927-032840-3
> quit
<- Logout                "Logout acknowledged"
```

(The `> ` prompts are interleaved with arriving messages in the real terminal;
they are lined up here for reading.)

### 3.2 Ack timing versus price resolution

The point of §4, visible in two timestamps 1.1 seconds apart:

```
20260927-03:28:45.228 IN   seq=2  35=D   (the market order arrives)
20260927-03:28:45.230 OUT  seq=2  35=8   (acked 2 ms later, 150=0)
...
20260927-03:28:46.362 OUT  seq=3  35=8   (filled, 31=341.07)
```

```
20260927-03:28:45.230 INFO  price pending: AAPL market order acked before its quote
20260927-03:28:45.231 INFO  ORDER O-20260927-032840-1 AAPL BUY 1000 MKT px=? (pending) rule=a-to-d-full -> NEW
20260927-03:28:46.362 INFO  price resolved: AAPL 341.07 (live:yfinance), 1.132s after the ack
20260927-03:28:46.362 INFO  FILL  O-20260927-032840-1 1000 @ 341.07 cum=1000 leaves=0 avg=341.0700 -> FILLED
```

Under Cook 3 that ack would have landed at `45:46.362`. The fill still waits for
the real price — it just no longer holds the acknowledgement hostage.

### 3.3 Band decisions

```
20260927-03:28:54.144 INFO  BAND  DEMO-1790479733839 BUY LMT 500.00 ref=341.07 (live:yfinance) band=±10% -> REJECT
20260927-03:28:54.145 INFO  ORDER O-20260927-032840-2 AAPL BUY 100 LMT px=500.00 (limit) rule=a-to-d-full -> REJECTED
20260927-03:28:56.880 INFO  BAND  DEMO-1790479736841 BUY LMT 230.00 ref=341.07 (live:yfinance) band=±10% -> PASS
```

The reject on the wire, with `103=3` and the §5 text:

```
20260927-03:28:54.144 OUT  seq=4    35=8  8=FIX.4.2|9=303|35=8|49=ORDERECHO|56=AGENT|34=4|52=20260927-03:28:54.144|37=O-20260927-032840-2|11=DEMO-1790479733839|17=E-20260927-032840-3|20=0|150=8|39=8|55=AAPL|54=1|38=100|40=2|44=500.00|32=0|31=0.00|151=0|14=0|6=0.0000|60=20260927-03:28:54.143|103=3|58=Limit 500.00 outside 10% band of ref 341.07 (live:yfinance)|10=021|
```

### 3.4 Replay

`resend 1 0` over seqs 1..6: seq 1 is the Logon reply, so it collapses into one
gap fill; the five ExecutionReports come back as themselves.

```
20260927-03:28:59.843 IN   seq=5    35=2  8=FIX.4.2|9=66|35=2|49=AGENT|56=ORDERECHO|34=5|52=20260927-03:28:59.843|7=1|16=0|10=119|
20260927-03:29:00.008 OUT  seq=1    35=4  8=FIX.4.2|9=99|35=4|49=ORDERECHO|56=AGENT|34=1|52=20260927-03:29:00.007|43=Y|122=20260927-03:29:00.007|123=Y|36=2|10=242|
20260927-03:29:00.008 OUT  seq=2    35=8  8=FIX.4.2|9=259|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-03:29:00.008|43=Y|122=20260927-03:28:45.230|37=O-20260927-032840-1|...|10=075|  # replay of seq 2
20260927-03:29:00.009 OUT  seq=3    35=8  8=FIX.4.2|9=266|35=8|...|34=3|52=20260927-03:29:00.009|43=Y|122=20260927-03:28:46.362|...|31=341.07|...|10=196|  # replay of seq 3
20260927-03:29:00.010 OUT  seq=4    35=8  8=FIX.4.2|9=334|35=8|...|34=4|52=20260927-03:29:00.009|43=Y|122=20260927-03:28:54.144|...|103=3|58=Limit 500.00 outside 10% band of ref 341.07 (live:yfinance)|10=028|  # replay of seq 4
20260927-03:29:00.011 OUT  seq=5    35=8  8=FIX.4.2|9=267|35=8|...|34=5|52=20260927-03:29:00.010|43=Y|122=20260927-03:28:56.880|...|10=207|  # replay of seq 5
20260927-03:29:00.011 OUT  seq=6    35=8  8=FIX.4.2|9=273|35=8|...|34=6|52=20260927-03:29:00.011|43=Y|122=20260927-03:28:57.390|...|31=230.00|...|10=251|  # replay of seq 6
```

```
20260927-03:29:00.011 INFO  RESEND 1..6 -> replayed 5, gap-filled 1 run(s)
```

Each replay keeps its original `34`, carries `43=Y` with `122` set to the
message's *original* SendingTime and a fresh `52`, and has its BodyLength and
CheckSum recomputed (note 9 growing from 228 to 259 as the two fields go in).
Nothing consumed a new sequence number: the Logout that followed went out on 7,
the number the session was already up to.

### 3.5 Console (banner and shutdown)

```
Sequence numbers reset to 1/1 (data/seqnums/ORDERECHO-AGENT.json)
Outbound message store archived to data/msgstore/ORDERECHO-AGENT.jsonl.20260927-032840
OrderEchoFixEmulator (Cook 4) - FIX 4.2 acceptor
  listening      : 127.0.0.1:9878
  comp ids       : sender=ORDERECHO target=AGENT
  config         : config/orderecho.yaml
  evidence file  : data/evidence/20260927-032840.jsonl
  fix log        : logs/fix
  engine log     : logs/engine
  seqnum file    : data/seqnums/ORDERECHO-AGENT.json
  message store  : data/msgstore/ORDERECHO-AGENT.jsonl (0 stored)
  control api    : http://127.0.0.1:8090  (docs at /docs)
  Ctrl+C to shut down.
^C
Shutting down...
20260927-03:29:05.057 INFO    session  ORDERECHO-AGENT  Shutdown complete
[engine exit 0]
```

Engine and client are both stopped; 9878 and 8090 are free.

## 4. How yfinance was silenced

Two mechanisms, because yfinance is noisy in two different ways.

**The line you actually saw is a log record, not a print.** In
`yfinance/data.py` the cookie failure goes through
`utils.get_yf_logger().warning(...)`, i.e. `logging.getLogger("yfinance")`.
That logger has no handler of its own, so Python's `lastResort` handler prints
it to **stderr** at WARNING. So the fix is a logging fix:
`quiet_third_party_logging()` attaches a small handler that forwards each record
into our engine log at DEBUG, sets the logger's level to DEBUG so nothing is
lost on the way, and — the part that actually silences it — sets
`propagate = False`, so the record never reaches the root logger or
`lastResort`. It is applied to `yfinance` and `peewee` (its cache layer) when
the transport starts.

Effect in the real run above: **zero** occurrences of "Cookie fetch" on the
terminal. Routed to DEBUG, the records are dropped by the shipped
`engine_level: INFO` and appear in the file when the level is DEBUG.

**For anything it prints,** a thread-scoped stdout guard. `contextlib.redirect_stdout`
replaces `sys.stdout` for the whole process, which would swallow the FIX-log
console echo any other task produced during the one-to-eight seconds a lookup
can take — exactly what §3.3 warns against. Instead `muted_stdout()` installs a
small proxy whose `write` checks a `threading.local` flag: the calling thread's
output is captured and forwarded to the engine log at DEBUG, and **every other
thread's output passes straight through** to the real stream. The worker thread
wraps its `yfinance.Ticker(...)` call in it; the proxy is installed under a lock
with a depth counter and removed when the last muted call leaves, so
`sys.stdout` is exactly what it was before.

`test_muting_one_thread_does_not_silence_the_others` pins the property that
matters: one thread muted, the other still printing, both asserted.

## 5. Decisions I made

### Architecture

**1. `RequestPrice` as a new app action.** §4 puts the lookup in the transport
and the result back through `session.on_price(order_id, quote)`, but the
transport has no way to learn the order id the pure core just minted. Rather
than have it scrape tag 37 out of the ack, the order book returns
`RequestPrice(order_id, symbol)` alongside the ack and the transport starts the
lookup when it sees it — the same shape as every other action.

**2. `Replay` as a new session action.** The session decides *what* to replay
(it owns the resend logic and the store); the transport rebuilds the bytes,
because encoding has always been its job. The session does read tag 52 out of
the stored string with a small helper, which is a string operation, not a decode.

**3. `on_price` records the price in any state, but only fires fills while
ACTIVE.** Gating the whole callback would leave an order pending forever if the
session dropped mid-lookup; firing regardless would write ERs to a closed
socket. So the price always lands and the due events stay scheduled, going out
on the first tick after the next Logon — the same rule Cook 2 §4 set for
deferred events.

**4. `market_price` now carries whatever quote the message needs.** For a
market `D` it is always `None` (the price comes later); for a limit `D`/`G` it
is the band reference. The parameter name is Cook 2's and I left it alone rather
than churn the signature, but it is documented at both ends.

**5. Market orders always start pending, even on a cache hit.** §4 says the
order has `price_source = "pending"` until the quote arrives, so I did not add
a synchronous cache peek that would sometimes skip that state. The round trip
through `to_thread` is sub-millisecond when the cache is warm.

### Price band

**6. The rule is matched before the band check, but its behavior is applied
after.** §5 says the check runs "before rule matching", yet §5 also lets a rule
override the width with `price_band_pct` — which cannot be known without
matching. So the order is: match (to learn the override and record "rule
matched"), check the band, and only then let the rule's behavior ack or
schedule anything. A band reject therefore beats a `reject` rule, and nothing is
ever acked or scheduled before the collar has had its say.

**7. The transport fetches a band reference whenever the band is enabled,**
without first working out whether the matching rule says `off`. Rule matching
belongs to the pure core, and the wasted work is one cache lookup.

**8. `BAND` log line format** follows §5's example exactly, in the same 5-wide
label column as `ORDER`/`FILL`/`API`/`RESEND`.

**9. Band evidence is one record plus one log line.** `price band
pass|reject|skipped` carries a short description for the evidence file, and a
separate `price band` event carries the finished `BAND ...` line which the
transport prints verbatim. The first is logged at DEBUG so the engine log has
exactly one line per decision — I had them both at INFO at first and the real
run showed the duplicate immediately.

### Replay

**10. A missing sequence number is treated as admin** — gap-filled, not an
error. §6.3 lists "any seq missing from the store" alongside the admin types,
which is what makes an injected `seq-gap` and an archived range behave the same.

**11. Gap fills and replays are never written to the store,** because both reuse
a sequence number that already belongs to an earlier message; storing them would
overwrite the original with its own substitute. `test_gap_fills_and_replays_are_never_stored`
pins it.

**12. `duplicate-last` falls back to the store** when the current connection has
not sent anything yet, so it still works right after a reconnect (§6.4).

**13. The archive suffix is `.jsonl.<YYYYMMDD-HHMMSS>`,** with `-2`, `-3` … if
two resets land in the same second. Archives are never deleted.

### Tests and hygiene

**14. Fixed test isolation that this cook exposed.** `storage.msgstore_dir`
has a default (§9 gives it one), and no existing test config set it — so every
integration test wrote to the repo's own `data/msgstore` and read each other's
messages. The first replay test failed with a gap fill on the wrong sequence
number because of it. All four integration configs now point at `tmp_path`, and
I deleted the polluted store (gitignored runtime data). Worth knowing: the same
trap exists for any future `storage` key that gets a default.

**15. Fixed a test-side sequencing bug in two reconnect tests.** A fresh
`FixTestClient` starts at sequence 1, but the emulator persists sequence numbers
across connections, so a reconnecting client has to continue from `next_in`. The
emulator was right to log the client out; the tests now read the expected number
from the seq store first. Not a weakened assertion — the tests were asking for
the wrong thing.

**16. Six Cook 2 order-book tests were updated, all §4-mandated.** They asserted
that a market order is priced at ack time, which §4 replaces. They now assert
the new contract end to end: acked as pending, `RequestPrice` returned, priced by
`on_price`, then filled. `test_market_order_without_a_quote_is_rejected` became
`test_market_order_without_a_quote_is_acked_as_pending`, which is the opposite
assertion because the spec inverted the behavior.

**17. Two Cook 3 response contracts were left alone.** `/health` still reports
`"version": "cook3"` and `/session/reset-seqnums` still returns exactly
`{"next_out", "next_in"}`. Cook 3 §8 and §6 fix both payloads and Cook 4 never
amends them, so changing either would have meant editing a spec-derived
assertion on my own judgement. The store *is* archived on reset — that is
§6.1 — it is just visible in the engine log and evidence rather than in the
response body. Both are question 1 below.

**18. `--no-exit` reuses the session command loop** rather than a bare sleep, so
everything you can type in `session` mode works after a one-shot order settles.

**19. Scripted-stdin test harness for session mode.** §10.23 wants `order`,
`cancel`, `quit` scripted, but the ClOrdID is invented at runtime, so a fixed
script cannot contain it. The harness lets a script entry be a callable that
reads the output produced so far — it waits for `11=DEMO-…` to appear and
returns `cancel DEMO-…`.

## 6. Questions for you

1. **Should `/health` say `cook4`, and should `reset-seqnums` report what it
   archived?** I left both exactly as Cook 3 specifies (decision 17) because
   Cook 4 does not mention them, but a `version` field that names the previous
   build is arguably worse than the inconsistency I avoided. One word from you
   and both become one-line changes.

2. **The band is skipped on the first limit order of a cold run, and I think
   that is worth a second look.** With live pricing, a limit order that arrives
   before anything has warmed that symbol gets no reference inside
   `lookup_timeout_ms: 1500`, falls back to the static table, and — with
   `enforce_on_fallback: false` — skips the check entirely. My first run showed
   exactly this: a 500.00 buy on AAPL sailed through at `ref=227.50
   (static:fallback) -> SKIPPED`. That is precisely what §5 asks for, and the
   second run (cache warm) rejected it correctly. But "the collar is off until
   someone else warms the symbol" is a real hole. Options: warm the symbols your
   rules name at startup, or let `lookup_timeout_ms` be generous on the first
   lookup per symbol only.

3. **Should a replayed message be re-stored under a *new* sequence number**
   if the counterparty is going to count it? Today replay reuses the original
   `34` and consumes nothing, which is correct FIX — I mention it only because
   it means the store never grows during a resend, so a client that resends
   repeatedly gets identical bytes each time. That seems right, but it is the
   kind of thing worth confirming before an agent depends on it.

4. **`price_band.mode` is global.** Rules can change the *width* but not the
   mode. Should `aggressive`/`both` be per-rule too?

5. **Carried over and still open:** working orders are not drop-copied on Logon
   (Cook 2), and `orders.send_pending_acks` / `replace_ack_ordstatus` are still
   config-only rather than flippable through the control API (Cook 3).
