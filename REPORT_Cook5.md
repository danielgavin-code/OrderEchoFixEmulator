# OrderEchoFixEmulator — Cook 5 build report

Built end to end from `SPEC_Cook5.md`. `pytest -q` passes fully: **439 passed**,
up from Cook 4's 328. No git commands were run. Cook 6 was not started.

**The golden fixtures were captured first, from untouched Cook 4 code**, before
a line of rendering was changed. Their SHA-256 is unchanged since capture, and
the 4.2 profile reproduces them byte for byte:

```
39ee14e903d0b587894c8e8e61b7be622d84cdfe3e2f3c6a7bdabbd09487706e   (at capture)
39ee14e903d0b587894c8e8e61b7be622d84cdfe3e2f3c6a7bdabbd09487706e   (now)
```

They earned their keep once already: mid-refactor they caught a reject-text
regression I would not have noticed (decision 4).

Background shell: a leftover `orderecho_DemoClient.py session` (PID 13782, up
8m37s) whose engine was long gone. It ignored SIGINT — it was blocked in
`sys.stdin.readline` on a worker thread — and stopped on SIGTERM. That is a
real wrinkle in Cook 4's session mode; question 4.

---

## 1. Files created and changed

**New (7)**

| File | What it is |
|---|---|
| `orderecho_FixVersion.py` | The version profiles: BeginString, required tags, enumerations, ExecType mapping, reason-code tables, and the renderers for the neutral `ExecReport` / `CancelReject`. |
| `orderecho_Version.py` | `ORDERECHO_VERSION` / `ORDERECHO_BUILD`, the only place a build is named. |
| `config/orderecho_fix44.yaml` | The default config with one line changed. |
| `tests/golden_scenario.py` | The scripted scenario, renderable in either version. |
| `tests/capture_golden.py` | Writes the fixtures; refuses to overwrite without `--force`. |
| `tests/golden/fix42/*.txt` | Ten frozen fixtures. |
| `tests/isolation.py` | `isolated_config` plus the guard's helpers (§3.5). |
| `tests/test_Golden.py`, `test_FixVersion.py`, `test_CarryOvers.py`, `test_Isolation.py` | 13 + 64 + 15 + 7 tests. |

**Changed (12)**

| File | Change |
|---|---|
| `orderecho_OrderBook.py` | Emits neutral `ExecReport`/`CancelReject` with neutral `Reason`s and renders through the session's profile; validation consults the profile; band runs before the rule is announced (§3.2); `on_no_reference: reject` (§3.1). |
| `orderecho_Session.py` | Holds a profile; BeginString mismatch → Logout + disconnect, never a session Reject (§5.5). |
| `orderecho_Transport.py` | Builds the profile, hands it to the order book, stamps the version on stored messages and archives the store when the version changes (§5.6); band-skip lines log at WARNING. |
| `orderecho_Codec.py` | Frames on any FIX BeginString, so a wrong-version message is decoded and answered rather than silently discarded. |
| `orderecho_Config.py` | `fix_version` accepts any profile we have; `price_band.on_no_reference`, `lookup_timeout_ms` default 3000, `pricing.warm_symbols`. |
| `orderecho_Pricing.py` | `warm_up` primes a list of symbols. |
| `orderecho_MessageStore.py` | Records and reports the FIX version of stored messages. |
| `orderecho_ControlApi.py` | `/health` returns version + build; `/status` reports `fix_version`; reset returns `archived_store`. |
| `orderecho_Main.py` | Banner shows version, build and FIX version; startup evidence event; warms `warm_symbols`. |
| `orderecho_DemoClient.py` | `--fix`, `exec=TRADE`, `(PossDup)` marker, counter-suffixed ClOrdIDs, `status` lists `SENT` orders (§3.6, §8). |
| `conftest.py` | The session-level isolation guard (§3.5). |
| `config/orderecho.yaml`, `README.md`, existing tests | New keys; version docs; `msgstore_dir` everywhere; version-parametrized integration suite. |

## 2. Test results

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 49%]
........................................................................ [ 65%]
........................................................................ [ 81%]
........................................................................ [ 98%]
.......                                                                  [100%]
439 passed in 45.97s
```

| File | Tests |
|---|---|
| `tests/test_FixVersion.py` | 64 (new) |
| `tests/test_OrderBook.py` | 60 |
| `tests/test_ControlApi.py` | 53 |
| `tests/test_Session.py` | 50 |
| `tests/test_Rules.py` | 36 |
| `tests/test_PriceBand.py` | 31 |
| `tests/test_Pricing.py` | 26 |
| `tests/test_DemoClient.py` | 22 |
| `tests/test_OrderIntegration.py` | 16 |
| `tests/test_CarryOvers.py` | 15 (new) |
| `tests/test_Golden.py` | 13 (new) |
| `tests/test_Replay.py` | 14 |
| `tests/test_Codec.py` | 12 |
| `tests/test_Logging.py` | 9 |
| `tests/test_Isolation.py` | 7 (new) |
| `tests/test_Integration.py` | 6 |
| `tests/test_SeqStore.py` | 5 |
| **Total** | **439** |

**Per version, for the parametrized suites:**

| Suite | FIX 4.2 | FIX 4.4 |
|---|---|---|
| `test_OrderIntegration.py` (whole order flow on real sockets) | 8 | 8 |
| `test_Golden.py` (frozen fixtures) | 13 | — |
| `test_FixVersion.py::test_fix44_never_emits_exec_trans_type` | — | 10 |
| `test_FixVersion.py::test_both_versions_agree_on_what_happened` | 9 scenarios, both versions compared in each |
| Reason-code table rows | 6 order + 4 cancel, both versions each |

Three consecutive full runs: 46.83s, 46.71s, 45.97s, all 439 passing.

## 3. A real run (§10.3)

### 3.1 FIX 4.4 — `config/orderecho_fix44.yaml`, live pricing

```
$ .venv/bin/python orderecho_Main.py --config config/orderecho_fix44.yaml --reset-seqnums
OrderEchoFixEmulator 0.5.0 (cook5) - FIX.4.4 acceptor
  listening      : 127.0.0.1:9878
  comp ids       : sender=ORDERECHO target=AGENT
  message store  : data/msgstore/ORDERECHO-AGENT.jsonl (0 stored)
  control api    : http://127.0.0.1:8090  (docs at /docs)
```

```
$ .venv/bin/python orderecho_DemoClient.py --config config/orderecho_fix44.yaml session   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT [FIX.4.4]
<- Logon                 HeartBtInt=30
Connected. Inbound messages print as they arrive.
> order AAPL 1000 buy mkt
-> NewOrderSingle      AAPL 1000 BUY MKT  11=DEMO-1790495911654-1
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790495911654-1 37=O-20260927-075823-1
<- ExecutionReport       AAPL exec=TRADE status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700  11=DEMO-1790495911654-1 37=O-20260927-075823-1
> order EFG 1000 buy mkt
-> NewOrderSingle      EFG 1000 BUY MKT  11=DEMO-1790495917034-2
<- ExecutionReport       EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790495917034-2 37=O-20260927-075823-2
<- ExecutionReport       EFG exec=TRADE status=PARTIALLY_FILLED  last=400@120.98  cum=400 leaves=600 avg=120.9800  11=DEMO-1790495917034-2 37=O-20260927-075823-2
<- ExecutionReport       EFG exec=TRADE status=PARTIALLY_FILLED  last=100@120.98  cum=500 leaves=500 avg=120.9800  11=DEMO-1790495917034-2 37=O-20260927-075823-2
> order AAPL 100 buy lmt 900.00
-> NewOrderSingle      AAPL 100 BUY LMT 900.00  11=DEMO-1790495923034-3
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1790495923034-3 37=O-20260927-075823-3  103=3  "Limit 900.00 outside 10% band of ref 341.07 (live:yfinance)"
> resend 1 0
-> ResendRequest       7=1 16=0
<- SequenceReset         (PossDup)
<- ExecutionReport       (PossDup)  AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790495911654-1 37=O-20260927-075823-1
<- ExecutionReport       (PossDup)  AAPL exec=TRADE status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700  11=DEMO-1790495911654-1 37=O-20260927-075823-1
<- ExecutionReport       (PossDup)  EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790495917034-2 37=O-20260927-075823-2
<- ExecutionReport       (PossDup)  EFG exec=TRADE status=PARTIALLY_FILLED  last=400@120.98  cum=400 leaves=600 avg=120.9800  11=DEMO-1790495917034-2 37=O-20260927-075823-2
<- ExecutionReport       (PossDup)  EFG exec=TRADE status=PARTIALLY_FILLED  last=100@120.98  cum=500 leaves=500 avg=120.9800  11=DEMO-1790495917034-2 37=O-20260927-075823-2
<- ExecutionReport       (PossDup)  AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1790495923034-3 37=O-20260927-075823-3  103=3  "Limit 900.00 outside 10% band of ref 341.07 (live:yfinance)"
> quit
<- Logout                "Logout acknowledged"
```

Everything §10.3 asks for is in there: `exec=TRADE` for the full fill *and* both
partials, a band reject against a **live** reference, and `(PossDup)` on every
replayed message. The ClOrdID counters (`-1`, `-2`, `-3`) are §3.6.

**FIX log — `8=FIX.4.4` throughout, and not one `20=`:**

```
20260927-07:58:31.320 IN   seq=1    35=A  8=FIX.4.4|9=75|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260927-07:58:31.319|98=0|108=30|141=Y|10=074|
20260927-07:58:31.634 OUT  seq=1    35=A  8=FIX.4.4|9=75|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260927-07:58:31.634|98=0|108=30|141=Y|10=074|
20260927-07:58:31.655 IN   seq=2    35=D  8=FIX.4.4|9=137|35=D|49=AGENT|56=ORDERECHO|34=2|52=20260927-07:58:31.654|11=DEMO-1790495911654-1|21=1|55=AAPL|54=1|38=1000|40=1|60=20260927-07:58:31.654|10=201|
20260927-07:58:31.657 OUT  seq=2    35=8  8=FIX.4.4|9=225|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-07:58:31.657|37=O-20260927-075823-1|11=DEMO-1790495911654-1|17=E-20260927-075823-1|150=0|39=0|55=AAPL|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20260927-07:58:31.656|10=246|
```

The replay, on the original sequence numbers with `43=Y` and the original `52`
in `122`:

```
20260927-07:58:47.086 OUT  seq=1    35=4  8=FIX.4.4|9=99|35=4|...|34=1|52=20260927-07:58:47.086|43=Y|122=20260927-07:58:47.085|123=Y|36=2|10=035|
20260927-07:58:47.086 OUT  seq=2    35=8  8=FIX.4.4|9=256|35=8|...|34=2|52=20260927-07:58:47.086|43=Y|122=20260927-07:58:31.657|37=O-20260927-075823-1|...
20260927-07:58:47.098 OUT  seq=3    35=8  8=FIX.4.4|9=263|35=8|...|34=3|52=20260927-07:58:47.098|43=Y|122=20260927-07:58:32.241|37=O-20260927-075823-1|...
20260927-07:58:47.101 OUT  seq=7    35=8  8=FIX.4.4|9=331|35=8|...|34=7|52=20260927-07:58:47.101|43=Y|122=20260927-07:58:43.039|37=O-20260927-075823-3|...
```

A grep over the whole 4.4 section of the FIX log finds **0** occurrences of
`|20=`; the 4.2 section below has 2.

**Engine log:**

```
20260927-07:58:23.330 INFO  Startup: version=0.5.0 build=cook5 fix_version=FIX.4.4 config=config/orderecho_fix44.yaml host=127.0.0.1 port=9878 heartbeat_grace_pct=20.0 logout_timeout_sec=10.0 resend_mode=replay price_band=on evidence=data/evidence/20260927-075823.jsonl
20260927-07:58:27.271 INFO  Price warm-up: AAPL 341.07 (live:yfinance)
20260927-07:58:29.112 INFO  Price warm-up: MSFT 516.17 (live:yfinance)
20260927-07:58:29.747 INFO  Price warm-up: SPY 771.35 (live:yfinance)
20260927-07:58:31.657 INFO  price pending: AAPL market order acked before its quote
20260927-07:58:31.657 INFO  ORDER O-20260927-075823-1 AAPL BUY 1000 MKT px=? (pending) rule=a-to-d-full -> NEW
20260927-07:58:31.658 INFO  price resolved: AAPL 341.07 (live:yfinance), 0.002s after the ack
20260927-07:58:32.242 INFO  FILL  O-20260927-075823-1 1000 @ 341.07 cum=1000 leaves=0 avg=341.0700 -> FILLED
20260927-07:58:37.054 INFO  price pending: EFG market order acked before its quote
20260927-07:58:39.024 INFO  price resolved: EFG 120.98 (live:yfinance), 1.980s after the ack
20260927-07:58:39.025 INFO  FILL  O-20260927-075823-2 400 @ 120.98 cum=400 leaves=600 avg=120.9800 -> PARTIALLY_FILLED
20260927-07:58:39.026 INFO  FILL  O-20260927-075823-2 100 @ 120.98 cum=500 leaves=500 avg=120.9800 -> PARTIALLY_FILLED
20260927-07:58:43.039 INFO  BAND  DEMO-1790495923034-3 BUY LMT 900.00 ref=341.07 (live:yfinance) band=±10% -> REJECT
20260927-07:58:43.040 INFO  ORDER O-20260927-075823-3 AAPL BUY 100 LMT px=900.00 (limit) rule=a-to-d-full -> REJECTED
20260927-07:58:47.102 INFO  RESEND 1..7 -> replayed 6, gap-filled 1 run(s)
```

Both Cook 4 carry-overs are visible here. The warm-up primed AAPL before any
order arrived, so the market order's price landed **0.002s after the ack** (a
cache hit) and the band had a live reference to judge 900.00 against —
where Cook 4 would have skipped the check on a cold cache.

### 3.2 The same market order on FIX 4.2, same codebase

```
$ .venv/bin/python orderecho_DemoClient.py --config config/orderecho.yaml session   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT [FIX.4.2]
> order AAPL 1000 buy mkt
-> NewOrderSingle      AAPL 1000 BUY MKT  11=DEMO-1790495944161-1
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790495944161-1 37=O-20260927-075856-1
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=1000@341.07  cum=1000 leaves=0 avg=341.0700  11=DEMO-1790495944161-1 37=O-20260927-075856-1
```

```
20260927-07:59:04.164 OUT  seq=2    35=8  8=FIX.4.2|9=230|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-07:59:04.164|37=O-20260927-075856-1|11=DEMO-1790495944161-1|17=E-20260927-075856-1|20=0|150=0|39=0|55=AAPL|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20260927-07:59:04.163|10=191|
20260927-07:59:04.704 OUT  seq=3    35=8  8=FIX.4.2|9=237|35=8|...|34=3|...|20=0|150=2|39=2|55=AAPL|54=1|38=1000|40=1|32=1000|31=341.07|151=0|14=1000|6=341.0700|...|10=057|
```

Same engine, same price, same order: `8=FIX.4.2`, `20=0` present, `150=2` on the
fill where 4.4 said `150=F`, and the client printed `exec=FILL` rather than
`exec=TRADE`. Both engines were stopped afterwards; 9878 and 8090 are free.

## 4. Decisions I made

### Capturing the golden output

**1. Fixtures capture what goes on the wire, not the narrative.** Each line is
an `AppSend` (MsgType plus ordered body fields), a `SessionReject`, or a
`RequestPrice`, with TransactTime normalized. `Evidence` actions are
deliberately excluded: §3.2 reorders some of them on purpose, and their text is
commentary rather than FIX. What must not move is the FIX, and that is
what is frozen.

**2. Ten scenarios, one file each** — acks, partial fills, cancel, replace,
pending acks, business rejects, rule rejects, band rejects, cancel rejects,
session rejects — covering every case §6 lists. A per-file split makes a
failure say which behavior moved.

**3. The capture script refuses to overwrite without `--force`,** so the
fixtures cannot be re-baselined by muscle memory once the refactor is under way.

**4. The fixtures caught a real regression, and I fixed the code.** Routing
validation through the profile, I wrote `f"Side not a {self.profile.name} value"`
— and `name` is `FIX.4.2`, where the original text said `FIX 4.2`. One
character, in a `58` a counterparty could be matching on. The fixture failed;
I gave the profile a separate prose `label` and the text went back to what it
was. Without §6 this would have shipped.

### The version seam

**5. The order book still returns `AppSend` with rendered fields.** Internally
it builds a neutral `ExecReport`/`CancelReject` and asks the profile to render
it, but the public API is unchanged — which is what let the same scenario
driver run against both the pre- and post-refactor code and mean the same
thing.

**6. Neutral `Reason` values, not codes.** The order book says
`BAD_QUANTITY`; the profile says `103=0` or `103=13`. Adding a version now
means adding a table row, not hunting for literals.

**7. `profile` defaults to FIX42 on `OrderBook`.** Every existing caller and
test keeps working, and a session that forgets to pass one behaves as before
rather than failing obscurely.

**8. The codec frames on `8=FIX`, not on the session's BeginString.** §5.5
needs a wrong-version Logon to be *decoded* so it can be answered with a Logout
naming the expected version. Strict framing would have discarded it as garbage
and left the counterparty hanging.

**9. The profile carries a prose `label` as well as a `name`.** `FIX.4.2` for
the wire, `FIX 4.2` for reject text — see decision 4.

**10. The 4.4 enumeration sets** are Side `1-9` plus `A-G`, OrdType `1-9`,
`A-M`, `P`, TimeInForce `0-7`. Values valid in 4.4 but unsupported by us stay
business rejects, per §5.3.

**11. TimeInForce is now checked structurally against the profile too.** §4
gives the profile TimeInForce; a value outside the version's set is `373=5`,
while a valid-but-unsupported one (anything but Day) remains a business reject.
No golden line moved, because 4.2 golden only exercises `59=1`, which is valid.

### Carry-overs

**12. The band and the rule: matched first, announced second.** §3.2 wants the
band checked before rule matching, but a rule may set `price_band_pct`, which
cannot be known without matching. So the rule is resolved silently, the band
decides and logs, and only then is "rule matched" recorded and the rule's
behavior applied. The log reads in the order the checks happen, the override
still works, and a band breach beats a `reject` rule — which §3.2 asks for
explicitly and `test_band_is_checked_before_the_rule` pins.

**13. `on_no_reference` governs only a genuinely absent quote.** A
`static:fallback` reference is a different situation with its own switch
(`enforce_on_fallback`), so it keeps its own behavior. `on_no_reference` fires
when there is no quote at all.

**14. The "no reference" reject reuses `evaluate_band`'s existing 4-tuple.**
The caller distinguishes the two rejecting reasons by whether a quote came
back — a band breach always has one. Returning a fifth element would have
broken every existing caller for no new information.

**15. A `G` rejected for want of a reference uses `102=2`.** §5.4's cancel
table has no row for it; broker option is the neighbouring catch-all, and the
`58` says exactly what happened.

**16. Band skips log one WARNING, not an INFO and a WARNING.** §3.1 wants a
warning per skip; the `BAND … -> SKIPPED (…)` line is promoted to WARNING
rather than adding a second line beside it.

**17. `/health` and the reset response changed shape, as specified.** §3.3 and
§3.4 answer my Cook 4 question 1: `/health` now returns version *and* build,
and reset returns `archived_store`. I updated the two Cook 3 assertions
accordingly — this time the spec told me to.

### Test isolation

**18. `isolated_config` is the only way a test should build a config,** and
`test_every_storage_key_is_isolated` reads the dataclasses to check that every
`*_dir` field is covered. A future storage key with a default fails that test
until it is added to `STORAGE_KEYS`, which is what §3.5 asks for.

**19. The guard is tested by actually failing a run.** A probe test is written
into `tests/`, pytest is run on it in a subprocess (so the repo's conftest — and
therefore the guard — applies), and the assertion is that the run fails naming
the leaked path. My first attempt put the probe in `tmp_path`, where the
conftest never loaded and the guard silently did not run; the test passed for
the wrong reason until I moved it.

### Clients and tests

**20. The demo client's shared 4.2 fixture stayed 4.2.** Parametrizing it over
both versions changed what a dozen existing tests were asserting (`exec=FILL`
became `exec=TRADE`), so 4.4 got its own fixture instead. The *integration*
suite is parametrized, because its assertions are version-neutral once the
ExecType is derived from the session.

**21. `session_rejects` is excluded from the version-neutrality test.** It
omits HandlInst, which 4.2 requires and 4.4 does not — a deliberate difference
covered by its own tests. §9.7 asks for parametrization "where the behavior is
version-neutral"; that scenario is not.

**22. Three of my own new tests were wrong and I fixed the tests.** A fixture
assertion that every golden file contains `SEND ` (session rejects contain only
`REJECT `); a ClOrdID regex that stopped at the first hyphen once §3.6 added a
counter; and an exit-code expectation of 0 for a client whose Logon is refused —
exiting 1 there is correct. No spec-derived assertion was weakened.

## 5. Questions for you

1. **`/health` is now `{"ok", "version", "build"}` — is anything parsing the
   old `{"ok", "version": "cook3"}` shape?** §3.3 specified the change and I
   made it, but it is the one breaking API change in this cook.

2. **Should the band reject on a replace get its own CxlRejReason?** Decision
   15 uses `102=2` because §5.4 has no row for it. If you would rather it were
   distinguishable, 4.4 has `102=6`-adjacent values free and 4.2 does not.

3. **`ExecRefID`/`ExecTransType` semantics for busts.** 4.4 drops tag 20, so a
   trade correction or cancel is expressed with `150=H`/`150=G` and `19`
   instead. We never bust a fill, so nothing is missing today — but if Cook 6
   or later adds one, that is the 4.4-shaped way to do it and worth specifying.

4. **Session mode ignores Ctrl+C while waiting for input** (found stopping the
   leftover client at the top of this run). `sys.stdin.readline` runs on an
   executor thread, so SIGINT does not interrupt it; SIGTERM works. Worth
   fixing with a non-blocking stdin reader, or is "type `quit`" enough?

5. **Carried over and still open:** working orders are not drop-copied on Logon
   (Cook 2), `orders.send_pending_acks` / `replace_ack_ordstatus` are still
   config-only (Cook 3), and `price_band.mode` is global rather than per-rule
   (Cook 4).
