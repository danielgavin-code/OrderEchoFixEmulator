# OrderEchoFixEmulator — Cook 2 Build Spec
**Cook 1 fixes + order state machine + symbol fill rules + pricing + demo client**

---

## 0. Ground rules for Claude Code

- Do **not** run any git commands.
- Do **not** make product decisions. On ambiguity, stop and explain (unless the run prompt says otherwise).
- Build only what is in scope (§2). Do not start Cook 3.
- All Cook 1 tests must keep passing (updated only where §3 changes behavior).
- When finished, report using §13.

---

## 1. Context

Cook 1 built the session layer. Cook 2 makes the emulator a working counterparty: it accepts orders over FIX, applies behavior rules from config, and returns ExecutionReports while maintaining correct order state.

Principles carried forward:
- **Pure core.** The order book is pure logic: inputs (message, time, price quote) in → outbound messages + evidence out. No sockets, no network, no `datetime.now()`.
- **Deterministic.** Same inbound messages + same config + same prices → same outbound content (except SendingTime/TransactTime).
- **Emulator is one target.** Behave like a real broker/venue would. Nothing may assume the counterparty is our Go agent.

---

## 2. Scope

**In scope**
- §3 Cook 1 fixes (three)
- §4 Application hook in the session
- §5 Order book: NewOrderSingle (D), OrderCancelRequest (F), OrderCancelReplaceRequest (G) → ExecutionReport (8), OrderCancelReject (9)
- §6 Validation rules
- §7 Symbol behavior rules from config
- §8 Pricing (static + yfinance, cached, with fallback)
- §9 Evidence and log additions
- §10 Demo client CLI
- §11 Tests

**Out of scope (do not build)**
- FastAPI control API, manual fill/cancel commands, tag injection — Cook 3
- Documentation site — Cook 4
- Persisting orders across engine restarts (orders live in memory for one run)
- Hot-reloading config (restart to pick up changes)
- TimeInForce other than Day; OrdTypes other than Market/Limit
- Replaying application messages on resend (still gap-fill everything)

---

## 3. Cook 1 fixes

**3.1 Bounded ResendRequest (Decision 9).**
When an inbound ResendRequest has `16=N` with N ≠ 0, the gap fill's `36` is `min(N + 1, next_out)`. When `16=0`, unchanged (`36 = next_out`). If `7` (BeginSeqNo) ≥ `next_out`, send nothing and record `Evidence("resend request for future seqnums ignored")`. Add tests for both.

**3.2 Counterparty-initiated logout state (engine log bug).**
When the counterparty sends Logout while we are ACTIVE, we reply Logout and go **directly** `ACTIVE → DISCONNECTED`. `LOGOUT_SENT` is only for logouts *we* initiate. Update the affected test to assert the transition sequence.

**3.3 Evidence `fields` becomes an ordered list of pairs (Decision 18).**
`"fields": [["8","FIX.4.2"],["9","69"],["35","A"], ...]` — preserves order and repeated tags. Update the writer, the §10 schema description in README, and all tests that read `fields`.

**3.4 Cook 1 open questions — answered.**
Decision 9 → fixed by §3.1. Decision 12 (no `373` on Logon-while-logged-on Reject) → keep as is. Decision 5 (don't echo `141=N`) → keep as is.

---

## 4. Application hook — changes to `orderecho_Session.py`

- `Session.__init__(config, seq_store, clock, app=None)`.
- In ACTIVE, after the sequence check passes, messages with MsgType `D`, `F`, `G` go to `app.on_app_message(msg, market_price)` when `app` is set. All other application MsgTypes still get BusinessMessageReject (35=j) exactly as in Cook 1. With `app=None`, Cook 1 behavior is unchanged.
- `on_message(msg, market_price=None)` gains an optional `market_price` argument (a `PriceQuote`, §8) which it passes through to the app.
- The app returns `list[AppAction]`:
  - `AppSend(msg_type, body_fields)` — the session wraps it in a normal `Send` (assigns seq, header).
  - `SessionReject(ref_seq, ref_tag, reason_code, text)` — the session sends 35=3 with `45`, `371`, `373`, `58`.
  - `Evidence(event, detail, order=None)` — passed through.
- `on_timer()` calls `app.on_timer()` **only when state is ACTIVE**. While disconnected or awaiting logon, scheduled order events simply stay due and fire on the first timer tick after the next successful Logon. Record `Evidence("scheduled order events deferred until logon")` once per disconnect if any are pending.
- Transport timer tick changes from 1s to **100 ms** so fill delays in the hundreds of milliseconds are honored. Heartbeat/TestRequest logic is unaffected (it compares elapsed time, not tick counts).

---

## 5. Order book — `orderecho_OrderBook.py` (PURE)

### 5.1 Interface

```python
class OrderBook:
    def __init__(self, orders_config, rules, clock, run_id): ...
    def on_app_message(self, msg, market_price=None) -> list[AppAction]
    def on_timer(self) -> list[AppAction]
    def get_order(self, order_id) -> OrderState | None
    def orders(self) -> list[OrderState]
```

### 5.2 Order state (one per order)

| Field | Notes |
|---|---|
| `order_id` | Ours. `O-<run_id>-<n>`, n from 1, never reused in a run. Stable across replaces. |
| `cl_ord_id` | Current ClOrdID (latest accepted). |
| `cl_ord_id_chain` | Every ClOrdID this order has had, in order. |
| `symbol`, `side`, `ord_type`, `account` | From the order. `account` optional. |
| `order_qty` | Current (changes on replace). Integer shares. |
| `price` | Limit price or None. `Decimal`. |
| `cum_qty`, `leaves_qty` | Integers. |
| `avg_px` | `Decimal`, computed from total notional / cum_qty. |
| `ord_status` | FIX 4.2 OrdStatus value. |
| `fill_price` | Decimal: limit price for limits; quote price for markets, captured at arrival. |
| `price_source` | e.g. `limit`, `live:yfinance`, `static:config`, `static:fallback`. |
| `rule_name` | Which config rule matched. |
| `scheduled` | Pending timed events (fills, unsolicited cancel), each with a due time. |
| `closed` | True once Filled, Canceled, or Rejected. |

ExecIDs: `E-<run_id>-<n>`, one counter for the whole run, every ExecutionReport gets a new one.

All money math uses `Decimal`. Formatting: `44`/`31` quantized to 2 dp; `6` (AvgPx) quantized to 4 dp; `AvgPx=0` before any fill.

### 5.3 ExecutionReport (35=8) fields — FIX 4.2

Always: `37` OrderID, `11` ClOrdID, `17` ExecID, `20=0` (ExecTransType New), `150` ExecType, `39` OrdStatus, `55`, `54`, `38` OrderQty, `40` OrdType, `32` LastShares, `31` LastPx, `151` LeavesQty, `14` CumQty, `6` AvgPx, `60` TransactTime (from clock).
Conditional: `44` if limit; `41` OrigClOrdID on cancel/replace acks; `1` Account if the order had one; `103` OrdRejReason on rejects; `58` Text when there is a reason to explain.
`32`/`31` are `0` on non-fill reports.

FIX 4.2 values used:
- ExecType (150): `0` New, `1` Partial fill, `2` Fill, `4` Canceled, `5` Replace, `6` Pending Cancel, `8` Rejected, `E` Pending Replace
- OrdStatus (39): `0` New, `1` Partially filled, `2` Filled, `4` Canceled, `5` Replaced, `6` Pending Cancel, `8` Rejected, `E` Pending Replace
- OrdRejReason (103): `0` Broker option, `1` Unknown symbol, `6` Duplicate order
- CxlRejReason (102): `0` Too late to cancel, `1` Unknown order, `2` Broker option, `3` Already pending cancel/replace
- CxlRejResponseTo (434): `1` Cancel request, `2` Cancel/Replace request

### 5.4 NewOrderSingle (D)

1. Validate (§6). Structural failure → `SessionReject`. Business failure → ER with `150=8`, `39=8`, `103`, `58`; the order is recorded as closed/rejected.
2. Match a rule (§7). If the rule is `reject`, send the reject ER (as above) and stop.
3. Create order: `39=0`, `cum=0`, `leaves=qty`. Capture `fill_price` and `price_source`.
4. Send ack ER: `150=0`, `39=0`.
5. Schedule the rule's follow-up events relative to the ack time (§7.3).

### 5.5 Fills (scheduled)

- Fill of `q` shares: clamp to `leaves_qty`; skip (with evidence) if the order is closed or `q` clamps to 0.
- `cum += q`, `leaves -= q`, notional += `q × fill_price`, `avg_px = notional / cum`.
- If `leaves == 0` → `150=2`, `39=2`, order closed; drop remaining scheduled events. Else `150=1`, `39=1`.
- `32=q`, `31=fill_price`.

### 5.6 Unsolicited cancel (rule-driven)

`150=4`, `39=4`, `11=<current ClOrdID>`, no `41`, `151=0`, `14`/`6` unchanged, `58=Canceled by OrderEcho rule <rule_name>`. Order closed; drop remaining scheduled events.

### 5.7 OrderCancelRequest (F)

Lookup is by `41` against each open order's **current** ClOrdID.
- `11` already used anywhere this run → 35=9, `102=2`, `58=Duplicate ClOrdID`.
- No match → 35=9, `37=NONE`, `39=8`, `102=1`.
- `55` or `54` don't match the order → 35=9, `102=2`, `58` explains.
- Order closed (filled/canceled/rejected) → 35=9, `102=0`, `39=<its status>`.
- Otherwise:
  - If `orders.send_pending_acks` → first ER `150=6`, `39=6`, `11=<F's 11>`, `41=<orig>`.
  - Then ER `150=4`, `39=4`, `11=<F's 11>`, `41=<orig>`, `151=0`. Order closed; scheduled events dropped; current ClOrdID becomes F's `11`.

35=9 fields (FIX 4.2): `37`, `11`, `41`, `39`, `434`, `102`, `58`.

### 5.8 OrderCancelReplaceRequest (G)

Lookup and the duplicate/unknown/mismatch/closed checks are the same as §5.7 (with `434=2`). Then:
- Only `38` and `44` may change. A different `55`, `54`, or `40` → 35=9, `102=2`, `58` explains.
- New `38` must be **greater than** `cum_qty` → else 35=9, `102=2`, `58=OrderQty must exceed CumQty`.
- Limit order: `44` required (structural, §6). Market order: `44` must be absent.
- Accept:
  - If `send_pending_acks` → ER `150=E`, `39=E` first.
  - Apply: `order_qty = new`, `leaves = new − cum`, `price = new` (limit), `fill_price` follows the new limit price for future fills. Current ClOrdID = G's `11`; append to chain.
  - ER `150=5`, `11=<G's 11>`, `41=<orig>`, with `39` per `orders.replace_ack_ordstatus`:
    - `current` (default) → the order's working status (`0` or `1`)
    - `replaced` → `5`
  - Remaining scheduled fills keep their share amounts; §5.5 clamping handles a reduced leaves.

---

## 6. Validation

Applies to D, F, G. **Order of checks:** required tags → data format → enumerations → business rules.

**Structural → `SessionReject` (35=3 with `45`, `371`, `373`, `58`)**
- Required tag missing → `373=1`, `371=<tag>`.
  - D: `11, 21, 55, 54, 60, 38, 40`; plus `44` if `40=2`.
  - F: `11, 41, 55, 54, 60`.
  - G: `11, 41, 21, 55, 54, 60, 38, 40`; plus `44` if `40=2`.
- Wrong data format (non-numeric qty/price, unparseable `60`) → `373=6`.
- Value not in FIX enumeration → `373=5`: `54` ∉ {1–9}, `40` ∉ FIX 4.2 OrdType set, `21` ∉ {1,2,3}.

**Business → reject ER (D) or 35=9 (F/G)**
- `38` ≤ 0 or not a whole number → `103=0`, `58` explains.
- `54` valid FIX but not supported (supported: `1` Buy, `2` Sell, `5` Sell short, `6` Sell short exempt) → `103=0`.
- `40` valid FIX but not `1` Market or `2` Limit → `103=0`, `58=OrdType not supported`.
- `44` ≤ 0 → `103=0`.
- `59` present and not `0` (Day) → `103=0`, `58=TimeInForce not supported`.
- `11` already used this run → `103=6`, `58=Duplicate ClOrdID`.
- Extra tags not listed here are accepted and ignored.

---

## 7. Behavior rules — `orderecho_Rules.py` (PURE)

### 7.1 Config

Add to `config/orderecho.yaml`:

```yaml
orders:
  default_delay_ms: 500
  send_pending_acks: false        # send 150=6 / 150=E before cancel / replace acks
  replace_ack_ordstatus: current  # current | replaced

rules:                            # first match wins; edit freely
  - name: nasdaq-test-reject
    match: { symbol: ZVZZT }
    behavior: reject
    reject_code: 1
    text: "Unknown symbol"

  - name: nasdaq-test-hold
    match: { symbol: ZWZZT }
    behavior: ack_only

  - name: a-to-d-full
    match: { first_letter: "A-D" }
    behavior: full_fill

  - name: e-to-g-partial
    match: { first_letter: "E-G" }
    behavior: partial_fill
    fills: ["40%", "10%"]
    then: leave                   # leave | fill_rest | cancel

  - name: h-to-j-cancel
    match: { first_letter: "H-J" }
    behavior: cancel_after_ack

  - name: k-to-m-reject
    match: { first_letter: "K-M" }
    behavior: reject
    reject_code: 0
    text: "Rejected by OrderEcho rule"

  - name: odd-lots
    match: { first_letter: "N-P" }
    behavior: partial_fill
    fills: [1, 2, 3, 405]
    then: fill_rest

  - name: default
    match: { any: true }
    behavior: full_fill
```

### 7.2 Matching

- `symbol`: exact, case-sensitive.
- `first_letter: "X-Y"`: first character of `55`, uppercased, within X..Y inclusive. A single letter `"Q"` is allowed.
- `any: true`: always matches.
- First match wins. **Config load fails** (clear error naming the rule) if: a rule has zero or multiple match keys; unknown behavior; a `fills` entry isn't a positive int or a `"N%"` string with 0 < N ≤ 100; percentages in one rule sum > 100; `then` is not a known value; no rule would match (i.e. no `any: true` rule at the end — require it).

### 7.3 Behaviors and schedules

`delay_ms` per rule, defaulting to `orders.default_delay_ms`. Events are spaced `delay_ms` apart, starting `delay_ms` after the ack.

| Behavior | After the ack |
|---|---|
| `full_fill` | One fill of the full qty. |
| `partial_fill` | One fill per `fills` entry, in order; then per `then`: `leave` (order stays open), `fill_rest` (fill remaining leaves), `cancel` (unsolicited cancel). |
| `cancel_after_ack` | One unsolicited cancel. |
| `ack_only` | Nothing. Order stays open. |
| `reject` | No ack. Reject ER immediately with `103=<reject_code>`, `58=<text>`. |

Percent fills resolve to shares **at ack time**: `floor(order_qty × pct / 100)`, minimum 1. Share fills are used as-is. Both clamp to leaves when they fire (§5.5).

---

## 8. Pricing — `orderecho_Pricing.py`

```yaml
pricing:
  mode: live            # live | static
  provider: yfinance
  cache_seconds: 300
  timeout_sec: 3
  static:
    AAPL: 227.50
    default: 100.00
```

- `PriceQuote(price: Decimal, source: str)`.
- `StaticPriceSource`: symbol from `static`, else `default` → source `static:config`.
- `YFinancePriceSource`: `yfinance.Ticker(sym).fast_info["last_price"]`, rounded to 2 dp → source `live:yfinance`. Cached per symbol for `cache_seconds`. On **any** failure (exception, timeout, None, ≤ 0) → static price with source `static:fallback`, plus an engine log WARNING with the reason. **Never raises.**
- Import `yfinance` lazily so `mode: static` works even if it's missing. Add `yfinance` to `requirements.txt`.
- **Where the lookup happens:** in the transport, not the pure core. Before dispatching an inbound `D` with `40=1` (Market), the transport resolves a quote via `asyncio.to_thread` with `timeout_sec`, then calls `session.on_message(msg, market_price=quote)`. Limit orders don't look up a price (`fill_price = 44`, source `limit`). Inbound messages stay strictly in arrival order; timers keep running during the lookup.
- Determinism: the quote is an *input* to the pure core, captured once per order and recorded in evidence. Tests **never** hit the network: use static mode, or monkeypatch `yfinance`.

---

## 9. Evidence and logs

- Evidence records gain an `order` key (null unless relevant). For every outbound 35=8 or 35=9, write a following `event` record with `detail` like `order O-…-3 PARTIALLY_FILLED` and `order` = snapshot `{order_id, cl_ord_id, symbol, side, order_qty, cum_qty, leaves_qty, avg_px, ord_status, price_source, rule_name}` (all values as strings).
- Also record events for: rule matched, price resolved (price + source), fill clamped/skipped, scheduled events dropped, events deferred until logon.
- Engine log (INFO) one line per order lifecycle step, e.g.:
  `ORDER O-20260926-030445-1 AAPL BUY 1000 MKT px=227.50 (live:yfinance) rule=a-to-d-full -> NEW`
  `FILL  O-20260926-030445-1 1000 @ 227.50 cum=1000 leaves=0 avg=227.5000 -> FILLED`
- FIX log unchanged.

---

## 10. Demo client — `orderecho_DemoClient.py`

A small CLI so orders can be sent by hand before the Go agent exists. Reuses `orderecho_Codec`.

```
python orderecho_DemoClient.py order AAPL 1000 buy mkt
python orderecho_DemoClient.py order EFG 1000 sell lmt 10.25 --wait 5
python orderecho_DemoClient.py cancel-demo HJK 500 buy mkt
```

- Connects to host/port from config, logs on as `target_comp_id`, **with `141=Y`** so demo runs never fight persisted sequence numbers.
- `order`: sends one D (ClOrdID `DEMO-<unix ms>`), prints every inbound message as a readable line (MsgType name, ExecType/OrdStatus names, CumQty/LeavesQty/AvgPx), waits `--wait` seconds (default 3), logs out.
- `cancel-demo`: sends a D, waits for the ack, sends F for it, prints results.
- Answers TestRequests; sends Heartbeats if it idles.
- Not part of the emulator's pure core; keep it simple.

---

## 11. Tests

**`test_OrderBook.py`** (FakeClock, static prices, no sockets)
1. Limit D → ack ER with every §5.3 field correct; `37`/`17` formats; `32=0`, `31=0`, `14=0`, `151=qty`, `6=0`.
2. `full_fill`: after delay, one ER `150=2 39=2 32=qty 31=px 14=qty 151=0 6=px`.
3. `partial_fill` 40%/10% on 1000 → fills 400, 100; statuses `1`,`1`; cum 400/500; leaves 600/500.
4. `partial_fill [1,2,3,405]` on 1000 with `then: fill_rest` → cum 1, 3, 6, 411, then final fill 589 → `39=2`.
5. Share fill larger than leaves is clamped.
6. `then: cancel` → unsolicited `150=4 39=4 151=0`, cum unchanged.
7. `cancel_after_ack` → ack then unsolicited cancel.
8. `reject` rule → no ack; `150=8 39=8 103=<code> 58=<text>`.
9. `ack_only` → ack only, no timer events.
10. Market order uses the supplied `PriceQuote`; limit uses `44`; AvgPx with fills at the same price equals that price at 4 dp.
11. AvgPx across a replace that changes limit price (fills at two prices) computed correctly with Decimal.
12. Validation: each structural case → correct `373`/`371`; each business case → correct reject ER; duplicate ClOrdID → `103=6`.
13. Cancel: valid → `150=4 39=4 11=new 41=orig 151=0`; scheduled fills dropped (advance clock, assert none fire).
14. Cancel unknown → 35=9 `37=NONE 39=8 102=1 434=1`. Cancel filled → `102=0 39=2`. Duplicate `11` → `102=2`. Symbol mismatch → `102=2`.
15. Cancel referencing a **previous** (not current) ClOrdID after a replace → `102=1`.
16. Replace qty up → `150=5`, leaves recalculated, current ClOrdID updated, chain extended, `OrderID` unchanged.
17. Replace to qty ≤ cum → 35=9 `434=2 102=2`. Replace changing side → `102=2`.
18. `send_pending_acks: true` → `150=6` before cancel ack; `150=E` before replace ack.
19. `replace_ack_ordstatus: replaced` → `39=5` on replace ack.
20. ExecIDs strictly increasing and unique across orders.

**`test_Rules.py`**
- Matching precedence (exact symbol beats range; first match wins; lowercase symbol matched by range after uppercasing).
- Every config-load failure in §7.2 raises with the rule name in the message.
- Percent resolution: floor, minimum 1.

**`test_Pricing.py`**
- Static lookup and default.
- yfinance success (monkeypatched) → `live:yfinance`, cached (second call doesn't hit it).
- yfinance exception / None / 0 / timeout → `static:fallback`, never raises.

**`test_Session.py` additions**
- D/F/G routed to the app; other app MsgTypes still get 35=j.
- `app=None` → Cook 1 behavior (35=j for D).
- App scheduled events don't fire while disconnected; fire after re-logon; deferral evidence recorded.
- §3.1 and §3.2 fixes.

**`test_OrderIntegration.py`** (real sockets, static pricing, small delays e.g. 50 ms)
- Log on → D AAPL 1000 MKT → receive ack + full fill with correct fields → Logout.
- D EFG 1000 LMT → ack + two partials; then F → cancel ack; no further fills arrive.
- D ZVZZT → immediate reject ER.
- Evidence contains order snapshots after every 35=8; FIX log line count equals evidence message count.

**`test_DemoClient.py`**
- Against a live acceptor on a random port: `order` command completes and exits 0; output includes the fill line.

All tests pass with `pytest -q`. No test touches the network.

---

## 12. README

Add: rules config explained with the default table, pricing modes, how to run the demo client, updated evidence schema (`fields` as pairs, `order` key).

---

## 13. Report back — write to `REPORT_Cook2.md`

1. Files created/changed (list).
2. Full `pytest -q` output (and `pytest -v` listing).
3. A real run: start `orderecho_Main.py` (live pricing), then in another process run `orderecho_DemoClient.py order AAPL 1000 buy mkt` and `orderecho_DemoClient.py order EFG 1000 buy lmt 10.00 --wait 3`. Paste both client outputs, the FIX log lines, and the engine log lines. If yfinance failed, say so and show the fallback line. Stop the engine afterwards.
4. "Decisions I made" — every ambiguity and the choice made.
5. Questions for me.
