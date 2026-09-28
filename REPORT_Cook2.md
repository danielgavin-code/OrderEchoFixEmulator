# OrderEchoFixEmulator — Cook 2 build report

Built end to end from `SPEC_Cook2.md`. `pytest -q` passes fully: **206 passed**,
up from Cook 1's 64. No git commands were run. Cook 3 was not started.

One thing worth reading before the detail: the §13.3 live-pricing run **fell
back to static pricing**, because yfinance's first call takes about 8 seconds on
this machine and §8 specifies `timeout_sec: 3`. The fallback behaved exactly as
designed, and a follow-up probe confirms live pricing works when given longer.
Details and the fallback line are in §3 below; it is question 1 in §5.

---

## 1. Files created and changed

**New (6)**

| File | What it is |
|---|---|
| `orderecho_OrderBook.py` | The pure order state machine (§5, §6): D/F/G → 35=8 / 35=9, fills, cancels, replaces, scheduling. |
| `orderecho_Rules.py` | Pure rule engine (§7): matching, strict config validation, percent resolution. |
| `orderecho_Pricing.py` | `PriceQuote`, static and yfinance sources, caching, fallback, the async `resolve_quote` helper (§8). |
| `orderecho_DemoClient.py` | The `order` / `cancel-demo` CLI (§10). |
| `tests/test_OrderBook.py` | 54 tests — all 20 §11 cases plus the evidence contract. |
| `tests/test_Rules.py` | 36 tests — matching precedence, every §7.2 load failure, percent resolution. |
| `tests/test_Pricing.py` | 19 tests — static, live (monkeypatched), cache, every failure mode, timeout. |
| `tests/test_OrderIntegration.py` | 6 tests — real sockets, static pricing, 50 ms delays. |
| `tests/test_DemoClient.py` | 9 tests — the CLI against a live acceptor on a random port. |

**Changed (9)**

| File | Change |
|---|---|
| `orderecho_Session.py` | §4 app hook (`app=`, `market_price=`, `AppSend`/`SessionReject`/`Evidence(order=)`, app timer only while ACTIVE, deferral evidence); §3.1 bounded ResendRequest; §3.2 counterparty logout goes straight to DISCONNECTED. |
| `orderecho_Transport.py` | 100 ms timer tick; builds the order book and price source; resolves a quote for inbound market orders off the event loop; §9 order evidence and lifecycle log lines. |
| `orderecho_Evidence.py` | §3.3 `fields` as ordered pairs; §9 `order` key. |
| `orderecho_Codec.py` | `FixMsg.fields()` returns ordered `[tag, value]` pairs instead of a dict. |
| `orderecho_Config.py` | `orders`, `rules` and `pricing` sections, with validation. |
| `orderecho_Main.py` | Banner says Cook 2. |
| `config/orderecho.yaml` | The §7.1 and §8 blocks, verbatim from the spec. |
| `requirements.txt` | Added `yfinance`. |
| `README.md` | §12: rules table, pricing modes, demo client, updated evidence schema. |
| `tests/test_Codec.py`, `tests/test_Integration.py`, `tests/test_Session.py` | Updated for §3.1/§3.2/§3.3, plus the §11 session additions. |

Cook 1 files untouched: `orderecho_Clock.py`, `orderecho_SeqStore.py`,
`orderecho_Logging.py`, `tests/fix_TestClient.py`, `tests/test_SeqStore.py`,
`tests/test_Logging.py`, `conftest.py`, `pytest.ini`.

## 2. Test results

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 34%]
........................................................................ [ 69%]
..............................................................           [100%]
206 passed in 11.52s
```

| File | Tests |
|---|---|
| `tests/test_OrderBook.py` | 54 |
| `tests/test_Session.py` | 50 (32 from Cook 1 + 18 new) |
| `tests/test_Rules.py` | 36 |
| `tests/test_Pricing.py` | 19 |
| `tests/test_Codec.py` | 12 |
| `tests/test_DemoClient.py` | 9 |
| `tests/test_Logging.py` | 9 |
| `tests/test_Integration.py` | 6 |
| `tests/test_OrderIntegration.py` | 6 |
| `tests/test_SeqStore.py` | 5 |
| **Total** | **206** |

No test touches the network: live pricing is exercised with a fake `yfinance`
module injected into `sys.modules`, and every socket test binds a random free
port on localhost. The suite was run repeatedly while building, and again with
the real `yfinance` installed, with no change in results.

The `pytest -v` listing follows at the end of this report (§6), to keep it out
of the way.

## 3. A real run (§13.3)

`orderecho_Main.py` with the shipped config (`pricing.mode: live`), then the two
demo client commands, then Ctrl+C.

### 3.1 What happened, in one paragraph

The AAPL market order arrived, the live price lookup **timed out after 3 s** and
fell back to the static table (227.50, `static:fallback`), and the order was
acked. The demo client's default `--wait 3` had already been consumed by that
lookup, so it logged out before the 500 ms fill was due — and the fill therefore
fired during the *second* client's session, which is exactly the §4 behavior:
scheduled events are deferred, not lost, and the order book is per engine run,
not per connection. The `scheduled order events deferred until logon` warning is
in the engine log below, and the deferred AAPL fill appears in the second
client's output. The EFG limit order priced itself off tag 44 and took its 40%
and 10% partials on schedule.

### 3.2 Demo client output

```
$ .venv/bin/python orderecho_DemoClient.py order AAPL 1000 buy mkt      [exit 0]
Connecting to 127.0.0.1:9878 as AGENT
<- Logon                 HeartBtInt=30
-> NewOrderSingle      AAPL 1000 BUY MKT  11=DEMO-1790475329102
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790475329102 37=O-20260927-021526-1
<- Logout                "Logout acknowledged"
```

```
$ .venv/bin/python orderecho_DemoClient.py order EFG 1000 buy lmt 10.00 --wait 3   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT
<- Logon                 HeartBtInt=30
-> NewOrderSingle      EFG 1000 BUY LMT 10.00  11=DEMO-1790475332825
<- ExecutionReport       EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790475332825 37=O-20260927-021526-2
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@227.50  cum=1000 leaves=0 avg=227.5000  11=DEMO-1790475329102 37=O-20260927-021526-1
<- ExecutionReport       EFG exec=PARTIAL_FILL status=PARTIALLY_FILLED  last=400@10.00  cum=400 leaves=600 avg=10.0000  11=DEMO-1790475332825 37=O-20260927-021526-2
<- ExecutionReport       EFG exec=PARTIAL_FILL status=PARTIALLY_FILLED  last=100@10.00  cum=500 leaves=500 avg=10.0000  11=DEMO-1790475332825 37=O-20260927-021526-2
<- Logout                "Logout acknowledged"
```

The AAPL fill in the second block is the deferred one from the first session.

### 3.3 yfinance: it failed, and here is the fallback line

```
20260927-02:15:32.103 WARNING session  ORDERECHO-AGENT  Price lookup for AAPL failed (timed out after 3.0s); falling back to 227.50 [static:fallback]
20260927-02:15:32.128 INFO    session  ORDERECHO-AGENT  price resolved: AAPL 227.50 (static:fallback)
```

Diagnosing it rather than guessing: a direct call outside the emulator succeeds,
but slowly, because the first call negotiates a cookie/crumb with Yahoo.

```
$ .venv/bin/python -c "import yfinance, time; t=time.time(); print(yfinance.Ticker('AAPL').fast_info['last_price'], f'in {time.time()-t:.2f}s')"
Cookie fetch from fc.yahoo.com failed (ConnectionError), continuing without it
last_price=341.07000732421875 in 8.09s
```

So the source works; 3 s is simply not enough for the first lookup. I did **not**
change the shipped `timeout_sec`, because §8 specifies `3` (see decision 20 and
question 1). Re-running the same scenario with a throwaway config at
`timeout_sec: 15` — the shipped config untouched — gives live pricing:

```
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700
20260927-02:16:38.443 INFO    session  ORDERECHO-AGENT  price resolved: AAPL 341.07 (live:yfinance)
20260927-02:16:38.445 INFO    session  ORDERECHO-AGENT  ORDER  O-20260927-021631-1 AAPL BUY 1000 MKT px=341.07 (live:yfinance) rule=a-to-d-full -> NEW
20260927-02:16:38.951 INFO    session  ORDERECHO-AGENT  FILL   O-20260927-021631-1 1000 @ 341.07 cum=1000 leaves=0 avg=341.0700 -> FILLED
```

### 3.4 Emulator console (startup and shutdown)

```
Sequence numbers reset to 1/1 (data/seqnums/ORDERECHO-AGENT.json)
20260927-02:15:26.730 INFO    session  ORDERECHO-AGENT  Acceptor listening on 127.0.0.1:9878 (sender=ORDERECHO target=AGENT run_id=20260927-021526)
OrderEchoFixEmulator (Cook 2) - FIX 4.2 acceptor
  listening      : 127.0.0.1:9878
  comp ids       : sender=ORDERECHO target=AGENT
  config         : config/orderecho.yaml
  evidence file  : data/evidence/20260927-021526.jsonl
  fix log        : logs/fix
  engine log     : logs/engine
  seqnum file    : data/seqnums/ORDERECHO-AGENT.json
  Ctrl+C to shut down.
^C
Shutting down...
20260927-02:15:36.445 INFO    session  ORDERECHO-AGENT  Acceptor stopped
20260927-02:15:36.445 INFO    session  ORDERECHO-AGENT  Shutdown complete
[engine exit 0]
```

(`^C` marks where a real SIGINT was delivered; the build ran non-interactively.)

### 3.5 FIX log

```
20260927-02:15:29.099 IN   seq=1    35=A  8=FIX.4.2|9=75|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260927-02:15:29.060|98=0|108=30|141=Y|10=060|
20260927-02:15:29.102 OUT  seq=1    35=A  8=FIX.4.2|9=75|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260927-02:15:29.102|98=0|108=30|141=Y|10=057|
20260927-02:15:29.103 IN   seq=2    35=D  8=FIX.4.2|9=135|35=D|49=AGENT|56=ORDERECHO|34=2|52=20260927-02:15:29.102|11=DEMO-1790475329102|21=1|55=AAPL|54=1|38=1000|40=1|60=20260927-02:15:29.102|10=058|
20260927-02:15:32.128 OUT  seq=2    35=8  8=FIX.4.2|9=228|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-02:15:32.128|37=O-20260927-021526-1|11=DEMO-1790475329102|17=E-20260927-021526-1|20=0|150=0|39=0|55=AAPL|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20260927-02:15:32.124|10=037|
20260927-02:15:32.129 IN   seq=3    35=5  8=FIX.4.2|9=77|35=5|49=AGENT|56=ORDERECHO|34=3|52=20260927-02:15:32.103|58=Demo client done|10=123|
20260927-02:15:32.132 OUT  seq=3    35=5  8=FIX.4.2|9=80|35=5|49=ORDERECHO|56=AGENT|34=3|52=20260927-02:15:32.132|58=Logout acknowledged|10=015|
20260927-02:15:32.822 IN   seq=1    35=A  8=FIX.4.2|9=75|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260927-02:15:32.810|98=0|108=30|141=Y|10=057|
20260927-02:15:32.824 OUT  seq=1    35=A  8=FIX.4.2|9=75|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260927-02:15:32.824|98=0|108=30|141=Y|10=062|
20260927-02:15:32.825 IN   seq=2    35=D  8=FIX.4.2|9=143|35=D|49=AGENT|56=ORDERECHO|34=2|52=20260927-02:15:32.825|11=DEMO-1790475332825|21=1|55=EFG|54=1|38=1000|40=2|60=20260927-02:15:32.825|44=10.00|10=149|
20260927-02:15:32.828 OUT  seq=2    35=8  8=FIX.4.2|9=236|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-02:15:32.827|37=O-20260927-021526-2|11=DEMO-1790475332825|17=E-20260927-021526-2|20=0|150=0|39=0|55=EFG|54=1|38=1000|40=2|44=10.00|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20260927-02:15:32.826|10=133|
20260927-02:15:32.925 OUT  seq=3    35=8  8=FIX.4.2|9=235|35=8|49=ORDERECHO|56=AGENT|34=3|52=20260927-02:15:32.925|37=O-20260927-021526-1|11=DEMO-1790475329102|17=E-20260927-021526-3|20=0|150=2|39=2|55=AAPL|54=1|38=1000|40=1|32=1000|31=227.50|151=0|14=1000|6=227.5000|60=20260927-02:15:32.924|10=168|
20260927-02:15:33.367 OUT  seq=4    35=8  8=FIX.4.2|9=241|35=8|49=ORDERECHO|56=AGENT|34=4|52=20260927-02:15:33.367|37=O-20260927-021526-2|11=DEMO-1790475332825|17=E-20260927-021526-4|20=0|150=1|39=1|55=EFG|54=1|38=1000|40=2|44=10.00|32=400|31=10.00|151=600|14=400|6=10.0000|60=20260927-02:15:33.366|10=134|
20260927-02:15:33.873 OUT  seq=5    35=8  8=FIX.4.2|9=241|35=8|49=ORDERECHO|56=AGENT|34=5|52=20260927-02:15:33.872|37=O-20260927-021526-2|11=DEMO-1790475332825|17=E-20260927-021526-5|20=0|150=1|39=1|55=EFG|54=1|38=1000|40=2|44=10.00|32=100|31=10.00|151=500|14=500|6=10.0000|60=20260927-02:15:33.871|10=135|
20260927-02:15:35.831 IN   seq=3    35=5  8=FIX.4.2|9=77|35=5|49=AGENT|56=ORDERECHO|34=3|52=20260927-02:15:35.825|58=Demo client done|10=137|
20260927-02:15:35.927 OUT  seq=6    35=5  8=FIX.4.2|9=80|35=5|49=ORDERECHO|56=AGENT|34=6|52=20260927-02:15:35.927|58=Logout acknowledged|10=033|
```

### 3.6 Engine log

```
20260927-02:15:26.730 INFO    session  ORDERECHO-AGENT  Acceptor listening on 127.0.0.1:9878 (sender=ORDERECHO target=AGENT run_id=20260927-021526)
20260927-02:15:26.730 INFO    session  ORDERECHO-AGENT  Startup: config=config/orderecho.yaml host=127.0.0.1 port=9878 heartbeat_grace_pct=20.0 logout_timeout_sec=10.0 evidence=data/evidence/20260927-021526.jsonl
20260927-02:15:29.060 INFO    session  ORDERECHO-AGENT  Connection accepted from ('127.0.0.1', 49467)
20260927-02:15:29.098 INFO    session  ORDERECHO-AGENT  connected: awaiting Logon
20260927-02:15:29.098 INFO    session  ORDERECHO-AGENT  State DISCONNECTED -> AWAITING_LOGON
20260927-02:15:29.101 INFO    session  ORDERECHO-AGENT  seqnums reset: Logon carried 141=Y
20260927-02:15:29.102 INFO    session  ORDERECHO-AGENT  logon accepted: HeartBtInt=30, next_in=2 next_out=2
20260927-02:15:29.102 INFO    session  ORDERECHO-AGENT  State AWAITING_LOGON -> ACTIVE
20260927-02:15:32.103 WARNING session  ORDERECHO-AGENT  Price lookup for AAPL failed (timed out after 3.0s); falling back to 227.50 [static:fallback]
20260927-02:15:32.127 INFO    session  ORDERECHO-AGENT  rule matched: AAPL matched rule 'a-to-d-full' (full_fill)
20260927-02:15:32.128 INFO    session  ORDERECHO-AGENT  price resolved: AAPL 227.50 (static:fallback)
20260927-02:15:32.128 INFO    session  ORDERECHO-AGENT  ORDER  O-20260927-021526-1 AAPL BUY 1000 MKT px=227.50 (static:fallback) rule=a-to-d-full -> NEW
20260927-02:15:32.128 INFO    session  ORDERECHO-AGENT  order events scheduled: 1 event(s) for O-20260927-021526-1 every 500ms
20260927-02:15:32.132 INFO    session  ORDERECHO-AGENT  Disconnecting: Logout requested by counterparty
20260927-02:15:32.132 INFO    session  ORDERECHO-AGENT  State ACTIVE -> DISCONNECTED
20260927-02:15:32.133 INFO    session  ORDERECHO-AGENT  disconnected: next_out=4 next_in=4
20260927-02:15:32.134 WARNING session  ORDERECHO-AGENT  scheduled order events deferred until logon: order events remain due and will fire after the next Logon
20260927-02:15:32.134 INFO    session  ORDERECHO-AGENT  Connection closed, peer=('127.0.0.1', 49467)
20260927-02:15:32.811 INFO    session  ORDERECHO-AGENT  Connection accepted from ('127.0.0.1', 49468)
20260927-02:15:32.822 INFO    session  ORDERECHO-AGENT  connected: awaiting Logon
20260927-02:15:32.822 INFO    session  ORDERECHO-AGENT  State DISCONNECTED -> AWAITING_LOGON
20260927-02:15:32.824 INFO    session  ORDERECHO-AGENT  seqnums reset: Logon carried 141=Y
20260927-02:15:32.825 INFO    session  ORDERECHO-AGENT  logon accepted: HeartBtInt=30, next_in=2 next_out=2
20260927-02:15:32.825 INFO    session  ORDERECHO-AGENT  State AWAITING_LOGON -> ACTIVE
20260927-02:15:32.827 INFO    session  ORDERECHO-AGENT  rule matched: EFG matched rule 'e-to-g-partial' (partial_fill)
20260927-02:15:32.827 INFO    session  ORDERECHO-AGENT  price resolved: EFG 10.00 (limit)
20260927-02:15:32.828 INFO    session  ORDERECHO-AGENT  ORDER  O-20260927-021526-2 EFG BUY 1000 LMT px=10.00 (limit) rule=e-to-g-partial -> NEW
20260927-02:15:32.828 INFO    session  ORDERECHO-AGENT  order events scheduled: 2 event(s) for O-20260927-021526-2 every 500ms
20260927-02:15:32.926 INFO    session  ORDERECHO-AGENT  FILL   O-20260927-021526-1 1000 @ 227.50 cum=1000 leaves=0 avg=227.5000 -> FILLED
20260927-02:15:33.368 INFO    session  ORDERECHO-AGENT  FILL   O-20260927-021526-2 400 @ 10.00 cum=400 leaves=600 avg=10.0000 -> PARTIALLY_FILLED
20260927-02:15:33.873 INFO    session  ORDERECHO-AGENT  FILL   O-20260927-021526-2 100 @ 10.00 cum=500 leaves=500 avg=10.0000 -> PARTIALLY_FILLED
20260927-02:15:35.927 INFO    session  ORDERECHO-AGENT  Disconnecting: Logout requested by counterparty
20260927-02:15:35.928 INFO    session  ORDERECHO-AGENT  State ACTIVE -> DISCONNECTED
20260927-02:15:35.929 INFO    session  ORDERECHO-AGENT  disconnected: next_out=7 next_in=4
20260927-02:15:35.929 INFO    session  ORDERECHO-AGENT  Connection closed, peer=('127.0.0.1', 49468)
20260927-02:15:36.445 INFO    session  ORDERECHO-AGENT  Acceptor stopped
20260927-02:15:36.445 INFO    session  ORDERECHO-AGENT  Shutdown complete
```

The engine was stopped afterwards; nothing is left listening on 9878.

---

# 4. Decisions I made

Every ambiguity I hit and what I chose. Where §3.4 answered a Cook 1 question I
took the answer as given: §3.1 is implemented, and decisions 12 and 5 from Cook 1
stay as they were, so **no Cook 1 questions remain open**.

## Architecture and plumbing

**1. `AppSend` and `SessionReject` live in `orderecho_Session.py`.** §4 names
them but not their home. The session is what interprets them, so they belong
next to `Send`; `orderecho_OrderBook` imports them, which is a one-way
dependency with no cycle.

**2. `Evidence` gained an `order` field rather than a parallel type.** §4 spells
the app's action as `Evidence(event, detail, order=None)`, and Cook 1 already had
`Evidence(event, detail)`; adding the third field keeps one type flowing through
the session, the transport, the evidence writer and the engine log.

**3. One order book per engine run, not per connection.** §4 requires scheduled
events to survive a disconnect and fire after the next Logon, and §2 says orders
live in memory "for one run". A per-connection book would drop them on every
reconnect. The transport therefore builds it once and hands the same instance to
each new `Session`.

**4. No `rules:` section means `app=None`.** §4 promises unchanged Cook 1
behavior with no app, so the transport only builds an order book when rules are
configured. This is what lets Cook 1's integration test keep asserting `35=j`
for a `D` without modification, while the shipped config gets full order
handling.

**5. The session probes the app for `has_pending_events()`.** §4 wants the
deferral evidence recorded "if any are pending" but names no interface. The
session uses `getattr(app, "has_pending_events", None)` and only records when it
is callable and returns True, so an app without the method simply never defers.

**6. `resolve_quote()` lives in `orderecho_Pricing.py` and is called from the
transport.** §8 fixes the *call site* as the transport; putting the
`asyncio.to_thread` + `wait_for` wrapper in the pricing module keeps the
fallback-on-timeout path next to the other failure paths instead of splitting it
across two files.

## ExecutionReport shape

**7. Field order inside the ER body** is
`37, 11, [41], 17, 20, 150, 39, [1], 55, 54, 38, 40, [44], 32, 31, 151, 14, 6, 60, [103], [58]`.
§5.3 lists which fields appear, not the order; this follows the spec's own
listing order with each conditional placed beside its relative.

**8. Zero prices still render to their configured precision.** `31` and `44` are
2 dp (`"0.00"`) and `6` is 4 dp (`"0.0000"`), per §5.2's quantization rule.
§11.1 writes these as `31=0` and `6=0`, which I read as shorthand for the same
values rather than a second, conflicting formatting rule — the tests assert both
the string form and the numeric value.

**9. 35=9 carries exactly §5.7's field list, in that order:**
`37, 11, 41, 39, 434, 102, 58`.

**10. The `CXLREJ` engine-log line format is mine.** §9 gives examples only for
`ORDER` and `FILL`; cancel rejects use the same column layout.

## Order book behavior

**11. Cancel/replace lookup scans every order by its current ClOrdID, open ones
first.** §5.7 says "each **open** order's current ClOrdID", but the same section
then requires a closed order to be found and answered with `102=0` and its
status, and §11.14 asks for exactly that ("cancel filled → `102=0 39=2`"). Open
orders are searched first so a rejected duplicate can never shadow a live order.
This is question 3.

**12. A market order with no quote is rejected, never priced.** `103=0`,
`58=No market price available`. §5.4 assumes a quote is always there, and via
the transport it always is; inventing a price in the pure core would be the one
way to make the order book non-deterministic.

**13. Replacing a market order that carries `44` → 35=9 `102=2`.** §5.8 says
`44` "must be absent" for a market order but gives no code; `102=2` is what the
neighbouring "does not match" cases use.

**14. Bad `36` on an inbound SequenceReset**: missing → `373=1`; present but not
a number → `373=6`. §7.6 covers only the "too low" case.

**15. `fill_rest` sizes itself when it fires, not at ack time,** so it fills
whatever is actually left after clamping and any replace.

**16. Unsolicited cancels and rule rejects drop the remaining schedule** and
record `scheduled events dropped`, matching §5.5's rule for a completed fill.

**17. Duplicate ClOrdID is `103=6` on a `D` and `102=2` on an `F`/`G`.** §6 gives
`103=6`, but a 35=9 has no `103` field, so F/G use §5.7's `102=2` with the same
text. A ClOrdID is consumed even by a message that is then rejected, so it can
never be reused.

**18. FIX 4.2 enumerations** taken as OrdType `1-9`, `A-I`, `P`; Side `1-9`;
HandlInst `1, 2, 3`. §6 names the sets without listing their members.

**19. Business checks that need order fields are skipped for `F`,** which
carries no quantity, order type or price. Side and TimeInForce are still checked.

## Rules and config

**20. `timeout_sec` stays at the spec's `3`,** even though the live run fell back
because of it. §8 states the value; changing it is a product decision, not a
minor ambiguity. This is question 1.

**21. The catch-all rule must be last.** §7.2 requires the load to fail when "no
`any: true` rule at the end". An `any: true` rule placed earlier is also an
error, naming it, because every rule after it is unreachable.

**22. Rule defaults**: `then` defaults to `leave` (the option that does nothing
further); `partial_fill` without `fills` is a config error; a `reject` rule
without `text` gets `Rejected by OrderEcho rule <name>` and without
`reject_code` gets `0`.

**23. Extra rule validation beyond §7.2's list,** all naming the rule:
malformed `first_letter`, backwards ranges (`"Z-A"`), unknown match keys,
duplicate rule names, and negative `delay_ms`.

**24. `RuleError` is re-raised as `ConfigError` from `load_config`,** so anything
loading config sees one exception type, while `load_rules` keeps its own for
direct use and for the §11 rule tests. Both messages name the rule.

**25. `pricing.static.default` is split out of the static map.** Entries that are
not usable prices (non-numeric, zero, negative) are dropped rather than failing
the load, since the default already covers those symbols.

## Pricing

**26. Failed live lookups are not cached — only successes are.** Caching a
fallback would pin a stale static price for `cache_seconds` after a one-second
network blip, which is the opposite of what the cache is for.

**27. A static source answers inline;** only a live source is sent to a worker
thread. There is nothing to overlap when the answer is a dict lookup.

## Evidence and logging

**28. Each ExecutionReport emits two Evidence actions:** the §9 snapshot record,
and one whose detail is the finished §9 human log line. The transport writes the
lifecycle line to the engine log verbatim at INFO and the snapshot at DEBUG, so
the engine log reads as one line per lifecycle step (as §9 shows) while the
evidence file gets the mandated snapshot after every 35=8 and 35=9.

**29. `order` sits between `detail` and `injected`** in the evidence record.

**30. Cook 1's decision 4 is unchanged** — the event name is still prefixed into
`detail`, since the schema still has no dedicated event field.

## Demo client

**31. Added `--host` and `--port` overrides,** which §10 does not mention. §11
requires driving the client against an acceptor on a random port, and writing a
throwaway config file for every test run would be worse.

**32. Inbound heartbeats are not printed.** Everything else is. On a 30-second
heartbeat with a 3-second wait they would rarely appear, but they are noise in a
tool whose whole job is to show order flow.

**33. `cancel-demo` waits for the first ExecutionReport before cancelling,** per
§10, with a 10-second cap and a printed note if it never arrives.

## Tests

**34. Three Cook 1 tests were updated, all of them §3-mandated:** `test_Codec`
and `test_Integration` for `fields` as ordered pairs (§3.3), and `test_Session`
plus `test_Integration` for the counterparty-logout transition (§3.2). No
spec-derived assertion was weakened — the §3.2 test was strengthened to assert
the whole transition sequence and that `LOGOUT_SENT` never appears.

**35. One of my own new tests was wrong and I fixed the test, not the code.**
`test_resend_request_for_future_seqnums_is_ignored` sent the ResendRequest with
an out-of-sequence MsgSeqNum, so the session correctly answered the *gap* before
it ever looked at tag 7. The fix was to send it in sequence and vary tag 7,
which is what the test was meant to exercise.

---

# 5. Questions for you

1. **`pricing.timeout_sec: 3` is too tight for yfinance's first call.** It takes
   about 8 s here because of Yahoo's cookie/crumb negotiation (§3.3), so the
   first market order in a run always falls back, and only later ones benefit
   from the 300-second cache. Raise the default to ~10 s, warm the cache at
   startup for a configured symbol list, or leave it as-is and accept that the
   first order prices statically? I left it at 3 because §8 specifies it.

2. **The demo client's `--wait` default of 3 s is shorter than one price lookup
   plus a fill delay,** which is why the AAPL fill landed in the next session.
   Should the default rise, or should `order` wait for a terminal
   ExecutionReport (filled/canceled/rejected) with `--wait` as a cap instead of a
   fixed sleep?

3. **Confirm decision 11** — §5.7 says lookup is against "each open order's
   current ClOrdID", but the closed-order branch in the same section and §11.14
   both need closed orders to be findable. I search all orders, open ones first.

4. **Should working orders be re-reported on Logon?** The real run showed an
   ExecutionReport for an order placed on a *previous* connection arriving on the
   next one, because the order book is per run (decision 3). That is realistic,
   but a real venue would usually also drop-copy the working order at logon so
   the client is not surprised. Out of scope as specified — worth a line in a
   later cook?

5. **`orders.send_pending_acks` and `replace_ack_ordstatus` are implemented and
   tested but off by default.** Are they meant to stay config-only, or should
   Cook 3's control API be able to flip them mid-run?

---

# 6. `pytest -v` listing

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
tests/test_DemoClient.py::test_side_and_ord_type_words_map_to_fix_values PASSED
tests/test_DemoClient.py::test_limit_order_requires_a_price PASSED
tests/test_DemoClient.py::test_limit_order_keeps_its_price_and_wait PASSED
tests/test_DemoClient.py::test_order_command_completes_and_reports_the_fill PASSED
tests/test_DemoClient.py::test_limit_order_command PASSED
tests/test_DemoClient.py::test_cancel_demo_command PASSED
tests/test_DemoClient.py::test_client_logs_on_with_reset_so_seqnums_never_fight PASSED
tests/test_DemoClient.py::test_port_override_beats_the_config PASSED
tests/test_DemoClient.py::test_refused_connection_exits_non_zero PASSED
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
tests/test_OrderBook.py::test_limit_ack_has_every_required_field PASSED
tests/test_OrderBook.py::test_account_is_echoed_when_present PASSED
tests/test_OrderBook.py::test_full_fill_after_delay PASSED
tests/test_OrderBook.py::test_partial_fill_percentages PASSED
tests/test_OrderBook.py::test_share_fills_then_fill_rest PASSED
tests/test_OrderBook.py::test_share_fill_larger_than_leaves_is_clamped PASSED
tests/test_OrderBook.py::test_partial_then_cancel PASSED
tests/test_OrderBook.py::test_cancel_after_ack PASSED
tests/test_OrderBook.py::test_reject_rule_sends_no_ack PASSED
tests/test_OrderBook.py::test_ack_only_schedules_nothing PASSED
tests/test_OrderBook.py::test_market_uses_quote_limit_uses_price PASSED
tests/test_OrderBook.py::test_market_order_without_a_quote_is_rejected PASSED
tests/test_OrderBook.py::test_avg_px_across_a_replace_that_changes_price PASSED
tests/test_OrderBook.py::test_avg_px_is_exact_with_awkward_prices PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[11] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[21] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[55] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[54] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[60] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[38] PASSED
tests/test_OrderBook.py::test_missing_required_tag_on_new_order[40] PASSED
tests/test_OrderBook.py::test_missing_price_on_limit_order PASSED
tests/test_OrderBook.py::test_bad_data_format_is_rejected[fields0-38] PASSED
tests/test_OrderBook.py::test_bad_data_format_is_rejected[fields1-44] PASSED
tests/test_OrderBook.py::test_bad_transact_time_is_rejected PASSED
tests/test_OrderBook.py::test_value_not_in_enumeration_is_rejected[kwargs0-54] PASSED
tests/test_OrderBook.py::test_value_not_in_enumeration_is_rejected[kwargs1-40] PASSED
tests/test_OrderBook.py::test_value_not_in_enumeration_is_rejected[kwargs2-21] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs0-positive whole number] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs1-positive whole number] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs2-positive whole number] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs3-Side not supported] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs4-OrdType not supported] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs5-Price must be > 0] PASSED
tests/test_OrderBook.py::test_business_failures_produce_a_reject_report[kwargs6-TimeInForce not supported] PASSED
tests/test_OrderBook.py::test_duplicate_cl_ord_id_on_new_order PASSED
tests/test_OrderBook.py::test_unknown_extra_tags_are_ignored PASSED
tests/test_OrderBook.py::test_cancel_request_drops_scheduled_fills PASSED
tests/test_OrderBook.py::test_cancel_unknown_order PASSED
tests/test_OrderBook.py::test_cancel_a_filled_order PASSED
tests/test_OrderBook.py::test_cancel_with_duplicate_cl_ord_id PASSED
tests/test_OrderBook.py::test_cancel_with_mismatched_symbol_or_side PASSED
tests/test_OrderBook.py::test_cancel_referencing_a_superseded_cl_ord_id PASSED
tests/test_OrderBook.py::test_replace_increases_qty PASSED
tests/test_OrderBook.py::test_replace_ack_status_reflects_partial_fills PASSED
tests/test_OrderBook.py::test_replace_to_qty_at_or_below_cum_is_rejected PASSED
tests/test_OrderBook.py::test_replace_changing_side_or_ordtype_is_rejected PASSED
tests/test_OrderBook.py::test_replacing_a_market_order_must_not_carry_a_price PASSED
tests/test_OrderBook.py::test_replace_of_a_limit_order_requires_a_price PASSED
tests/test_OrderBook.py::test_pending_acks_precede_cancel_and_replace PASSED
tests/test_OrderBook.py::test_replace_ack_ordstatus_replaced PASSED
tests/test_OrderBook.py::test_exec_ids_are_unique_and_increasing_across_orders PASSED
tests/test_OrderBook.py::test_every_report_is_followed_by_an_order_snapshot PASSED
tests/test_OrderBook.py::test_rule_and_price_events_are_recorded PASSED
tests/test_OrderIntegration.py::test_market_order_acked_then_fully_filled PASSED
tests/test_OrderIntegration.py::test_limit_order_partials_then_cancel_stops_further_fills PASSED
tests/test_OrderIntegration.py::test_rule_rejected_symbol_gets_one_reject_report PASSED
tests/test_OrderIntegration.py::test_validation_failure_produces_a_session_reject PASSED
tests/test_OrderIntegration.py::test_evidence_and_logs_line_up PASSED
tests/test_OrderIntegration.py::test_orders_survive_a_reconnect_and_fire_after_logon PASSED
tests/test_Pricing.py::test_static_lookup_and_default PASSED
tests/test_Pricing.py::test_static_default_when_no_default_configured PASSED
tests/test_Pricing.py::test_static_prices_are_quantized_to_cents PASSED
tests/test_Pricing.py::test_static_source_ignores_unusable_entries PASSED
tests/test_Pricing.py::test_yfinance_success PASSED
tests/test_Pricing.py::test_yfinance_result_is_cached PASSED
tests/test_Pricing.py::test_cache_is_per_symbol PASSED
tests/test_Pricing.py::test_unusable_price_falls_back[None] PASSED
tests/test_Pricing.py::test_unusable_price_falls_back[0] PASSED
tests/test_Pricing.py::test_unusable_price_falls_back[-1] PASSED
tests/test_Pricing.py::test_unusable_price_falls_back[nonsense] PASSED
tests/test_Pricing.py::test_unusable_price_falls_back[nan] PASSED
tests/test_Pricing.py::test_exception_falls_back_and_never_raises PASSED
tests/test_Pricing.py::test_missing_yfinance_falls_back PASSED
tests/test_Pricing.py::test_failures_are_not_cached PASSED
tests/test_Pricing.py::test_resolve_quote_with_a_static_source PASSED
tests/test_Pricing.py::test_resolve_quote_runs_a_live_source_off_the_event_loop PASSED
tests/test_Pricing.py::test_resolve_quote_times_out_into_the_fallback PASSED
tests/test_Pricing.py::test_timers_keep_running_during_a_slow_lookup PASSED
tests/test_Rules.py::test_exact_symbol_beats_a_range_that_also_matches PASSED
tests/test_Rules.py::test_first_match_wins_even_when_a_later_rule_is_narrower PASSED
tests/test_Rules.py::test_lowercase_symbols_match_a_range_after_uppercasing PASSED
tests/test_Rules.py::test_exact_symbol_match_is_case_sensitive PASSED
tests/test_Rules.py::test_single_letter_range PASSED
tests/test_Rules.py::test_range_boundaries_are_inclusive PASSED
tests/test_Rules.py::test_catch_all_matches_anything PASSED
tests/test_Rules.py::test_zero_match_keys_fails_naming_the_rule PASSED
tests/test_Rules.py::test_multiple_match_keys_fails_naming_the_rule PASSED
tests/test_Rules.py::test_unknown_behavior_fails_naming_the_rule PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[0] PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[-5] PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule PASSED [ 62%]
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule PASSED [ 62%]
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[abc%] PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[50] PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[1.5] PASSED
tests/test_Rules.py::test_bad_fill_entry_fails_naming_the_rule[True] PASSED
tests/test_Rules.py::test_percentages_over_one_hundred_fail_naming_the_rule PASSED
tests/test_Rules.py::test_percentages_summing_to_exactly_one_hundred_are_allowed PASSED
tests/test_Rules.py::test_unknown_then_value_fails_naming_the_rule PASSED
tests/test_Rules.py::test_partial_fill_without_fills_fails_naming_the_rule PASSED
tests/test_Rules.py::test_rule_set_without_a_catch_all_fails PASSED
tests/test_Rules.py::test_catch_all_that_is_not_last_fails PASSED
tests/test_Rules.py::test_duplicate_rule_names_fail PASSED
tests/test_Rules.py::test_bad_first_letter_format_fails_naming_the_rule PASSED
tests/test_Rules.py::test_backwards_range_fails_naming_the_rule PASSED
tests/test_Rules.py::test_unknown_match_key_fails_naming_the_rule PASSED
tests/test_Rules.py::test_rule_without_a_name_fails_with_its_index PASSED
tests/test_Rules.py::test_empty_rule_list_fails PASSED
tests/test_Rules.py::test_percent_fills_floor PASSED
tests/test_Rules.py::test_percent_fills_have_a_floor_of_one_share PASSED
tests/test_Rules.py::test_share_fills_are_used_as_given PASSED
tests/test_Rules.py::test_mixed_share_and_percent_fills PASSED
tests/test_Rules.py::test_rule_delay_overrides_the_default PASSED
tests/test_Rules.py::test_reject_rule_gets_default_code_and_text PASSED
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
tests/test_Session.py::test_app_message_types_are_routed_to_the_app[D] PASSED
tests/test_Session.py::test_app_message_types_are_routed_to_the_app[F] PASSED
tests/test_Session.py::test_app_message_types_are_routed_to_the_app[G] PASSED
tests/test_Session.py::test_market_price_is_passed_through_to_the_app PASSED
tests/test_Session.py::test_other_application_messages_still_get_business_reject PASSED
tests/test_Session.py::test_without_an_app_new_order_single_gets_business_reject PASSED
tests/test_Session.py::test_app_send_is_wrapped_in_a_session_send PASSED
tests/test_Session.py::test_app_session_reject_becomes_a_35_3 PASSED
tests/test_Session.py::test_app_evidence_is_passed_straight_through PASSED
tests/test_Session.py::test_app_timer_runs_only_while_active PASSED
tests/test_Session.py::test_scheduled_events_wait_for_the_next_logon PASSED
tests/test_Session.py::test_deferral_evidence_is_recorded_once_per_disconnect PASSED
tests/test_Session.py::test_no_deferral_evidence_when_nothing_is_pending PASSED
tests/test_Session.py::test_resend_request_with_finite_end_seq_no_is_bounded PASSED
tests/test_Session.py::test_resend_request_end_seq_no_is_never_past_next_out PASSED
tests/test_Session.py::test_resend_request_with_zero_end_seq_no_is_unchanged PASSED
tests/test_Session.py::test_resend_request_for_future_seqnums_is_ignored PASSED
tests/test_Session.py::test_resend_request_without_end_seq_no_is_rejected PASSED
============================= 206 passed in 11.27s =============================
```
