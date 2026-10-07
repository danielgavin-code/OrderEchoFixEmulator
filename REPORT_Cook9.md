# REPORT — Cook 9 (0.9.0, `cook9`)

The gaps the Go agent's certification run found, closed: TimeInForce beyond
Day, Pending New / Done For Day / Expired / too-late-to-cancel, account
validation, open orders across restarts with `POST /admin/restart`,
OrderStatusRequest, `379` on a business reject, a gap queue with a
closed-range ResendRequest, a negative price cache, and the four Python
timeline-check fixes.

**Every new behaviour is off in `config/orderecho.yaml`** (it answers exactly as
0.8.0 did — the golden fixtures are byte-identical) **and on in
`config/orderecho_multi.yaml`**.

Ground rules kept: no git; no spec-derived test weakened; golden fixtures
untouched; `../OrderEcho` read, never modified. Your running emulator (pid
14395, ports 9878/9879/8090) and agent service were left alone throughout; the
real run used a private copy on other ports (section 3).

> Note on the rules: OrderEcho's `CLAUDE.md` says "The Python emulator lives at
> ../OrderEchoFixEmulator … never modify it". This cook modifies the emulator
> because your instruction for it explicitly asked for that; I treated the
> newer, explicit instruction as overriding. See Questions, Q9.

---

## 1. Files

All paths in `OrderEchoFixEmulator/`.

### New

| File | What |
|---|---|
| `orderecho_OrderStore.py` | Open-order persistence: append-only JSONL per session (`{"v":1,"op":"upsert"\|"close",...}`), compacted atomically on load and every 200 writes, torn last line tolerated, directory created lazily. |
| `tests/test_TimeInForce.py` | 48 tests — spec 3: IOC/FOK/GTC/GTX/GTD per version, GTD session rejects, the Day-only default. |
| `tests/test_Lifecycle.py` | 28 tests — spec 4: Pending New, Done For Day, Expire, lock/unlock and too-late-to-cancel, valid accounts (103=0 / 103=15), book-level and end-to-end through the API and FIX. |
| `tests/test_Persistence.py` | 10 tests — spec 5: the store, book save/restore (chains, OrderIDs, rescheduled fills, duplicate ClOrdIDs after restore), Day expiry on the next logon, `POST /admin/restart` in-process (GTC survives, fills resume, cancel by original ClOrdID works), and a real **process** restart of `orderecho_Main.py` with a status request and a cancel afterwards. |
| `tests/test_ProtocolFixes.py` | 17 tests — spec 6: `379` gated and ungated (and absent without an ID, the 7.8 shape), the gap queue (closed range, holding, in-order release, TestRequest behind a gap, second hole, Logon gap, overflow → Logout, off = unchanged, end-to-end over TCP), the negative price cache (expiry, only the first order waits, engine wiring). |
| `tests/test_CheckFixes.py` | 11 tests — spec 7: one regression per Python check fix, each in the shape of the agent's log that exposed it. |

### Changed

| File | Change |
|---|---|
| `orderecho_Version.py` | 0.9.0 / `cook9`. |
| `orderecho_Config.py` | New keys (section 5, D1): `orders.time_in_force`, `send_pending_new`, `valid_accounts`, `persist`, `status_requests`; `session.gap_queue`, `business_reject_ref_id`; `storage.orders_dir`; `pricing.negative_cache_seconds`. All in the schema, so the generated config reference lists them. |
| `orderecho_FixVersion.py` | Report kinds Pending New (A), Done For Day (3), Expired (C), Status (4.2 `20=3` + ExecType = OrdStatus; 4.4 `150=I` + `790`). Reason "unknown account": 4.2 `103=0`, 4.4 `103=15`. Required tags for `35=H`. |
| `orderecho_OrderBook.py` | TIF handling, GTD expiry, lifecycle actions, account check, persistence hooks, `serialize`/`restore`, `on_session_logon`, OrderStatusRequest. |
| `orderecho_Session.py` | `379` on `35=j`; the gap queue; routes `35=H` to the book when status requests are on; a logon hook for the book. |
| `orderecho_Transport.py` | Order store per session; restore at startup; `restart()` (logout all, close listeners, reload config and state, listen again on the same ports). |
| `orderecho_ControlApi.py` | `POST /orders/{order_id}/done-for-day`, `/expire`, `/lock`, `/unlock`; `POST /admin/restart`. |
| `orderecho_Pricing.py` | Negative cache; `resolve_quote` answers from it at once. |
| `orderecho_Timeline.py` | Fix 1 (reject direction) and fix 4 (duplicate-reject chain split, a port of the agent's `splitDuplicates`). |
| `orderecho_LogParse.py` | Fix 2 (frame by BodyLength, not the first `10=`) and fix 3 (framing verified over the bytes as written: latin-1 or UTF-8). |
| `orderecho_DemoClient.py` | `--tif`, `--expire-time`, `--expire-date`, `--account`; `query` subcommand; session-mode `order … tif= expire= account=` and `query`. |
| `orderecho_BuildDocs.py` | The sample ExecutionReport includes `790`, so the generated field table lists it. |
| `dictionaries/overlay.json` | Names for tag 790 (OrdStatusReqID) and `35=H` (OrderStatusRequest), which the base dictionary lacked. |
| `config/orderecho_multi.yaml` | Everything on (section 5, D2). `config/orderecho.yaml` is **unchanged**. |
| `docs/src/*.md` (7 pages), `docs/site/*` | Prose for all of the above; changelog 0.9.0; site regenerated. |
| `tests/isolation.py` | `storage.orders_dir` added to the isolated storage keys. |
| `tests/test_CarryOvers.py`, `tests/test_Viewer.py` | Version literals 0.9.0/`cook9`; `cook9`/"Cook 9" added to the build-name needles. |
| `tests/test_Timeline.py` | The `line()` helper now sets CompIDs by direction (D6). No assertion changed. |
| `tests/test_Docs.py` | The demo-client subcommand check reads the real parser instead of a hard-coded list, and requires `query` (D7). |

Golden fixtures: 20 files in `tests/golden/fix42|fix44`, last modified
Sep 27, none newer than the spec; `shasum` of all 20 combined:
`38cba85dc4c0a2a0045b88d9aebb725e1b1c520c`.

---

## 2. `pytest -q`

The suite was run twice:
- **In a scratch mirror of the repo:** an rsync of the repo excluding `data/`, `logs/` and `.venv`, run with the repo's `.venv`. This is the clean result.
- **In the repo itself.**

The in-repo run reports one error from the isolation guard. The guard fails the run if `data/` or `logs/` change, and your live emulator (pid 14395) writes to them. No test wrote there.

Mirror:

```
........................................................................ [ 96%]
............................                                             [100%]
820 passed in 122.31s (0:02:02)
```

In the repo (the tail as captured; the four modified files are written by
your live emulator, pid 14395 — the same 820 tests pass):

```
...
  modified data/seqnums/strict-broker.json
  modified logs/fix/agent42_20261004.log
  modified logs/fix/agent44_20261004.log
  modified logs/fix/strict-broker_20261004.log
Build the config with tests/isolation.isolated_config so every storage path lives under tmp_path (spec 3.5).
=========================== short test summary info ============================
ERROR tests/test_Viewer.py::test_the_banner_shows_the_guide_url - Failed: tes...
820 passed, 1 error in 120.84s (0:02:00)
```

---

## 3. Real run, `config/orderecho_multi.yaml`

To leave your emulator alone, I ran a private copy of the repo in my scratch
directory. Its config was `orderecho_multi.yaml` with **only the three ports changed** (9878→19878,
9879→19879, 8090→18090):

```
6c6
<   fix_port: 9878                # sessions without their own port use this
---
>   fix_port: 19878               # sessions without their own port use this
96c96
<     port: 9879
---
>     port: 19879
116c116
<   port: 8090
---
>   port: 18090
```

Startup:

```
20261004-01:13:47.376 INFO    session  engine  Acceptor listening on 127.0.0.1:19878 for agent42, agent44 (run_id=20261004-011347)
20261004-01:13:47.377 INFO    session  engine  Acceptor listening on 127.0.0.1:19879 for strict-broker (run_id=20261004-011347)
20261004-01:13:47.498 INFO    session  engine  Control API listening on http://127.0.0.1:18090 (docs at /docs)
OrderEchoFixEmulator 0.9.0 (cook9) - 3 FIX sessions
  config         : config/orderecho_c9run.yaml
  evidence file  : data/evidence/20261004-011347.jsonl
  fix log        : logs/fix
  engine log     : logs/engine

  session          version  route                      port   rules  band
  ---------------- -------- -------------------------- ------ ------ ----
  agent42          FIX.4.2  ORDERECHO -> AGENT         19878  8      10%
  agent44          FIX.4.4  ORDERECHO -> AGENT         19878  8      10%
  strict-broker    FIX.4.2  STRICTBRK -> AGENT         19879  1      2%

  control api    : http://127.0.0.1:18090  (docs at /docs)
  guide          : http://127.0.0.1:18090/guide
  log viewer     : http://127.0.0.1:18090/viewer
  Ctrl+C to shut down.
20261004-01:13:47.499 INFO    session  engine  Startup: version=0.9.0 build=cook9 config=config/orderecho_c9run.yaml sessions=[agent42=FIX.4.2@127.0.0.1:19878; agent44=FIX.4.4@127.0.0.1:19878; strict-broker=FIX.4.2@127.0.0.1:19879] evidence=data/evidence/20261004-011347.jsonl
20261004-01:13:49.373 INFO    session  engine  Price warm-up: AAPL 333.69 (live:yfinance)
20261004-01:13:50.299 INFO    session  engine  Price warm-up: MSFT 517.53 (live:yfinance)
```

### IOC, FOK (Pending New on every order)

```
$ orderecho_DemoClient.py --session agent42 order EFG 1000 buy mkt --tif ioc --account CERT1
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      EFG 1000 BUY MKT IOC account=CERT1  11=DEMO-1791076434055-1
<- ExecutionReport       EFG exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076434055-1 37=O-20261004-011347-1
<- ExecutionReport       EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076434055-1 37=O-20261004-011347-1
<- ExecutionReport       EFG exec=PARTIAL_FILL status=PARTIALLY_FILLED  last=400@121.08  cum=400 leaves=600 avg=121.0800  11=DEMO-1791076434055-1 37=O-20261004-011347-1
<- ExecutionReport       EFG exec=CANCELED status=CANCELED  cum=400 leaves=0 avg=121.0800  11=DEMO-1791076434055-1 37=O-20261004-011347-1  "IOC remainder canceled"
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent42 order EFG 1000 buy mkt --tif fok --account CERT1
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      EFG 1000 BUY MKT FOK account=CERT1  11=DEMO-1791076435419-1
<- ExecutionReport       EFG exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076435419-1 37=O-20261004-011347-2
<- ExecutionReport       EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076435419-1 37=O-20261004-011347-2
<- ExecutionReport       EFG exec=CANCELED status=CANCELED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076435419-1 37=O-20261004-011347-2  "FOK not fully fillable"
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent42 order AAPL 100 buy mkt --tif fok --account CERT1
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      AAPL 100 BUY MKT FOK account=CERT1  11=DEMO-1791076435610-1
<- ExecutionReport       AAPL exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076435610-1 37=O-20261004-011347-3
<- ExecutionReport       AAPL exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076435610-1 37=O-20261004-011347-3
<- ExecutionReport       AAPL exec=FILL status=FILLED  last=100@333.69  cum=100 leaves=0 avg=333.6900  11=DEMO-1791076435610-1 37=O-20261004-011347-3
<- Logout                "Logout acknowledged"
```

### GTD: a missing expiry; an account check in both versions; expiry

The first GTD attempt used a limit of 99.00 and was rejected by the price band, which is configured behaviour: ZWZZT's live reference is 10.12. The run below uses a limit of 10.00.

```
(now UTC: 20261004-01:14:04)
$ orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 99.00 --tif gtd --expire-time 20261004-01:14:16 --account CERT1 --wait 25
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      ZWZZT 100 BUY LMT 99.00 GTD until 20261004-01:14:16 account=CERT1  11=DEMO-1791076444197-1
<- ExecutionReport       ZWZZT exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076444197-1 37=O-20261004-011347-4  103=3  "Limit 99.00 outside 10% band of ref 10.12 (live:yfinance)"
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 99.00 --tif gtd --account CERT1 --wait 3
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      ZWZZT 100 BUY LMT 99.00 GTD account=CERT1  11=DEMO-1791076445168-1
<- Reject                "Required tag missing: 126 (GTD needs ExpireTime 126 or ExpireDate 432)"
.. still waiting for a terminal order state after 3s; giving up
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent42 order AAPL 100 buy mkt --account NOPE
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      AAPL 100 BUY MKT account=NOPE  11=DEMO-1791076448358-1
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076448358-1 37=O-20261004-011347-5  103=0  "Unknown account NOPE"
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent44 order AAPL 100 buy mkt --account NOPE
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.4] session=agent44
<- Logon                 HeartBtInt=30
-> NewOrderSingle      AAPL 100 BUY MKT account=NOPE  11=DEMO-1791076448556-1
<- ExecutionReport       AAPL exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076448556-1 37=O-20261004-011347-6  103=15  "Unknown account NOPE"
<- Logout                "Logout acknowledged"
```

```
(now UTC: 20261004-01:14:15.000)
$ orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 10.00 --tif gtd --expire-time 20261004-01:14:27 --account CERT1 --wait 25
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      ZWZZT 100 BUY LMT 10.00 GTD until 20261004-01:14:27 account=CERT1  11=DEMO-1791076456055-1
<- ExecutionReport       ZWZZT exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076456055-1 37=O-20261004-011347-7
<- ExecutionReport       ZWZZT exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076456055-1 37=O-20261004-011347-7
<- ExecutionReport       ZWZZT exec=EXPIRED status=EXPIRED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076456055-1 37=O-20261004-011347-7  "GTD order expired"
<- Logout                "Logout acknowledged"

(finished UTC: 20261004-01:14:27)
```

### `/admin/restart` with an open GTC order, and OrderStatusRequest

```
$ orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 10.00 --tif gtc --account CERT1 --wait 2
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> NewOrderSingle      ZWZZT 100 BUY LMT 10.00 GTC account=CERT1  11=DEMO-1791076490135-1
<- ExecutionReport       ZWZZT exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076490135-1 37=O-20261004-011347-8
<- ExecutionReport       ZWZZT exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076490135-1 37=O-20261004-011347-8
.. still waiting for a terminal order state after 2s; giving up
<- Logout                "Logout acknowledged"
--- before restart
$ orderecho_DemoClient.py --session agent42 query DEMO-1791076490135-1 ZWZZT buy
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> OrderStatusRequest  11=DEMO-1791076490135-1 ZWZZT
<- ExecutionReport       ZWZZT exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076490135-1 37=O-20261004-011347-8  (status answer, 20=3)
<- Logout                "Logout acknowledged"

$ wc -l data/orders/agent42.jsonl
      20 data/orders/agent42.jsonl

$ curl -sPOST localhost:18090/admin/restart
{"restarted":true,"seconds":0.015,"ports":[19878,19879],"sessions":["agent42","agent44","strict-broker"],"open_orders":{"agent42":1,"agent44":0,"strict-broker":0}}

--- after restart
$ curl -s 'localhost:18090/orders?status=open'
{'order_id': 'O-20261004-011347-8', 'cl_ord_id': 'DEMO-1791076490135-1', 'leaves_qty': '100'}

$ orderecho_DemoClient.py --session agent42 query DEMO-1791076490135-1 ZWZZT buy
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> OrderStatusRequest  11=DEMO-1791076490135-1 ZWZZT
<- ExecutionReport       ZWZZT exec=NEW status=NEW  cum=0 leaves=100 avg=0.0000  11=DEMO-1791076490135-1 37=O-20261004-011347-8  (status answer, 20=3)
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent44 query DEMO-1791076490135-1 ZWZZT buy
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.4] session=agent44
<- Logon                 HeartBtInt=30
-> OrderStatusRequest  11=DEMO-1791076490135-1 ZWZZT
<- ExecutionReport       ZWZZT exec=ORDER_STATUS status=REJECTED  cum=0 leaves=0 avg=0.0000  11=DEMO-1791076490135-1 37=NONE  "Unknown order"
<- Logout                "Logout acknowledged"

$ orderecho_DemoClient.py --session agent42 query NO-SUCH-ORDER ZWZZT buy
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.2] session=agent42
<- Logon                 HeartBtInt=30
-> OrderStatusRequest  11=NO-SUCH-ORDER ZWZZT
<- ExecutionReport       ZWZZT exec=REJECTED status=REJECTED  cum=0 leaves=0 avg=0.0000  11=NO-SUCH-ORDER 37=NONE  (status answer, 20=3)  "Unknown order"
<- Logout                "Logout acknowledged"
```

The 4.4 "Unknown order" is correct: the order belongs to session `agent42`,
and sessions do not see each other's orders. A 4.4 status answer for a live
order:

```
$ orderecho_DemoClient.py --session agent44 order EFG 1000 buy mkt --tif gtc --account CERT1 --wait 3
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.4] session=agent44
<- Logon                 HeartBtInt=30
-> NewOrderSingle      EFG 1000 BUY MKT GTC account=CERT1  11=DEMO-1791076504103-1
<- ExecutionReport       EFG exec=PENDING_NEW status=PENDING_NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076504103-1 37=O-20261004-011347-9
<- ExecutionReport       EFG exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000  11=DEMO-1791076504103-1 37=O-20261004-011347-9
<- ExecutionReport       EFG exec=TRADE status=PARTIALLY_FILLED  last=400@121.08  cum=400 leaves=600 avg=121.0800  11=DEMO-1791076504103-1 37=O-20261004-011347-9
<- ExecutionReport       EFG exec=TRADE status=PARTIALLY_FILLED  last=100@121.08  cum=500 leaves=500 avg=121.0800  11=DEMO-1791076504103-1 37=O-20261004-011347-9
.. still waiting for a terminal order state after 3s; giving up
<- Logout                "Logout acknowledged"
$ orderecho_DemoClient.py --session agent44 query DEMO-1791076504103-1 EFG buy
Connecting to 127.0.0.1:19878 as AGENT [FIX.4.4] session=agent44
<- Logon                 HeartBtInt=30
-> OrderStatusRequest  11=DEMO-1791076504103-1 EFG
<- ExecutionReport       EFG exec=ORDER_STATUS status=PARTIALLY_FILLED  cum=500 leaves=500 avg=121.0800  11=DEMO-1791076504103-1 37=O-20261004-011347-9
<- Logout                "Logout acknowledged"

$ curl -sX POST localhost:18090/orders/O-20261004-011347-8/cancel
{"error":"session_not_active","detail":"Session agent42 is DISCONNECTED, not ACTIVE"}
```

The final cancel of the GTC order through the API returned `409 session_not_active`. That is existing behaviour: an unsolicited cancel needs a logged-on session. The order stayed open in the scratch store. A cancel by its original ClOrdID after a restart, sent over FIX, is covered by `test_admin_restart_keeps_open_orders_and_their_schedules` and `test_open_orders_survive_a_process_restart`.

### FIX log and engine log lines

```
### agent42 (FIX.4.2)
20261004-01:13:54.055 IN   seq=2    35=D  8=FIX.4.2|9=149|35=D|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:13:54.055|1=CERT1|11=DEMO-1791076434055-1|21=1|55=EFG|54=1|38=1000|40=1|60=20261004-01:13:54.055|59=3|10=234|
20261004-01:13:54.097 OUT  seq=2    35=8  8=FIX.4.2|9=237|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:13:54.096|37=O-20261004-011347-1|11=DEMO-1791076434055-1|17=E-20261004-011347-1|20=0|150=A|39=A|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20261004-01:13:54.056|10=255|
20261004-01:13:54.097 OUT  seq=3    35=8  8=FIX.4.2|9=237|35=8|49=ORDERECHO|56=AGENT|34=3|52=20261004-01:13:54.097|37=O-20261004-011347-1|11=DEMO-1791076434055-1|17=E-20261004-011347-2|20=0|150=0|39=0|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20261004-01:13:54.057|10=225|
20261004-01:13:55.232 OUT  seq=4    35=8  8=FIX.4.2|9=244|35=8|49=ORDERECHO|56=AGENT|34=4|52=20261004-01:13:55.232|37=O-20261004-011347-1|11=DEMO-1791076434055-1|17=E-20261004-011347-3|20=0|150=1|39=1|1=CERT1|55=EFG|54=1|38=1000|40=1|32=400|31=121.08|151=600|14=400|6=121.0800|60=20261004-01:13:55.227|10=080|
20261004-01:13:55.233 OUT  seq=5    35=8  8=FIX.4.2|9=264|35=8|49=ORDERECHO|56=AGENT|34=5|52=20261004-01:13:55.232|37=O-20261004-011347-1|11=DEMO-1791076434055-1|17=E-20261004-011347-4|20=0|150=4|39=4|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=0|14=400|6=121.0800|60=20261004-01:13:55.228|58=IOC remainder canceled|10=209|
20261004-01:13:55.419 IN   seq=2    35=D  8=FIX.4.2|9=149|35=D|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:13:55.419|1=CERT1|11=DEMO-1791076435419-1|21=1|55=EFG|54=1|38=1000|40=1|60=20261004-01:13:55.419|59=4|10=250|
20261004-01:13:55.424 OUT  seq=2    35=8  8=FIX.4.2|9=237|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:13:55.424|37=O-20261004-011347-2|11=DEMO-1791076435419-1|17=E-20261004-011347-5|20=0|150=A|39=A|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20261004-01:13:55.420|10=001|
20261004-01:13:55.425 OUT  seq=3    35=8  8=FIX.4.2|9=237|35=8|49=ORDERECHO|56=AGENT|34=3|52=20261004-01:13:55.425|37=O-20261004-011347-2|11=DEMO-1791076435419-1|17=E-20261004-011347-6|20=0|150=0|39=0|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=1000|14=0|6=0.0000|60=20261004-01:13:55.421|10=227|
20261004-01:13:55.426 OUT  seq=4    35=8  8=FIX.4.2|9=260|35=8|49=ORDERECHO|56=AGENT|34=4|52=20261004-01:13:55.426|37=O-20261004-011347-2|11=DEMO-1791076435419-1|17=E-20261004-011347-7|20=0|150=4|39=4|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:13:55.422|58=FOK not fully fillable|10=253|
20261004-01:14:05.170 OUT  seq=2    35=3  8=FIX.4.2|9=150|35=3|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:05.169|45=2|371=126|373=1|58=Required tag missing: 126 (GTD needs ExpireTime 126 or ExpireDate 432)|10=205|
20261004-01:14:08.360 OUT  seq=2    35=8  8=FIX.4.2|9=264|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:08.360|37=O-20261004-011347-5|11=DEMO-1791076448358-1|17=E-20261004-011347-12|20=0|150=8|39=8|1=NOPE|55=AAPL|54=1|38=100|40=1|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:14:08.359|103=0|58=Unknown account NOPE|10=136|
20261004-01:14:16.056 IN   seq=2    35=D  8=FIX.4.2|9=181|35=D|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:16.055|1=CERT1|11=DEMO-1791076456055-1|21=1|55=ZWZZT|54=1|38=100|40=2|60=20261004-01:14:16.055|44=10.00|59=6|126=20261004-01:14:27|10=110|
20261004-01:14:16.060 OUT  seq=2    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:16.060|37=O-20261004-011347-7|11=DEMO-1791076456055-1|17=E-20261004-011347-14|20=0|150=A|39=A|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:16.057|10=081|
20261004-01:14:16.061 OUT  seq=3    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=3|52=20261004-01:14:16.061|37=O-20261004-011347-7|11=DEMO-1791076456055-1|17=E-20261004-011347-15|20=0|150=0|39=0|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:16.057|10=050|
20261004-01:14:27.126 OUT  seq=4    35=8  8=FIX.4.2|9=266|35=8|49=ORDERECHO|56=AGENT|34=4|52=20261004-01:14:27.125|37=O-20261004-011347-7|11=DEMO-1791076456055-1|17=E-20261004-011347-16|20=0|150=C|39=C|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:14:27.048|58=GTD order expired|10=214|
20261004-01:14:50.135 IN   seq=2    35=D  8=FIX.4.2|9=159|35=D|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:50.135|1=CERT1|11=DEMO-1791076490135-1|21=1|55=ZWZZT|54=1|38=100|40=2|60=20261004-01:14:50.135|44=10.00|59=1|10=047|
20261004-01:14:50.141 OUT  seq=2    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:50.140|37=O-20261004-011347-8|11=DEMO-1791076490135-1|17=E-20261004-011347-17|20=0|150=A|39=A|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:50.136|10=075|
20261004-01:14:50.141 OUT  seq=3    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=3|52=20261004-01:14:50.141|37=O-20261004-011347-8|11=DEMO-1791076490135-1|17=E-20261004-011347-18|20=0|150=0|39=0|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:50.137|10=045|
20261004-01:14:52.379 IN   seq=2    35=H  8=FIX.4.2|9=95|35=H|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:52.379|11=DEMO-1791076490135-1|55=ZWZZT|54=1|10=040|
20261004-01:14:52.381 OUT  seq=2    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:52.380|37=O-20261004-011347-8|11=DEMO-1791076490135-1|17=E-20261004-011347-19|20=3|150=0|39=0|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:52.380|10=057|
20261004-01:14:52.670 IN   seq=2    35=H  8=FIX.4.2|9=95|35=H|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:52.669|11=DEMO-1791076490135-1|55=ZWZZT|54=1|10=042|
20261004-01:14:52.671 OUT  seq=2    35=8  8=FIX.4.2|9=247|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:52.671|37=O-20261004-011347-8|11=DEMO-1791076490135-1|17=E-20261004-011347-20|20=3|150=0|39=0|1=CERT1|55=ZWZZT|54=1|38=100|40=2|44=10.00|32=0|31=0.00|151=100|14=0|6=0.0000|60=20261004-01:14:52.670|10=054|
20261004-01:14:53.047 IN   seq=2    35=H  8=FIX.4.2|9=88|35=H|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:53.047|11=NO-SUCH-ORDER|55=ZWZZT|54=1|10=116|
20261004-01:14:53.049 OUT  seq=2    35=8  8=FIX.4.2|9=216|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:53.048|37=NONE|11=NO-SUCH-ORDER|17=E-20261004-011347-22|20=3|150=8|39=8|55=ZWZZT|54=1|38=0|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:14:53.048|58=Unknown order|10=213|
### agent44 (FIX.4.4)
20261004-01:14:08.558 OUT  seq=2    35=8  8=FIX.4.4|9=260|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:08.558|37=O-20261004-011347-6|11=DEMO-1791076448556-1|17=E-20261004-011347-13|150=8|39=8|1=NOPE|55=AAPL|54=1|38=100|40=1|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:14:08.557|103=15|58=Unknown account NOPE|10=247|
20261004-01:14:52.857 IN   seq=2    35=H  8=FIX.4.4|9=95|35=H|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:14:52.857|11=DEMO-1791076490135-1|55=ZWZZT|54=1|10=043|
20261004-01:14:52.859 OUT  seq=2    35=8  8=FIX.4.4|9=218|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:14:52.859|37=NONE|11=DEMO-1791076490135-1|17=E-20261004-011347-21|150=I|39=8|55=ZWZZT|54=1|38=0|32=0|31=0.00|151=0|14=0|6=0.0000|60=20261004-01:14:52.858|58=Unknown order|10=214|
20261004-01:15:07.326 IN   seq=2    35=H  8=FIX.4.4|9=93|35=H|49=AGENT|56=ORDERECHO|34=2|52=20261004-01:15:07.325|11=DEMO-1791076504103-1|55=EFG|54=1|10=048|
20261004-01:15:07.327 OUT  seq=2    35=8  8=FIX.4.4|9=236|35=8|49=ORDERECHO|56=AGENT|34=2|52=20261004-01:15:07.327|37=O-20261004-011347-9|11=DEMO-1791076504103-1|17=E-20261004-011347-27|150=I|39=1|1=CERT1|55=EFG|54=1|38=1000|40=1|32=0|31=0.00|151=500|14=500|6=121.0800|60=20261004-01:15:07.327|10=003|
### engine log
20261004-01:13:47.376 INFO    session  engine  Acceptor listening on 127.0.0.1:19878 for agent42, agent44 (run_id=20261004-011347)
20261004-01:13:47.377 INFO    session  engine  Acceptor listening on 127.0.0.1:19879 for strict-broker (run_id=20261004-011347)
20261004-01:13:47.498 INFO    session  engine  Control API listening on http://127.0.0.1:18090 (docs at /docs)
20261004-01:14:52.414 INFO    session  engine  Restart requested: Engine restart via control API
20261004-01:14:52.427 INFO    session  agent42  order restored: O-20261004-011347-8 ZWZZT BUY 100 LMT px=10.00 (limit) rule=nasdaq-test-hold NEW cum=0 leaves=100 tif=GTC chain=DEMO-1791076490135-1 0 event(s) rescheduled
20261004-01:14:52.427 INFO    session  agent42  Restored 1 open order(s) from data/orders/agent42.jsonl
20261004-01:14:52.428 INFO    session  engine  Acceptor listening on 127.0.0.1:19878 for agent42, agent44 (run_id=20261004-011347)
20261004-01:14:52.428 INFO    session  engine  Acceptor listening on 127.0.0.1:19879 for strict-broker (run_id=20261004-011347)
20261004-01:14:52.429 INFO    session  engine  Restart complete in 0.015s; listening on 19878, 19879; open orders restored: agent42=1, agent44=0, strict-broker=0
20261004-01:14:52.429 INFO    session  engine  API   POST /admin/restart -> 200
```

The Python timeline over the same log gives `verdict: PASS` for all four
chains: the IOC, the FOK kill, the GTD expiry, and the GTC order spanning the restart.

790 does not appear in the 4.4 status answers above because the demo client
sends no OrdStatusReqID. It is echoed when sent, and that is tested.

### Stopped

```
$ kill -TERM 18656        # my private engine only
stopped 18656
Shutting down...
20261004-01:15:25.827 INFO    session  engine  Acceptor stopped
20261004-01:15:25.827 INFO    session  engine  Shutdown complete
$ lsof -iTCP -sTCP:LISTEN | grep -E ':1(8090|9878|9879)'
private ports free
$ pgrep -fl orderecho_Main
14395 python orderecho_Main.py --config config/orderecho_multi.yaml     # yours, untouched
```

Everything I started is stopped; pid 14395 is untouched.

---

## 4. The agent's 11 N/A cases, plus 7.8

All of these assume the agent runs against `config/orderecho_multi.yaml`. Two
changes to the agent are needed first, whatever is done with the N/A list:

- **Account.** `valid_accounts: [CERT1, CERT2]` rejects every order without one
  of those accounts. Set `account: CERT1` on the agent's emulator sessions —
  the key already exists in the agent's config (`internal/config/config.go`,
  "tag 1 on D when set"). **Until that is done, every order case fails against
  the new emulator**, including your running instance once it restarts on
  0.9.0 code (Q4).
- Remove the 11 entries from `not_applicable:` in `certs/targets/emulator.yaml`.

| Case | Was N/A because | Now | Control step for `certs/targets/emulator.yaml` |
|---|---|---|---|
| 4.5 IOC | Day only | `59=3` accepted. On `ZWZZT` (`ack_only`) the order is acked and the remainder canceled at once: `150=4 39=4 58=IOC remainder canceled`, so it is never left resting. | none (auto) |
| 4.6 FOK | Day only | `59=4` accepted. On `ZWZZT` it is killed with CumQty 0, `58=FOK not fully fillable`, and never partially filled. On `AAPL` it would get one full fill. | none (auto) |
| 4.8 GTC across session restart | Day only | `59=1` accepted. The order lives across logout and reconnect (it always did within one engine run), and with `orders.persist` across an engine restart too. | none (auto) |
| 4.9 GTX | Day only | `59=5` accepted and treated as Day (D3). | none (auto) |
| 5.1 Pending New | never sent | `orders.send_pending_new: true`: `150=A 39=A` before every `150=0`. | none (auto) |
| 5.5 Done For Day | no behaviour, no endpoint | `150=3 39=3`, LeavesQty 0. | `post: "/orders/{{order.o1.order_id}}/done-for-day"` |
| 5.6 Expired | never expires | `150=C 39=C` via a GTD expiry or the endpoint. **But the case as written still fails** — see below. | `post: "/orders/{{order.o1.order_id}}/expire"` (with the TIF changed — Q3) |
| 6.4 Too late to cancel | no pending-fill state | Lock puts the order in "fill in progress"; the cancel gets `35=9 102=0 434=1 58=Too late to cancel: a fill is in progress`. The case accepts 102 0 or 1. | `post: "/orders/{{order.o1.order_id}}/lock"` (optionally `/unlock` afterwards; the order stays open and locked otherwise) |
| 6.8 Pending Replace | multi had `send_pending_acks: false` | The multi config now sets it true: `150=E 39=E`, then `150=5`. | none (auto) |
| 7.6 Invalid Account | no validation | `NO-SUCH-ACCOUNT` is rejected: `150=8 39=8 58=Unknown account NO-SUCH-ACCOUNT`, with `103=0` (4.2) or `103=15` (4.4). It matches the case's `exec_type: REJECTED` alternative. | none (auto) |
| 8.7 Exchange restart | orders lost, no endpoint | `orders.persist: true` plus `POST /admin/restart`. Every session is logged out (the agent should answer the Logout), the listeners come back on the same ports, sequence numbers and open orders are reloaded, and the reconnect with `reset: false` works. The cancel is then accepted. | `post: "/admin/restart"` (unscoped; it restarts every session) |
| 7.8 (PASS with warning) | `379` missing | `session.business_reject_ref_id: true` adds `379` **when the rejected message has a business ID**. The case's raw `35=ZZ` carries only `58`, so FIX gives 379 nothing to refer to and it is still omitted, and the warning stays. To clear it, add an ID to the raw message, e.g. `raw: [[35, ZZ], [11, "{{clordid}}"], [58, …]]`, and `379` then echoes it (Q5). | none (auto) |

**5.6 in detail.** The case sends an IOC on the hold symbol, then a control step to expire it. Under spec §3 the
emulator now cancels the remainder of an IOC at once (`150=4`), so the order is
closed before the control step. `/expire` would then answer 409, and `expect:
EXPIRED` would time out. Either of these works:
- `tif: gtd` with an ExpireTime a few seconds ahead and no control step;
- `tif: day` (or gtc) with `post: /orders/{{order.o1.order_id}}/expire`.

Both change the agent's suite, which is out of scope here (Q3).

`limit_buy: "1.00"` on ZWZZT is a passive buy, so it clears the 10% band
(aggressive mode). That held in the A3 runs and is unchanged.

---

## 5. Decisions

**D1. Four gating keys beyond §8.**
- **The keys:** `orders.time_in_force` (default `[day]`), `orders.status_requests` (default false), `session.gap_queue` (false) and `session.business_reject_ref_id` (false).
- **Why `time_in_force`:** §0 requires goldens byte-identical and existing tests passing. Golden scenario B7 sends `59=1` and expects "TimeInForce not supported". Several Cook 1–8 tests pin `16=0`, the `35=j` field list, and `35=H` → `35=j`.
- **Why the other three:** the same §0 constraints, plus "every new behaviour defaults OFF in the base config".
- All four are on in the multi config.

**D2. The multi config turns everything on,** including `send_pending_acks: true`. That clears 6.8. The base config is unchanged.

**D3. Time in force.**
- **Ack first:** IOC and FOK are acked (`150=0`) before their outcome, as most venues do.
- **IOC:** takes the rule's *immediate* outcome:
  - `full_fill` → one fill;
  - `partial_fill` → the first fill only;
  - `ack_only` and `cancel_after_ack` → nothing, then the cancel.
- **FOK:** "fully fillable" means `full_fill`, or `partial_fill` with `then: fill_rest`.
- **GTX:** treated as Day.
- **ExpireDate (432):** the order lives through the end of that UTC day.
- **Malformed expiry:** a malformed 126/432 is `373=6`; a missing one is `373=1 371=126`.
- **Precedence:** band → rule → TIF, so a reject rule still rejects.

**D4. Pending prices.** A market IOC/FOK that has nothing to fill closes without waiting for its quote. Only the new closing events (IOC cancel, FOK kill, GTD expiry) bypass a pending price. Cancels and fills keep their 0.8 timing.

**D5. Lock semantics.**
- Lock and unlock require an open order.
- Unlocking an order that isn't locked is a 409.
- A fill, or any terminal event, clears the lock.
- While locked, both cancel and replace get `102=0`.

**D6. Persistence.**
- **What is stored:** the order and its scheduled events, stored as relative delays.
- **When:** every change, and once more after scheduling, so a restored order resumes its fills.
- **On restore:** events are rescheduled relative to the reload, never fired at once.
- **Day expiry:** applies only to *restored* Day orders from an earlier UTC date, at their session's next logon. There is no end-of-session clock (Q7).
- **The snapshot:** the order snapshot in evidence is unchanged, because a Cook 6 test pins its keys.
- **Lookup order:** a status request finds the order by `37`, then by its current ClOrdID, then by any earlier ClOrdID in its chain.
- **Other sessions:** an order in another session is "Unknown order".

**D7. Restart.**
- **Sequence:** `POST /admin/restart` logs out every live session (waiting up to `logout_timeout_sec` for replies), closes the listeners, reloads the config from its file, rebuilds the price source and runtimes, and listens again on the same ports. A port-0 config keeps its bound ports.
- **Response:** it returns `{restarted, seconds, ports, sessions, open_orders}`.
- **Conflicts:** a second restart while one is running is a 409.

**D8. 379.**
- **When:** set only when the rejected message carries a business-level ID, as FIX specifies. ClOrdID is checked first, then the other ID tags FIX lists.
- **Why:** inventing an ID would be wrong FIX.
- **Effect:** this is why 7.8 still warns.

**D9. Gap queue details.**
- **Holding:** a Logon that reveals a gap is processed at once, and its sequence number is held as a placeholder until the fill consumes it.
- **Overflow:** the 1001st held message triggers a Logout with `58=Gap queue overflow`.
- **A second hole:** a hole left before held messages gets its own closed-range request.

**D10. Negative cache.**
- **Defaults:** the class default is 0, which keeps a 0.4 test of "failures are not cached" meaningful. The config default is 300, per spec, and the engine passes it.
- **When it is active:** live mode only.

**D11. Check fixes.**
- **Fix 1:** a Reject only answers a request from the *other* side. The side is judged by SenderCompID, else by log direction; if neither is known, the reject counts. `test_Timeline.py`'s helper gave every line the same CompIDs, which the old bug masked, so I fixed the helper (assertion unchanged).
- **Fix 2:** messages are framed by BodyLength, falling back to the first `10=` that starts a field.
- **Fix 3:** a message verifies if its bytes check out under latin-1 (this emulator's logs) or UTF-8 (the agent's).
- **Fix 4:** a port of the agent's `splitDuplicates`.

**D12. Docs.**
- **`test_Docs`:** the demo-client subcommand check now introspects the real parser. That is stronger than the old hard-coded list, and it now requires `query`.
- **Dictionary overlay:** names added for 790 and `35=H`, so the generated tables don't show "Tag790".

**D13. Test infrastructure.**
- **Mirror runs:** test runs used a mirror because the live emulator trips the isolation guard.
- **Real run:** a private copy with only the ports changed.

---

## 6. Questions

**Q1. Gating keys (D1).** Are `orders.time_in_force`, `orders.status_requests`, `session.gap_queue` and `session.business_reject_ref_id` the names and defaults you want? Turning them on in the base config would require regenerating the B7 golden and changing the Cook 1–8 tests that pin the old behaviour.

**Q2. Ack before IOC/FOK (D3).** IOC/FOK get `150=0` before their fill, cancel or kill. Some venues send the outcome directly. Keep this, or make it configurable?

**Q3. Case 5.6.** The suite sends an IOC and then a control step to expire it. The emulator cancels an IOC remainder (`150=4`), as spec §3 says, so the case cannot pass as written. Which should change?
- (a) The agent's suite: `tif: gtd` with an ExpireTime, or `tif: day` plus `/expire`.
- (b) The emulator: report an IOC remainder as Expired (`150=C`), as some venues do. This would contradict spec §3's "IOC remainder canceled".

**Q4. `valid_accounts` in the multi config.** The agent must send `account: CERT1`; until then every order case is rejected. Your running emulator (pid 14395) is still on the old code. When it next starts, the agent's existing setup breaks until its session config gets the account. Keep `valid_accounts` in the multi config, or move it to a separate config?

**Q5. 7.8 and 379 (D8).** Should the agent's 7.8 raw message carry an ID (e.g. `11=`) so that 379 is exercised, or should the emulator put something in 379 even when there is no ID? I chose standard FIX: omit it.

**Q6. `/admin/restart` scope.** It restarts the whole engine, every session, which matches "exchange restart". Is a per-session restart also wanted?

**Q7. Day orders.** They are expired only when restored from an earlier UTC date. Should there also be a session-end time (e.g. `orders.day_end_utc`) that expires live Day orders, or sends Done For Day automatically?

**Q8. GTX** is treated as Day. Is that enough, or should GTX have its own behaviour?

**Q9. The CLAUDE.md rule.** OrderEcho's `CLAUDE.md` says never to modify `../OrderEchoFixEmulator`. I followed your explicit Cook 9 instruction instead. You may want to reword that rule (e.g. "except in emulator cooks") so future sessions don't see a conflict.

---

## Cook 9.1

Your answers: Q1 keep (no change); Q2 add `orders.ioc_fok_ack`; Q3 the agent's
suite changes (no emulator change); Q4 reject only a present, unlisted account;
Q5 standard FIX stays (no change). Plus the guard change.

### Changes

| What | Where |
|---|---|
| `orders.ioc_fok_ack` (bool, default **true**). With false, an IOC or FOK order gets no `150=0`: its fill, cancel or kill is the first report. If Pending New is on, `150=A` still comes first. Day/GTC/GTX/GTD are unaffected. The multi config sets it to true explicitly. | `orderecho_Config.py`, `orderecho_OrderBook.py`, `config/orderecho_multi.yaml` |
| `orders.valid_accounts` checks tag 1 only when it is present; an order with no Account is accepted. A present, unlisted Account is still rejected (`103=0` / `103=15`). | `orderecho_OrderBook.py`, config comment, schema note |
| The repo guard tells a running emulator apart from a leaky test (below). | `tests/isolation.py`, `conftest.py` |
| Docs: TIF section (`ioc_fok_ack`), Accounts section, troubleshooting (the new guard message), a "9.1" changelog note; site regenerated. | `docs/src/order-behavior.md`, `troubleshooting.md`, `changelog.md`, `docs/site/*` |

Tests:
- **New:**
  - `test_TimeInForce.py`: 6 tests for `ioc_fok_ack`. They cover IOC and FOK without the ack in both versions, Pending New still coming first, other TIFs untouched, and the YAML default, load and validation.
  - `test_Lifecycle.py`: `test_a_missing_account_is_allowed` (4.2 and 4.4).
  - `test_Isolation.py`: 5 guard tests.
- **Changed, on your Q4 answer:**
  - The "missing account is rejected" case is gone from the account-reject test, which now covers an unlisted account only.
  - `test_a_rejected_order_gets_no_pending_new` triggered its reject with a missing account; it now uses an unlisted one, with the same assertion.

### The guard

When `data/` or `logs/` changed during the run, the guard now looks for the writer. It finds a process either by its open files under the repo's `data/` or `logs/` (`lsof`), or as a Python process running `orderecho_Main.py` whose working directory is the repo. A shell whose command line merely mentions `orderecho_Main.py` doesn't count. If it finds an emulator:

```
a running emulator, pid N, is writing to repo data/logs; stop it before running tests
    (<its command line>)
These changes are most likely its doing, not a test's:
  modified logs/fix/...
```

Any other process holding files there is named neutrally ("a running process, pid N, has files open…"). If nothing is found, the old message about `isolated_config` stands.

It was checked against a real engine: `orderecho_Main.py` with the multi config, started in the scratch mirror and taking an order while a short pytest ran there:

```
..........E                                                              [100%]
==================================== ERRORS ====================================
_______ ERROR at teardown of test_open_orders_survive_a_process_restart ________
a running emulator, pid 55872, is writing to repo data/logs; stop it before running tests
    (<repo>/.venv/bin/python orderecho_Main.py --config $SP/c9/guard_demo.yaml)
These changes are most likely its doing, not a test's:
  created data/msgstore/agent42.jsonl.20261007-194145
  modified data/evidence/20261007-194143.jsonl
  modified data/msgstore/agent42.jsonl
  modified data/orders/agent42.jsonl
  modified data/seqnums/agent42.json
  modified logs/engine/orderecho_20261007.log
  modified logs/fix/agent42_20261007.log
=========================== short test summary info ============================
ERROR tests/test_Persistence.py::test_open_orders_survive_a_process_restart
10 passed, 1 error in 9.84s
```

That engine was stopped afterwards; nothing I started is still running.

### `pytest -q`, three times

Final result, after the Dropbox fix below: **three clean runs in the repo, 835 passed each.**

First attempt, in the repo (no emulator running):

```
=== run 1
...........................................E                             [100%]
==================================== ERRORS ====================================
___________ ERROR at teardown of test_the_banner_shows_the_guide_url ___________
tests wrote to the repo's own runtime directories:
  deleted data/guard_probe/leak.txt
Build the config with tests/isolation.isolated_config so every storage path lives under tmp_path (spec 3.5).
=========================== short test summary info ============================
ERROR tests/test_Viewer.py::test_the_banner_shows_the_guide_url - Failed: tes...
835 passed, 1 error in 147.77s (0:02:27)
=== run 2
835 passed in 148.05s (0:02:28)
=== run 3
...........................................E                             [100%]
==================================== ERRORS ====================================
___________ ERROR at teardown of test_the_banner_shows_the_guide_url ___________
tests wrote to the repo's own runtime directories:
  created data/guard_probe/leak.txt
Build the config with tests/isolation.isolated_config so every storage path lives under tmp_path (spec 3.5).
=========================== short test summary info ============================
ERROR tests/test_Viewer.py::test_the_banner_shows_the_guide_url - Failed: tes...
835 passed, 1 error in 147.97s (0:02:27)
```

**All 835 tests pass in every run.** The guard error in runs 1 and 3 is **not** a running emulator, and not the new code. The cause is Dropbox.

The repo is inside Dropbox (CloudStorage), and Dropbox's File Provider restores files a test has just deleted. Cook 3's guard test (`test_the_guard_fails_a_run_that_writes_to_the_repo`, spec 9.12) deliberately writes `data/guard_probe/leak.txt` into the real repo and deletes it. Dropbox brings it back during the next run, either before the test's cleanup (seen as "deleted") or after it (seen as "created"). After the third run, `data/guard_probe/leak.txt` was back on disk, timestamped after the test removed it. I've left it there.

My first version of the new end-to-end guard test failed the same way. It now runs in a throwaway copy of `conftest.py` + `tests/isolation.py` under `tmp_path` and never touches the real `data/`.

The same suite in the scratch mirror, outside Dropbox, three times:

```
=== mirror run 1
835 passed in 119.82s (0:01:59)
=== mirror run 2
835 passed in 119.65s (0:01:59)
=== mirror run 3
835 passed in 122.02s (0:02:02)
```

### The Dropbox fix (9.1-Q1 answered: both)

- **(a), done by you:** `data/` and `logs/` are Dropbox-ignored in both repos. Both directories now carry `com.dropbox.ignored`, which I confirmed with `xattr`.
- **(b), done here:** Cook 3's `test_the_guard_fails_a_run_that_writes_to_the_repo` now runs in a throwaway repo under `tmp_path`.
  - That repo holds `conftest.py` and `tests/isolation.py`, copied verbatim, with the real code on `PYTHONPATH`. The guard is the same code, run the same way.
  - Its assertions are unchanged, plus one more: the real repo's `data/guard_probe` must not exist afterwards.
  - The new end-to-end guard test shares the same `_guard_copy` helper.
  - No test now writes into the real `data/` or `logs/`.
- **Leftovers deleted:** `data/guard_probe/leak.txt` (it held `this should fail the run`) and the empty `data/guard_probe/` directory.

`pytest -q` in the repo, three times, no emulator running:

```
=== run 1
835 passed in 149.61s (0:02:29)
=== run 2
835 passed in 148.28s (0:02:28)
=== run 3
835 passed in 151.01s (0:02:31)
```

After the runs, `data/` holds only `evidence`, `msgstore` and `seqnums`; no `guard_probe*` came back.

### Decisions

- **9.1-D1.** `ioc_fok_ack: false` drops only the `150=0`. Pending New is a separate switch and still comes first, because you asked only about the ack.
- **9.1-D2.** The version stays 0.9.0 / `cook9`; the changes are noted under 0.9.0 in the changelog as "Follow-ups (9.1)". A bump would also mean changing the version literals pinned in tests, and nobody asked for one.
- **9.1-D3.** I first left the Cook 3 guard test alone because it is spec-derived. You then approved moving it, so it now runs in a throwaway repo (above), with the same assertions.

### Questions

- **9.1-Q1. The Dropbox issue.** Answered: both (a) and (b) were done; see "The Dropbox fix" above. No open questions remain for 9.1.
