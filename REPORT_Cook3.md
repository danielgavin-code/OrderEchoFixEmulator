# OrderEchoFixEmulator — Cook 3 build report

Built end to end from `SPEC_Cook3.md`. `pytest -q` passes fully: **261 passed**,
up from Cook 2's 206. No git commands were run. Cook 4 was not started.

Before anything else: there was a leftover `orderecho_Main.py` still listening on
9878 from a session started at 22:22 (after the Cook 2 report). I sent it SIGINT,
it logged out and shut down cleanly (exit 0), and both 9878 and 8090 were free
for the rest of the build.

The §3.1 follow-ups paid off immediately — the real run in §3 below priced
`ZWZZT` live at 24.50 from yfinance, where Cook 2's 3-second timeout would have
fallen back to static.

---

## 1. Files created and changed

**New (2)**

| File | What it is |
|---|---|
| `orderecho_ControlApi.py` | The FastAPI app and the uvicorn server that runs it on the engine's own event loop: order, session, injection and read endpoints, the shared error shape, and the per-call engine-log line. |
| `tests/test_ControlApi.py` | 47 tests — all 17 §9 cases plus the error shape, listing/clearing injections, the orders endpoints and the engine-log format. |

**Changed (11)**

| File | Change |
|---|---|
| `orderecho_Transport.py` | `Injector`/`Mutation` and `apply_mutation` (§7); the send path split into `_do_send` → `_write_raw` so injected bytes are logged and recorded correctly; the `/messages` ring buffer; peer tracking; and the control-API hooks (`run_app_actions`, `run_session_actions`, `send_test_request`, `force_disconnect`, `inject_seq_gap`, `duplicate_last`, `recent_messages`). |
| `orderecho_OrderBook.py` | §5 manual actions (`manual_fill`, `manual_fill_rest`, `manual_cancel`, `hold`) and `OrderActionError`; `_apply_fill` takes a price; `_unsolicited_cancel` takes text. |
| `orderecho_Config.py` | `ControlApiConfig`, the loopback check (`is_loopback`), and `pricing.timeout_sec` default 3 → 10 (§3.1). |
| `orderecho_Pricing.py` | `warm_up()` — one background `SPY` lookup at startup, result discarded, failures only logged (§3.1). |
| `orderecho_Codec.py` | `Codec.rebuild()`, which re-encodes from mutated pairs and recomputes 9 and 10 — or deliberately breaks 10. |
| `orderecho_Evidence.py` | `injected` is now a real parameter on every record type rather than a hard-coded `false`. |
| `orderecho_Main.py` | Starts and stops the control API task, kicks off the price warm-up, banner says Cook 3 and prints the API URL. |
| `orderecho_DemoClient.py` | §3.2: `--wait` is a maximum (default 10); `order` returns on Filled/Canceled/Rejected and `cancel-demo` on the cancel ack or reject, both scoped to the client's own ClOrdIDs. |
| `config/orderecho.yaml` | `control_api:` block; `pricing.timeout_sec: 10`. |
| `requirements.txt` | Added `fastapi`, `uvicorn`, `httpx`. |
| `README.md` | Control API section with the endpoint table and the mischief recipes; updated pricing and demo-client text. |
| `tests/fix_TestClient.py` | Fixed a latent bug — see decision 18. |
| `tests/test_Pricing.py`, `tests/test_DemoClient.py` | The §3 tests (warm-up, early return). |

Also changed, cosmetically: the engine log's label column is now 5 wide
throughout (`ORDER`, `FILL`, `CXLREJ`, `API`), which is what the examples in
both Cook 2 §9 and Cook 3 §4 show. Cook 2 shipped one space wider — see
decision 17.

## 2. Test results

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 55%]
........................................................................ [ 82%]
.............................................                            [100%]
261 passed in 19.61s
```

| File | Tests |
|---|---|
| `tests/test_OrderBook.py` | 54 |
| `tests/test_Session.py` | 50 |
| `tests/test_ControlApi.py` | 47 (new) |
| `tests/test_Rules.py` | 36 |
| `tests/test_Pricing.py` | 23 (+4) |
| `tests/test_DemoClient.py` | 13 (+4) |
| `tests/test_Codec.py` | 12 |
| `tests/test_Logging.py` | 9 |
| `tests/test_Integration.py` | 6 |
| `tests/test_OrderIntegration.py` | 6 |
| `tests/test_SeqStore.py` | 5 |
| **Total** | **261** |

Run three times back to back: 18.17s, 18.44s, 18.07s, all 261 passing. Every
integration test starts a full engine — FIX acceptor *and* control API — on its
own pair of random free ports, and drives it with `httpx.AsyncClient` and the
Cook 1 test FIX client together. Nothing touches the network: pricing is static
in tests, and the yfinance warm-up test injects a fake slow module.

## 3. A real run (§10.3)

Engine started with the shipped config (live pricing, control API on 8090), a
demo client holding a `ZWZZT` order open, then curl.

### 3.1 The curl session

```
$ curl -s http://127.0.0.1:8090/status
{
  "state": "ACTIVE",
  "sender_comp_id": "ORDERECHO",
  "target_comp_id": "AGENT",
  "peer": "127.0.0.1:51217",
  "next_out": 3,
  "next_in": 3,
  "heart_bt_int": 30,
  "last_sent": "2026-09-27T02:44:31.010Z",
  "last_received": "2026-09-27T02:44:31.008Z",
  "pending_test_req_id": null,
  "open_orders": 1,
  "run_id": "20260927-024423",
  "evidence_path": "data/evidence/20260927-024423.jsonl",
  "fix_log_path": "logs/fix/ORDERECHO-AGENT_20260927.log",
  "engine_log_path": "logs/engine/orderecho_20260927.log"
}

$ curl -s http://127.0.0.1:8090/orders?status=open
{
  "orders": [
    {
      "order_id": "O-20260927-024423-1",
      "cl_ord_id": "DEMO-1790477068566",
      "symbol": "ZWZZT",
      "side": "1",
      "order_qty": "1000",
      "cum_qty": "0",
      "leaves_qty": "1000",
      "avg_px": "0.0000",
      "ord_status": "0",
      "price_source": "live:yfinance",
      "rule_name": "nasdaq-test-hold"
    }
  ]
}

$ curl -s -X POST http://127.0.0.1:8090/orders/O-20260927-024423-1/fill -H 'content-type: application/json' -d '{"qty": 300}'
{
  "order": {
    "order_id": "O-20260927-024423-1",
    "cl_ord_id": "DEMO-1790477068566",
    "symbol": "ZWZZT",
    "side": "1",
    "order_qty": "1000",
    "cum_qty": "300",
    "leaves_qty": "700",
    "avg_px": "24.5000",
    "ord_status": "1",
    "price_source": "live:yfinance",
    "rule_name": "nasdaq-test-hold"
  },
  "sent": [
    {
      "seq": 3,
      "msg_type": "8",
      "raw": "8=FIX.4.2|9=234|35=8|49=ORDERECHO|56=AGENT|34=3|52=20260927-02:44:31.425|37=O-20260927-024423-1|11=DEMO-1790477068566|17=E-20260927-024423-2|20=0|150=1|39=1|55=ZWZZT|54=1|38=1000|40=1|32=300|31=24.50|151=700|14=300|6=24.5000|60=20260927-02:44:31.424|10=230|",
      "injected": false
    }
  ]
}

$ curl -s -X POST http://127.0.0.1:8090/inject/next -H 'content-type: application/json' -d '{"msg_type": "8", "set": {"9999": "FOO"}}'
{
  "queued": {
    "id": 1,
    "msg_type": "8",
    "set": {
      "9999": "FOO"
    },
    "remove": [],
    "corrupt_checksum": false,
    "count": 1,
    "remaining": 1
  }
}

$ curl -s -X POST http://127.0.0.1:8090/orders/O-20260927-024423-1/fill-rest -H 'content-type: application/json' -d {}
{
  "order": {
    "order_id": "O-20260927-024423-1",
    "cl_ord_id": "DEMO-1790477068566",
    "symbol": "ZWZZT",
    "side": "1",
    "order_qty": "1000",
    "cum_qty": "1000",
    "leaves_qty": "0",
    "avg_px": "24.5000",
    "ord_status": "2",
    "price_source": "live:yfinance",
    "rule_name": "nasdaq-test-hold"
  },
  "sent": [
    {
      "seq": 4,
      "msg_type": "8",
      "raw": "8=FIX.4.2|9=242|35=8|49=ORDERECHO|56=AGENT|34=4|52=20260927-02:44:31.467|37=O-20260927-024423-1|11=DEMO-1790477068566|17=E-20260927-024423-3|20=0|150=2|39=2|55=ZWZZT|54=1|38=1000|40=1|32=700|31=24.50|151=0|14=1000|6=24.5000|60=20260927-02:44:31.466|9999=FOO|10=198|",
      "injected": true
    }
  ]
}
```

`9999=FOO` is on the wire, the frame is well formed (BodyLength 242 and
CheckSum 198 both recomputed after the mutation), and the response says
`"injected": true`.

`GET /messages?limit=10` returned the eight messages of the run, newest last;
the first two are shown here for length, and the full set is the FIX log in §3.3:

```
$ curl -s http://127.0.0.1:8090/messages?limit=10
{
  "messages": [
    {
      "ts": "2026-09-27T02:44:28.560Z",
      "kind": "in",
      "seq": 1,
      "msg_type": "A",
      "raw": "8=FIX.4.2|9=75|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260927-02:44:28.326|98=0|108=30|141=Y|10=066|",
      "injected": false
    },
    ...
  ]
}
```

### 3.2 Demo client

It never used its 60-second ceiling: the `fill-rest` closed the order, and §3.2's
early return brought the client home.

```
$ .venv/bin/python orderecho_DemoClient.py order ZWZZT 1000 buy mkt --wait 60   [exit 0]
Connecting to 127.0.0.1:9878 as AGENT
<- Logon                 HeartBtInt=30
-> NewOrderSingle      ZWZZT 1000 BUY MKT  11=DEMO-1790477068566
<- ExecutionReport       ZWZZT exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1790477068566 37=O-20260927-024423-1
<- ExecutionReport       ZWZZT exec=PARTIAL_FILL status=PARTIALLY_FILLED  last=300@24.50  cum=300 leaves=700 avg=24.5000  11=DEMO-1790477068566 37=O-20260927-024423-1
<- ExecutionReport       ZWZZT exec=FILL status=FILLED  last=700@24.50  cum=1000 leaves=0 avg=24.5000  11=DEMO-1790477068566 37=O-20260927-024423-1
<- Logout                "Logout acknowledged"
```

### 3.3 FIX log

```
20260927-02:44:28.327 IN   seq=1    35=A  8=FIX.4.2|9=75|35=A|49=AGENT|56=ORDERECHO|34=1|52=20260927-02:44:28.326|98=0|108=30|141=Y|10=066|
20260927-02:44:28.566 OUT  seq=1    35=A  8=FIX.4.2|9=75|35=A|49=ORDERECHO|56=AGENT|34=1|52=20260927-02:44:28.566|98=0|108=30|141=Y|10=072|
20260927-02:44:28.567 IN   seq=2    35=D  8=FIX.4.2|9=136|35=D|49=AGENT|56=ORDERECHO|34=2|52=20260927-02:44:28.566|11=DEMO-1790477068566|21=1|55=ZWZZT|54=1|38=1000|40=1|60=20260927-02:44:28.566|10=004|
20260927-02:44:31.011 OUT  seq=2    35=8  8=FIX.4.2|9=229|35=8|49=ORDERECHO|56=AGENT|34=2|52=20260927-02:44:31.010|37=O-20260927-024423-1|11=DEMO-1790477068566|17=E-20260927-024423-1|20=0|150=0|39=0|55=ZWZZT|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20260927-02:44:31.009|10=201|
20260927-02:44:31.425 OUT  seq=3    35=8  8=FIX.4.2|9=234|35=8|49=ORDERECHO|56=AGENT|34=3|52=20260927-02:44:31.425|37=O-20260927-024423-1|11=DEMO-1790477068566|17=E-20260927-024423-2|20=0|150=1|39=1|55=ZWZZT|54=1|38=1000|40=1|32=300|31=24.50|151=700|14=300|6=24.5000|60=20260927-02:44:31.424|10=230|
20260927-02:44:31.467 OUT  seq=4    35=8  8=FIX.4.2|9=242|35=8|49=ORDERECHO|56=AGENT|34=4|52=20260927-02:44:31.467|37=O-20260927-024423-1|11=DEMO-1790477068566|17=E-20260927-024423-3|20=0|150=2|39=2|55=ZWZZT|54=1|38=1000|40=1|32=700|31=24.50|151=0|14=1000|6=24.5000|60=20260927-02:44:31.466|9999=FOO|10=198|  # injected: set 9999=FOO
20260927-02:44:31.468 IN   seq=3    35=5  8=FIX.4.2|9=77|35=5|49=AGENT|56=ORDERECHO|34=3|52=20260927-02:44:31.468|58=Demo client done|10=138|
20260927-02:44:31.470 OUT  seq=5    35=5  8=FIX.4.2|9=80|35=5|49=ORDERECHO|56=AGENT|34=5|52=20260927-02:44:31.470|58=Logout acknowledged|10=023|
```

The injected line is the only one carrying a `#` comment, and the raw message is
still last on the line, so it can be pasted into a decoder like any other.

### 3.4 Engine log (API and order lines)

```
20260927-02:44:23.338 INFO    session  ORDERECHO-AGENT  Control API listening on http://127.0.0.1:8090 (docs at /docs)
20260927-02:44:31.010 INFO    session  ORDERECHO-AGENT  rule matched: ZWZZT matched rule 'nasdaq-test-hold' (ack_only)
20260927-02:44:31.010 INFO    session  ORDERECHO-AGENT  price resolved: ZWZZT 24.50 (live:yfinance)
20260927-02:44:31.011 INFO    session  ORDERECHO-AGENT  ORDER O-20260927-024423-1 ZWZZT BUY 1000 MKT px=24.50 (live:yfinance) rule=nasdaq-test-hold -> NEW
20260927-02:44:31.389 INFO    session  ORDERECHO-AGENT  API   GET /status -> 200
20260927-02:44:31.405 INFO    session  ORDERECHO-AGENT  API   GET /orders -> 200
20260927-02:44:31.425 INFO    session  ORDERECHO-AGENT  FILL  O-20260927-024423-1 300 @ 24.50 cum=300 leaves=700 avg=24.5000 -> PARTIALLY_FILLED
20260927-02:44:31.425 INFO    session  ORDERECHO-AGENT  API   POST /orders/O-20260927-024423-1/fill qty=300 -> 200
20260927-02:44:31.450 INFO    session  ORDERECHO-AGENT  API   POST /inject/next msg_type=8 count=1 -> 200
20260927-02:44:31.467 WARNING session  ORDERECHO-AGENT  Injected into outbound 8 seq=4: set 9999=FOO
20260927-02:44:31.467 INFO    session  ORDERECHO-AGENT  FILL  O-20260927-024423-1 700 @ 24.50 cum=1000 leaves=0 avg=24.5000 -> FILLED
20260927-02:44:31.468 INFO    session  ORDERECHO-AGENT  API   POST /orders/O-20260927-024423-1/fill-rest -> 200
20260927-02:44:31.485 INFO    session  ORDERECHO-AGENT  API   GET /messages -> 200
```

The engine exited 0 on SIGINT afterwards; 9878 and 8090 are both free and no
`orderecho_*` processes remain.

---

# 4. Decisions I made

## The API's seam with the engine

**1. The API holds a `Transport` and calls back through it.** §4 says handlers
call the engine directly; `Transport` is the only object that already owns the
session, the order book, the logs and the socket, so `build_app(transport)`
keeps the seam to one object instead of five.

**2. Order and session actions run through three new transport methods** —
`run_app_actions`, `run_session_actions` and `send_test_request` — which convert
app actions via `Session._run_app` and execute them through the same
`_run_actions` path a timer tick uses. That is what makes an API-driven fill
indistinguishable from a scheduled one in the evidence and the logs.

**3. `_do_send` was split into `_do_send` and `_write_raw`.** Injection,
`duplicate-last` and ordinary sends all need "put these bytes on the wire and
record them everywhere"; only ordinary sends need "encode from an action
first". Splitting it stopped the three paths from drifting apart.

**4. `OrderActionError` carries a code, not an HTTP status.** The pure core
raises `not_found` / `order_closed` / `invalid_request` and the API maps those
to 404 / 409 / 400, so `orderecho_OrderBook.py` still imports nothing from
FastAPI.

**5. `Injector`, `Mutation` and `apply_mutation` live in
`orderecho_Transport.py`.** §7 says injection is implemented in the
session/transport send path, and §1's file list adds only
`orderecho_ControlApi.py`, so they went where the send path is rather than into
a module the spec does not name.

**6. uvicorn runs with its signal handling disabled.** `_NoSignalServer`
overrides `capture_signals` to a no-op. uvicorn 0.54 installs `signal.signal`
handlers inside `serve()`, which would have replaced the `loop.add_signal_handler`
hooks `orderecho_Main.py` installs — Ctrl+C would have stopped the API and left
the acceptor running. The engine keeps sole ownership of SIGINT/SIGTERM.

## Errors and responses

**7. FastAPI's validation errors are remapped to the §4 error shape.** A bad
body would otherwise come back as a 422 with FastAPI's own envelope; a handler
turns it into `400 {"error": "invalid_request", "detail": ...}` so the API has
exactly one error shape. §5 also wants a bad `qty` to be 400, not 422.

**8. Message summaries carry a fourth key, `injected`.** §5 specifies
`{"seq", "msg_type", "raw"}`; the extra boolean is what tells a caller whether
the bytes it just asked for were mutated on the way out, which seemed worth more
than strict minimalism. Same shape is used by `/messages`.

**9. A missing JSON body is allowed wherever every field has a default** —
`cancel`, `hold`, `fill-rest`, `logout`, `duplicate-last`. `curl -X POST` with no
`-d` is the natural way to call those.

**10. `/session/reset-seqnums` resets the store, not a live session object.**
It only runs while DISCONNECTED, and the next connection builds a fresh
`Session` that loads from the store, so there is nothing in memory to reset.

**11. `/orders/{id}` finds its ExecutionReports by filtering the ring buffer**
for outbound `35=8` containing `|37=<order_id>|`, rather than keeping a second
index on the order. It inherits the buffer's 1000-message horizon, which is the
same horizon `/messages` has.

**12. The ring buffer records in/out messages only,** not discarded inbound
frames. §8 says "in/out messages", and a discarded frame has no sequence number
or MsgType to report. Discards remain in the evidence log and the FIX log.

## Injection

**13. One mutation per outbound message, FIFO.** §7 describes queueing "a
mutation"; when several are pending, the first queued one that matches the
MsgType is applied and consumed. Chaining several onto one message would make
the resulting bytes much harder to reason about from the evidence.

**14. `set` on an existing tag replaces it in place, keeping field order;** a new
tag is appended. That way `set {"58": "..."}` overwrites the real Text rather
than producing a message with the tag twice.

**15. `/inject/seq-gap` and `/session/disconnect` record an evidence *event*
with `injected: true`.** §7's rule is written for outbound messages, and neither
of these sends one — but both are deliberate mischief, and the whole point of
the flag is that nothing done on purpose looks like a bug later.

**16. `duplicate-last` resends the last message from this connection and does
not consume a sequence number.** With `poss_dup`, `43=Y` and `122=<original 52>`
are inserted straight after tag 52, which is where Cook 1 §6 puts them.

## Cosmetic

**17. The engine log's label column is now 5 wide across `ORDER`, `FILL`,
`CXLREJ` and `API`.** Cook 3 §4's example (`API   POST …`) and Cook 2 §9's
examples (`ORDER O-…`, `FILL  O-…`) all use a 5-wide label plus one space; Cook 2
shipped one space wider than that. Bringing `API` into line meant bringing the
others with it, so the columns still align. Purely cosmetic, and it makes the
logs match both specs exactly.

## Tests

**18. Fixed a latent bug in `tests/fix_TestClient.py`.** `logon(reset=True)`
passed an explicit `seq=1`, and `send()` only advances its counter when the seq
is *not* given — so the message after a resetting Logon reused sequence number 1
and the emulator (correctly) logged the client out. No existing test had noticed,
because no existing test sent anything after a resetting Logon. It now rewinds
the counter and lets `send()` allocate. This is a fix to a test helper, not a
weakened assertion.

**19. One of my own new tests was wrong and I replaced it, not the code.**
`test_control_api_refuses_a_non_loopback_host` assumed the `Config` *dataclass*
validates its host; it does not — `load_config` and `ControlApiServer.__init__`
do, and both already had passing tests. I replaced it with parametrized tests of
`is_loopback` itself, so §9.17 is now covered at all three levels: the gate, the
config file, and the server.

**20. The §9.4 hold test uses a deliberately slow rule.** The E–G rule in the API
test config has `delay_ms: 1000` so that `POST /hold` reliably lands before the
first scheduled fill would fire; the test then waits 1.5x that delay and asserts
nothing arrived, and separately that the order has no scheduled events and is
still open.

**21. Kept every Cook 2 conservative choice,** per §3.3 — including the cancel
lookup spanning closed orders, rejecting a market order with no quote, and the
`103`/`102` split for duplicate ClOrdIDs.

---

# 5. Questions for you

1. **Should `/inject/next` be able to stack several mutations onto one
   message?** Today the first matching mutation wins and is consumed
   (decision 13). Stacking would let you, say, remove `60` *and* corrupt the
   checksum in one go; right now that needs `corrupt_checksum` plus `remove` in
   a single call, which does work, but two separately queued mutations will not
   both hit the same message.

2. **`/session/disconnect` closes the socket but leaves the order book intact,**
   so a reconnecting client will see ExecutionReports for orders it placed on the
   previous connection (Cook 2's per-run order book). That is deliberate and
   realistic, but combined with `reset-seqnums` it is an easy way to confuse a
   client under test. Worth a `?drop_orders=true` option, or leave it?

3. **Should `/status` expose the pending injection queue** (it is on `GET
   /inject` today) so a single call tells you everything about the engine's
   current state? I kept them separate because `/status` is documented as session
   state.

4. **`pricing.timeout_sec: 10` plus the SPY warm-up worked** — the real run
   priced ZWZZT live. Should the warm-up symbol be configurable, or warm a list
   of symbols the rules are likely to see?

5. **Carried over from Cook 2, still open:** should working orders be
   re-reported on Logon (a drop-copy), and should `orders.send_pending_acks` /
   `replace_ack_ordstatus` become runtime-flippable through this API now that one
   exists?
