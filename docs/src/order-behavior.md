# Order behavior

What happens to an order after it is acked is decided by `rules:` in the
config. Rules are matched **in order, first match wins**, and the last rule
must be a catch-all so that every order gets an answer.

## Matching

A rule matches on exactly one of:

| Key | Matches |
|---|---|
| `symbol: ZVZZT` | tag 55 exactly, case-sensitive |
| `first_letter: "A-D"` | the uppercased first character of tag 55, in that range |
| `first_letter: "Q"` | a single letter |
| `any: true` | everything |

Zero or several match keys in one rule is a config error, as is a rule set with
no catch-all, an unknown behavior, or an impossible fill schedule.

## Behaviors

| Behavior | After the ack |
|---|---|
| `full_fill` | One fill of the whole quantity. |
| `partial_fill` | One fill per `fills` entry, then `then:`. |
| `cancel_after_ack` | One unsolicited cancel. |
| `ack_only` | Nothing. The order stays working. |
| `reject` | No ack at all — a reject ExecutionReport with `reject_code` and `text`. |

`then:` says what to do when the listed fills are done: `leave` (the remainder
rests), `fill_rest` (one more fill for everything left), or `cancel` (an
unsolicited cancel for the remainder).

`fills` entries are whole share counts or percentages of the order quantity:

```yaml
- name: e-to-g-partial
  match: { first_letter: "E-G" }
  behavior: partial_fill
  fills: ["40%", "10%"]
  then: leave

- name: odd-lots
  match: { first_letter: "N-P" }
  behavior: partial_fill
  fills: [1, 2, 3, 405]
  then: fill_rest
```

A percentage is floored, never less than one share, and the schedule may not sum
to more than 100%.

## Scheduling

Events are spaced `delay_ms` apart — the rule's own, else
`orders.default_delay_ms` — starting one delay after the ack. Nothing is
instant, because a client that only works against instant fills is not
finished.

Orders live in memory for one engine run. They survive a dropped connection:
scheduled fills stay due and fire on the first timer tick after the next Logon.
They do not survive a restart.

Two other settings shape the answers:

```yaml
orders:
  default_delay_ms: 500
  send_pending_acks: false        # send 150=6 / 150=E before cancel / replace acks
  replace_ack_ordstatus: current  # current | replaced
```

## Certification behaviours

The behaviours below exist so a client can certify every row of a venue
checklist against the emulator. **Each one is off in `config/orderecho.yaml`**,
which still answers exactly as 0.8.0 did, and **all of them are on in
`config/orderecho_multi.yaml`**.

| Key | Off (the default) | On |
|---|---|---|
| `orders.time_in_force` | `[day]`: any other `59` is rejected, `58=TimeInForce not supported` | the listed TimeInForce values are accepted |
| `orders.send_pending_new` | the ack is the first report | `150=A 39=A` (Pending New) comes first |
| `orders.valid_accounts` | `[]`: tag 1 is not checked | tag 1, when present, must be one of the list |
| `orders.status_requests` | `35=H` gets a `35=j` | `35=H` is answered |
| `orders.persist` | orders live for one run | open orders survive a restart |
| `orders.send_pending_acks` | (since 0.4.0) | `150=6` / `150=E` before cancel / replace acks |

### Time in force

`orders.time_in_force` lists the values accepted, by name: `day`, `gtc`, `ioc`,
`fok`, `gtx`, `gtd`. A `59` that is absent means Day. The band and the rule
still come first — band, then rule, then TimeInForce — so a `reject` rule
rejects whatever the TimeInForce.

| TimeInForce | What happens |
|---|---|
| Day `0`, GTC `1`, GTX `5` | As a Day order: the rule's schedule runs. A GTC order persists across restarts (with `orders.persist`); GTX is treated as Day, since the emulator has no extended hours. |
| IOC `3` | The rule's *immediate* outcome happens at once — `full_fill`: one full fill; `partial_fill`: its first fill only; `ack_only` or `cancel_after_ack`: nothing — then whatever is left is canceled at once: `150=4 39=4 58=IOC remainder canceled`. |
| FOK `4` | If the rule would fill the whole quantity (`full_fill`, or `partial_fill` with `then: fill_rest`), one immediate full fill. Otherwise `150=4 39=4 58=FOK not fully fillable`, with CumQty 0: never a partial fill. |
| GTD `6` | As Day until it expires, then `150=C 39=C` (Expired), LeavesQty 0, and any fills still scheduled are dropped. It needs `126` ExpireTime (UTC) or `432` ExpireDate (the order lives through that whole UTC day); without either it gets a session Reject `373=1 371=126`. |

IOC and FOK are both acknowledged (`150=0`) before their outcome. With
`orders.ioc_fok_ack: false` the acknowledgement is left out and the fill, cancel
or kill is the first report (after Pending New, if that is on). A market IOC
or FOK fills when its quote arrives; if it has nothing to fill, its cancel does
not wait for the quote.

### Lifecycle states

| Report | How |
|---|---|
| Pending New `150=A 39=A` | Before every accepted order's ack, with `orders.send_pending_new`. |
| Done For Day `150=3 39=3` | `POST /orders/{order_id}/done-for-day`: LeavesQty 0, the order is closed. |
| Expired `150=C 39=C` | A GTD order's expiry, or `POST /orders/{order_id}/expire` for any open order. |
| Too late to cancel `35=9 102=0` | `POST /orders/{order_id}/lock` puts an open order in "fill in progress": a cancel or replace is refused until `POST /orders/{order_id}/unlock`, or the order's next fill or terminal event, clears it. |

The ExecType and OrdStatus values `3`, `A` and `C` are the same in FIX 4.2 and
4.4.

### Accounts

With `orders.valid_accounts: [CERT1, CERT2]`, an order whose tag 1 names an
account not in the list is rejected: `35=8 39=8`, `103=0` in FIX 4.2 (it has no
closer code) and `103=15` (Unknown account(s)) in FIX 4.4, with
`58=Unknown account <x>`. An order **without** tag 1 is not checked. An empty
list means no check at all.

`config/orderecho_multi.yaml` lists `CERT1` and `CERT2`, so a client that sends
no Account is unaffected, and the "invalid Account" case (7.6 in the OrderEcho
agent's suite) is rejected.

### Order status requests

With `orders.status_requests`, an OrderStatusRequest (`35=H`: `11`, `55`, `54`,
optionally `37`) is answered with one ExecutionReport saying where the order
stands, and changes nothing. FIX 4.2 sends `20=3` (Status) with ExecType equal
to the current OrdStatus; FIX 4.4 sends `150=I` (Order Status) and echoes
`790` OrdStatusReqID. The order is found by `37`, its current ClOrdID, or any
ClOrdID it has had. An unknown order gets `37=NONE 39=8 58=Unknown order`.

### Open orders across restarts

With `orders.persist`, every change to an open order is appended to
`<storage.orders_dir>/<session-id>.jsonl` (default `data/orders/`); the file
is compacted on every load and every 200 writes. On startup — or after
`POST /admin/restart` — every open order comes back with its state, its whole
ClOrdID chain and its OrderID, so a cancel by an earlier ClOrdID still works,
and a reused ClOrdID is still a duplicate. Fills that were scheduled resume
relative to the reload. A Day order from an earlier UTC date is expired
(`150=C`) when its session next logs on.

## The shipped rules

Every first letter has a different personality, so a client can be exercised
without editing config. Both the default and the multi-session config ship with
these.

| Symbol | Rule | What you get |
|---|---|---|
| `ZVZZT` | `nasdaq-test-reject` | Immediate reject, "Unknown symbol" |
| `ZWZZT` | `nasdaq-test-hold` | Ack, then nothing — a resting order |
| `A`–`D` | `a-to-d-full` | Ack, then one full fill |
| `E`–`G` | `e-to-g-partial` | Ack, then 40% and 10% fills; the rest rests |
| `H`–`J` | `h-to-j-cancel` | Ack, then an unsolicited cancel |
| `K`–`M` | `k-to-m-reject` | Immediate reject |
| `N`–`P` | `odd-lots` | Fills of 1, 2, 3, 405, then the remainder |
| anything else | `default` | Ack, then one full fill |

`GET /rules` reports the effective rules in match order, including each one's
delay and band setting.

## Pricing

Limit orders fill at their own limit price. Market orders need a quote, and the
ack **never waits** for one: the order is acked immediately with
`price_source: pending`, the lookup runs in the background, and any scheduled
fill holds until the quote lands and then goes out in order.

```yaml
pricing:
  mode: live            # live | static
  provider: yfinance
  cache_seconds: 300
  timeout_sec: 10
  warm_symbols: [AAPL, MSFT, SPY]
  static:
    AAPL: 227.50
    default: 100.00
```

`static` mode reads the table (`static:config`). `live` mode asks yfinance,
caches each symbol for `cache_seconds`, and warms `warm_symbols` in the
background at startup so the first limit order of a run does not meet a cold
cache. Startup never waits for that.

On **any** failure — exception, timeout, missing or non-positive price — live
mode falls back to the static table with source `static:fallback` and a WARNING
in the engine log. It never raises and never blocks the event loop, so a network
problem slows one order rather than stopping the emulator. `yfinance` is
imported lazily, so `static` mode works without it installed.

A manual fill of a still-pending order needs an explicit price; without one it
is a `409 price_pending`.

## Price band

A sell-side fat-finger collar. Limit orders — a `D` with `40=2`, and a `G` that
moves a limit price — are checked against a reference quote **before** they are
acked. Market orders never are.

```yaml
price_band:
  enabled: true
  pct: 10                  # allowed distance from the reference, percent
  mode: aggressive         # aggressive | both
  enforce_on_fallback: false
  lookup_timeout_ms: 3000
  on_no_reference: skip    # skip | reject
```

`aggressive` rejects only the dangerous side — a buy above `ref + band`, a sell
below `ref − band` — because offering to sell far above the market is not a fat
finger, it just will not trade. `both` rejects any limit further than the band
from the reference. A price exactly on the edge passes.

A rejected `D` gets an ExecutionReport with `150=8 39=8 103=3` and a `58`
saying what happened. A rejected `G` gets a `35=9` with `434=2 102=2`, and the
order is left exactly as it was. Every decision is one engine-log line:

```
BAND  W1 BUY LMT 500.00 ref=227.50 (static:config) band=±10% -> REJECT
```

The band is checked before the symbol's rule, so a silly price is refused on its
own terms even when the rule would have rejected the order anyway.

The check is **skipped** — never failed — when the band is off, when no
reference arrives inside `lookup_timeout_ms` and `on_no_reference` is `skip`
(each skip is a WARNING), or when the only quote available came from the
fallback and `enforce_on_fallback` is false. `on_no_reference: reject` refuses
the order instead, with `58=No reference price available`.

Any rule may override the width with `price_band_pct: 2`, or opt out entirely
with `price_band_pct: off`.

## Reject codes

Which code goes on the wire depends on the session's version. Generated from the
profiles.

<!-- generate:reject-reasons -->
