# OrderEchoFixEmulator — Cook 4 Build Spec
**Fixes from live testing + instant acks + price band check + real message replay on resend + demo client session mode**

## 0. Ground rules
- No git commands. Build only §2 scope. Do not start Cook 5 (FIX 4.4) or any Go work.
- All existing tests keep passing, updated only where this spec changes behavior.
- Report per §11 into REPORT_Cook4.md.

## 1. Context
Live testing of Cook 3 found: a queued injection leaked into the next session; order endpoints reported `session_not_active` when `order_closed` was the more useful answer; market-order acks waited ~2s (up to 10s) on the Yahoo lookup; yfinance prints noise to the console. Cook 4 fixes those and adds two sell-side features: a price band (fat-finger collar) and real replay of application messages on ResendRequest.
Principles unchanged: pure core, deterministic given its inputs, every outbound message through the session, evidence for everything.

## 2. Scope
In: §3 fixes, §4 instant acks, §5 price band, §6 resend replay, §7 demo client session mode, §8 evidence/logs, §9 config, §10 tests, README.
Out: FIX 4.4, multiple sessions, log viewer, docs site, order persistence across restarts.

## 3. Fixes from live testing
3.1 **Injections are session-scoped.** On disconnect, clear all pending `/inject/next` mutations. Record `Evidence("pending injections dropped on disconnect", detail=<list>)` and an engine log WARNING if any were dropped. `GET /inject` after reconnect shows none.
3.2 **Order endpoint check order:** unknown order → 404 `not_found`; closed → 409 `order_closed`; invalid body (qty/price) → 400 `invalid_request`; session not ACTIVE → 409 `session_not_active`. Order facts are reported regardless of session state.
3.3 **Quiet yfinance.** Its console output ("Cookie fetch from fc.yahoo.com failed…") must not reach our terminal. Route yfinance's logger output into our engine log at DEBUG. If it uses `print`, suppress it **without** globally redirecting stdout (that would swallow our own console echo from other tasks) — e.g. redirect only inside the worker thread's call via a thread-safe approach, or silence at the source. Explain the method used in the report.

## 4. Instant acks (market orders)
- A D with `40=1` is acked **immediately**; the price lookup no longer blocks the ack.
- The transport starts the quote lookup in the background (`asyncio.to_thread`, `pricing.timeout_sec`) and delivers the result via a new pure-core input: `session.on_price(order_id, quote)` → `app.on_price(order_id, quote)`.
- Until the quote arrives the order has `fill_price = None`, `price_source = "pending"`. Scheduled fills that come due while the price is pending **wait**, then fire in order the moment the quote arrives (same tick).
- `POST /orders/{id}/fill` with no `price` while pending → 409 `price_pending`. With an explicit `price` → allowed.
- Lookups always resolve (live, or `static:fallback` on failure/timeout), so pending never lasts beyond `timeout_sec`.
- Evidence records `price resolved` with source and the delay since the ack.
- Limit orders: `fill_price = 44` immediately, as before. (Band lookups for limits are §5.)

## 5. Price band (sell-side fat-finger collar)
Config:
```yaml
price_band:
  enabled: true
  pct: 10                  # allowed distance from reference, percent
  mode: aggressive         # aggressive | both
  enforce_on_fallback: false
  lookup_timeout_ms: 1500  # max wait for a reference before the ack decision
```
Rules may override with `price_band_pct: <number>` or `price_band_pct: off`.

**Applies to** D with `40=2`, and G on a limit order. Market orders are never checked.

**Reference:** before dispatching a limit D or G, the transport resolves a quote with `lookup_timeout_ms` as the timeout (cache hits are instant). If none arrives in time, or the source is `static:fallback` with `enforce_on_fallback: false` → skip the check, record `Evidence("price band skipped: <reason>")`. `static:config` quotes ARE enforced (tests and static mode rely on this). The quote is passed into the pure core as an input; the pure core does the comparison.

**Check:** `band = ref × pct / 100`.
- `aggressive`: Buy (54=1) rejects if `44 > ref + band`; Sell / Sell short (54=2,5,6) rejects if `44 < ref − band`.
- `both`: rejects if `|44 − ref| > band`.
- Exactly at the edge passes.

**Reject:**
- D → ER `150=8 39=8 103=3` (Order exceeds limit), `58=Limit <px> outside <pct>% band of ref <ref> (<source>)`.
- G → 35=9 `434=2 102=2`, same `58`; the order is unchanged.
- Runs after §6-of-Cook-2 validation and before rule matching.

**Log:** `BAND  <cl_ord_id> BUY LMT 500.00 ref=227.50 (live:yfinance) band=±10% -> REJECT` (or `-> PASS` / `-> SKIPPED (<reason>)`).
`GET /rules` also returns the effective `price_band` config.

## 6. Real message replay on ResendRequest
6.1 **Outbound message store** — `orderecho_MessageStore.py`
- Every outbound message is stored **as sent** (after any injection), keyed by seq: `data/msgstore/<SENDER>-<TARGET>.jsonl`, one JSON line per message `{seq, msg_type, raw, sent_ts, injected}` where `raw` keeps real SOH characters (JSON-escaped). Flush per line.
- Loaded at startup so replay survives engine restarts.
- On sequence reset (Logon `141=Y`, `--reset-seqnums`, or `POST /session/reset-seqnums`) the current file is renamed with a timestamp suffix (archived, never deleted) and a fresh store starts.
- Gap-fill messages we send in response to a ResendRequest are **not** stored (they reuse old seqs).

6.2 **Config:** `session.resend_mode: replay | gapfill` (default `replay`). `gapfill` keeps Cook 3 behavior exactly.

6.3 **Replay algorithm** for inbound ResendRequest `7=B`, `16=E`:
- `end = next_out − 1` if `E = 0`, else `min(E, next_out − 1)`. If `B > end` → send nothing, evidence (as Cook 2 §3.1).
- Walk seqs `B..end` in order:
  - **Admin types** `0, 1, 2, 3, 4, 5, A`, and any seq missing from the store (e.g. skipped by `/inject/seq-gap`, or archived) → not resent. Consecutive runs collapse into **one** SequenceReset-GapFill: `34=<first seq of run>`, `43=Y`, `122=<now>`, `123=Y`, `36=<first seq after the run>`.
  - **Application types** → resend the stored message with: same `34`, `43=Y`, `122=<its original 52>`, `52=<now>`; every other field identical; recompute `9` and `10`.
- Everything is emitted in seq order as one batch before any new outbound message. None of it consumes `next_out`.
- A replayed message that was originally injected keeps its mutation, and its evidence record is `injected: true` with `detail="replay of injected seq N"`.
- Evidence records each replayed/gap-filled range; engine log: `RESEND 5..12 -> replayed 6, gap-filled 3 runs`.

6.4 `POST /inject/duplicate-last` now reads from the message store (behavior unchanged).

## 7. Demo client session mode
- `python orderecho_DemoClient.py session` — logs on (`141=Y`), stays connected until Ctrl+C or `quit`, prints every inbound message, answers TestRequests, sends Heartbeats.
- Interactive commands typed at a `> ` prompt:
  - `order <SYM> <QTY> <buy|sell> <mkt|lmt> [PX]`
  - `cancel <ClOrdID>` (sends F using the order's known symbol/side)
  - `replace <ClOrdID> <QTY> [PX]` (sends G)
  - `resend <begin> [end]` (sends a ResendRequest; end defaults to 0)
  - `status` (lists the orders this client sent, with last known OrdStatus/Cum/Leaves)
  - `help`, `quit`
- Inbound messages print while you type (don't wait for input). Use asyncio stdin reading.
- `order` and `cancel-demo` gain `--no-exit`: after the terminal state, stay connected like `session` until Ctrl+C.

## 8. Evidence and logs
Evidence events added: `price pending`, `price resolved` (source, delay), band `pass/reject/skipped`, `pending injections dropped`, resend replay ranges, store archived. All as `event` records with detail; order snapshots continue after every 35=8/35=9.

## 9. Config additions (with defaults)
```yaml
session:
  resend_mode: replay
storage:
  msgstore_dir: data/msgstore
price_band: (see §5)
```

## 10. Tests
**Fixes**
1. Queue an injection, disconnect, reconnect → next ER is clean; drop evidence recorded.
2. Closed order + logged out → `POST fill` → 409 `order_closed`; unknown + logged out → 404.
3. yfinance noise: monkeypatched yfinance that prints and logs → nothing on stdout from it; message present in engine log at DEBUG.

**Instant acks**
4. Market D with a slow price source (monkeypatched delay) → ack arrives immediately; fill waits for the quote, then fires; evidence shows delay.
5. Scheduled partials that come due while pending all fire in order when the quote lands.
6. Manual fill with no price while pending → 409 `price_pending`; with price → 200.

**Price band** (`test_PriceBand.py`, static pricing)
7. Buy limit inside band → accepted; exactly at edge → accepted; above → reject 103=3 with exact 58.
8. Sell below band → reject; sell far above: `aggressive` → accepted, `both` → rejected.
9. Market orders never checked.
10. G moving price outside band → 35=9 434=2 102=2, order unchanged; inside → accepted.
11. Fallback quote with `enforce_on_fallback` false → skipped + evidence; true → enforced.
12. Lookup slower than `lookup_timeout_ms` → skipped, order acked.
13. Rule override `price_band_pct: 2` tightens; `off` disables; `enabled: false` disables all.
14. Integration: D AAPL LMT 500 with static AAPL=227.50 → reject ER over the wire.

**Replay** (`test_Replay.py`)
15. Send ack + 2 fills, client ResendRequest for that range → 3 ERs replayed with original seqs, `43=Y`, `122`=original 52, valid checksums, identical bodies.
16. Range mixing heartbeats and ERs → admin runs collapsed into single gap fills with correct `34`/`36`; ERs replayed.
17. Bounded `16=N` respected; `16=0` goes to `next_out − 1`; `B > end` → nothing.
18. After `/inject/seq-gap skip 3` → the skipped seqs are gap-filled, surrounding ERs replayed.
19. Replay of an injected message keeps the mutation and evidence says so.
20. Engine restart between send and ResendRequest → replay still works from the store file.
21. Logon `141=Y` archives the store (old file renamed, new file empty).
22. `resend_mode: gapfill` → Cook 3 behavior (single gap fill).

**Demo client**
23. `session` mode: scripted stdin (`order`, `cancel`, `quit`) against a live acceptor → expected outputs, exit 0.
24. `--no-exit` stays connected after terminal state until stdin `quit`.

All pass with `pytest -q`. No test touches the real network.

## 11. Report → REPORT_Cook4.md
1. Files created/changed. 2. Full `pytest -q` output.
3. Real run (live pricing): start the engine, then with the demo client in `session` mode: a market order on AAPL (show ack timing vs price-resolved line), a buy limit on AAPL far above market (band reject), a buy limit inside the band, then `resend 1 0` → show replayed ERs in the client output and the RESEND engine log line. Paste client output, FIX log lines, and engine log lines. Stop everything afterwards.
4. How yfinance noise was silenced. 5. Decisions I made. 6. Questions for me.
