# Cook 7 — the log viewer

OrderEchoFixEmulator **0.7.0 (cook7)**. Everything in SPEC_Cook7.md §3–§9 is
built, plus the two extras you asked for: the version/build bump with its
`/health` assertions, and `engine.logon_timeout_sec`.

`pytest -q`: **651 passed**, up from 495 at the end of Cook 6.

---

## 1. Files

### Created

| File | Lines | What it is |
|---|---|---|
| `orderecho_LogParse.py` | 456 | §3 the pure parser: log lines → `ParsedMessage`, plus `ParseStats` |
| `orderecho_FixDict.py` | 241 | §4 tag names and enum meanings, version-aware |
| `dictionaries/overlay.json` | 91 | §4 the hand-written overlay the emulator guarantees |
| `orderecho_Timeline.py` | 617 | §5 order chains and the ten consistency checks |
| `orderecho_LogView.py` | 662 | §6 the CLI: `view`, `timeline`, `stats`, `serve` |
| `orderecho_LogViewer.py` | 328 | §7 `MessageSource` and the FastAPI router |
| `viewer/index.html` | 328 | §7 the page: one file, no external anything |
| `tests/test_LogParse.py` | 337 | §9.1–2, 67 tests (parser and dictionary) |
| `tests/test_Timeline.py` | 483 | §9.3–4, 41 tests (chains and checks) |
| `tests/test_LogView.py` | 293 | §9.5, §9.7, 20 tests (CLI and performance) |
| `tests/test_Viewer.py` | 374 | §9.6, 28 tests (router, engine mount, `serve`) |

### Changed

| File | Change |
|---|---|
| `orderecho_Version.py` | `ORDERECHO_VERSION = "0.7.0"`, `ORDERECHO_BUILD = "cook7"` |
| `orderecho_Config.py` | `EngineConfig.logon_timeout_sec: float = 30.0`, parsed in `_load_engine` with the usual number validation (minimum 0) |
| `orderecho_Transport.py` | `_route` reads `config.engine.logon_timeout_sec`; the old `ROUTE_TIMEOUT` constant stays as the fallback for a config object that predates the field |
| `orderecho_ControlApi.py` | mounts the viewer router at `/viewer` over the engine's own `logs/fix`; adds `GET /orders/{order_id}/timeline` |
| `orderecho_FixVersion.py` | one prose comment reworded (see decision 12) |
| `tests/test_CarryOvers.py` | `/health` now asserts `0.7.0` / `cook7`; the build-name guard's needles extended to cook7 |
| `README.md` | new "Reading the logs" section (~120 lines) before "Tests" |

### The structure of `dictionaries/fix_tags.json`, and how it is loaded

The file is FIXReader's, 149,708 bytes, and it is **not modified** — Cook 7
only reads it. Its top level is a JSON **list of 181 objects**, one per tag:

```json
{
  "tag": 8,
  "name": "BeginString",
  "description": "Identifies beginning of a new message and the protocol
                  version. Must always be the first field in any FIX message.",
  "data_type": "String",
  "required_in":  [{"code": "All", "name": "All Messages"}],
  "optional_in":  [{"code": "D", "name": "New Order Single"}, ...],
  "valid_values": {"FIX.4.0": "FIX.4.0", "FIX.4.2": "FIX.4.2", ...},
  "fix_version_added": "FIX.4.0",
  "notes": "...",
  "fix_versions": ["FIX.4.0", "FIX.4.1", "FIX.4.2", "FIX.4.4"]
}
```

59 of the 181 entries carry `valid_values`, and that is a flat
`{value: meaning}` map — it is *not* keyed by FIX version, so the file cannot
by itself say that `150=F` means Trade in 4.4 and nothing in 4.2.

`FixDictionary._load_base` therefore:

- reads the list and indexes it by `int(entry["tag"])`;
- takes `name` as the tag name and trims `description` at the first em-dash,
  `" - "` or `" ("` for the short form the CLI prints;
- takes `valid_values` as the version-independent enum map.

`_load_overlay` then layers `dictionaries/overlay.json` on top. The overlay has
three sections and **wins wherever the two disagree**:

- `tags` — per-version tag names, e.g. 32 is `LastShares` in 4.0/4.1/4.2 and
  `LastQty` otherwise;
- `enums` — the version-independent values for the 16 tags this emulator
  actually sends (35, 39, 150, 54, 40, 59, 20, 21, 103, 102, 434, 373, 380,
  141, 123, 43), so the viewer never shows a bare number for our own traffic
  whatever the base file does;
- `version_enums` — where the versions disagree: FIX.4.2 gets
  `150: {1: PartialFill, 2: Fill}`, FIX.4.4 gets `150: {F: Trade, …}` with
  `1`/`2` still named but marked *(deprecated in 4.4)*.

Everything the base file was consulted for is recorded so it can degrade: a
missing, unreadable, or wrong-shaped `fix_tags.json` leaves the dictionary
running on the overlay alone and appends a line to `FixDictionary.warnings`
rather than raising. Four tests cover those three failure shapes.

---

## 2. `pytest -q`

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 11%]
........................................................................ [ 22%]
........................................................................ [ 33%]
........................................................................ [ 44%]
........................................................................ [ 55%]
........................................................................ [ 66%]
........................................................................ [ 77%]
........................................................................ [ 88%]
........................................................................ [ 99%]
...                                                                      [100%]
651 passed in 98.01s (0:01:38)
```

No warnings, no skips, no xfails. The Cook 5 isolation guard is green: nothing
in the run wrote to the repo's `data/` or `logs/`. The §9.7 performance test
parses a generated 100,000-message, 26.9 MB log — **5.3 s** on this machine
against a 15 s budget, and because the parse streams, **peak 0.1 MB** against
a 20 MB budget (the peak does not move with the size of the file).

---

## 3. The real run

Started with the three-session config, control API on 8090:

```
$ .venv/bin/python orderecho_Main.py --config config/orderecho_multi.yaml
OrderEchoFixEmulator 0.7.0 (cook7) - 3 FIX sessions
  config         : config/orderecho_multi.yaml
  evidence file  : data/evidence/20260927-093636.jsonl
  fix log        : logs/fix
  engine log     : logs/engine

  session          version  route                      port   rules  band
  ---------------- -------- -------------------------- ------ ------ ----
  agent42          FIX.4.2  ORDERECHO -> AGENT         9878   8      10%
  agent44          FIX.4.4  ORDERECHO -> AGENT         9878   8      10%
  strict-broker    FIX.4.2  STRICTBRK -> AGENT         9879   1      2%

  control api    : http://127.0.0.1:8090  (docs at /docs)
  Ctrl+C to shut down.
20260927-09:36:39.420 INFO session engine  Price warm-up: AAPL 341.07 (live:yfinance)
20260927-09:36:40.257 INFO session engine  Price warm-up: MSFT 516.17 (live:yfinance)
```

Pricing was live, so the band rejects below are against real reference prices.

Then the demo client, driven through `orderecho_DemoClient.py session` on
three sessions — **agent42** (4.2) and **agent44** (4.4) share port 9878 and
are told apart by BeginString; **strict-broker** has 9879 to itself:

```
[agent42] -> NewOrderSingle      AAPL 1000 BUY LMT 227.50
[agent42] <- ExecutionReport     AAPL exec=NEW status=NEW cum=0 leaves=1000
[agent42] <- ExecutionReport     AAPL exec=FILL status=FILLED last=1000@227.50 cum=1000 leaves=0 avg=227.5000
[agent42] -> NewOrderSingle      FDX 1000 BUY LMT 250.00
[agent42] <- ExecutionReport     FDX exec=PARTIAL_FILL last=400@250.00 cum=400 leaves=600
[agent42] <- ExecutionReport     FDX exec=PARTIAL_FILL last=100@250.00 cum=500 leaves=500
[agent42] -> NewOrderSingle      ZWZZT 500 BUY LMT 10.00        (ack-only rule: stays live)
[agent42] -> OrderCancelReplaceRequest  41=DEMO-...-3 11=DEMO-...-4-R qty=800 px=10.50
[agent42] <- ExecutionReport     ZWZZT exec=REPLACE status=NEW leaves=800
[agent42] -> OrderCancelRequest  41=DEMO-...-4-R 11=DEMO-...-5-C
[agent42] <- ExecutionReport     ZWZZT exec=CANCELED status=CANCELED
[agent42] -> NewOrderSingle      AAPL 100 BUY LMT 900.00
[agent42] <- ExecutionReport     AAPL exec=REJECTED 103=3 "Limit 900.00 outside 10% band of ref 341.07 (live:yfinance)"

[agent44] -> NewOrderSingle      MSFT 600 SELL LMT 400.00
[agent44] <- ExecutionReport     MSFT exec=REJECTED 103=3 "Limit 400.00 outside 10% band of ref 516.17 (live:yfinance)"
[agent44] -> NewOrderSingle      KO 300 BUY LMT 60.00
[agent44] <- ExecutionReport     KO exec=REJECTED 103=0 "Rejected by OrderEcho rule"
[agent44] -> NewOrderSingle      CSCO 400 BUY MKT
[agent44] <- ExecutionReport     CSCO exec=TRADE status=FILLED last=400@106.70   (150=F: 4.4)

[strict-broker] -> NewOrderSingle AAPL 100 BUY LMT 227.50
[strict-broker] <- ExecutionReport AAPL exec=REJECTED 103=0 "Strict broker rejects everything"
```

### `view` of today's logs, plain

```
$ .venv/bin/python orderecho_LogView.py view logs/fix/*_20260927.log --no-color
20260927-09:36:40.922 --> agent42            1 Logon                  141=Y 108=30
20260927-09:36:40.925 <-- agent42            1 Logon                  141=Y 108=30
20260927-09:37:00.813 --> agent42            2 New Order              Buy 1000 AAPL Limit @227.50 11=DEMO-1790501820812-1
20260927-09:37:00.836 <-- agent42            2 Execution Report       New/New cum=0 lv=1000 avg=0.0000 11=DEMO-1790501820812-1
20260927-09:37:01.411 <-- agent42            3 Execution Report       Fill/Filled 1000@227.50 cum=1000 lv=0 avg=227.5000 11=DEMO-1790501820812-1
20260927-09:37:04.815 --> agent42            3 New Order              Buy 1000 FDX Limit @250.00 11=DEMO-1790501824814-2
20260927-09:37:06.101 <-- agent42            4 Execution Report       New/New cum=0 lv=1000 avg=0.0000 11=DEMO-1790501824814-2
20260927-09:37:06.682 <-- agent42            5 Execution Report       PartialFill/Partially Filled 400@250.00 cum=400 lv=600 avg=250.0000 11=DEMO-1790501824814-2
20260927-09:37:07.194 <-- agent42            6 Execution Report       PartialFill/Partially Filled 100@250.00 cum=500 lv=500 avg=250.0000 11=DEMO-1790501824814-2
20260927-09:37:08.817 --> agent42            4 New Order              Buy 500 ZWZZT Limit @10.00 11=DEMO-1790501828816-3
20260927-09:37:09.661 <-- agent42            7 Execution Report       New/New cum=0 lv=500 avg=0.0000 11=DEMO-1790501828816-3
20260927-09:37:12.816 --> agent42            5 Order Cancel/Replace Request ZWZZT Buy 11=DEMO-1790501832816-4-R 41=DEMO-1790501828816-3 qty=800 @10.50
20260927-09:37:12.819 <-- agent42            8 Execution Report       Replace/New cum=0 lv=800 avg=0.0000 11=DEMO-1790501832816-4-R
20260927-09:37:16.817 --> agent42            6 Order Cancel Request   ZWZZT Buy 11=DEMO-1790501836816-5-C 41=DEMO-1790501832816-4-R
20260927-09:37:16.975 <-- agent42            9 Execution Report       Canceled/Canceled cum=0 lv=0 avg=0.0000 11=DEMO-1790501836816-5-C
20260927-09:37:20.820 --> agent42            7 New Order              Buy 100 AAPL Limit @900.00 11=DEMO-1790501840819-6
20260927-09:37:20.825 <-- agent42           10 Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-1790501840819-6 103=Order Exceeds Limit "Limit 900.00 outside 10% band of ref 341.07 (live:yfinance)"
20260927-09:37:25.822 --> agent42            8 Logout                 58=Demo client done
20260927-09:37:25.894 <-- agent42           11 Logout                 58=Logout acknowledged
20260927-09:37:28.010 --> agent44            1 Logon                  141=Y 108=30
20260927-09:37:28.112 <-- agent44            1 Logon                  141=Y 108=30
20260927-09:37:47.915 --> agent44            2 New Order              Sell 600 MSFT Limit @500.00 11=DEMO-1790501867915-1
20260927-09:37:48.138 <-- agent44            2 Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-1790501867915-1 103=Broker/Exchange Option "Rejected by OrderEcho rule"
20260927-09:37:51.915 --> agent44            3 New Order              Sell 600 MSFT Limit @400.00 11=DEMO-1790501871915-2
20260927-09:37:51.928 <-- agent44            3 Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-1790501871915-2 103=Order Exceeds Limit "Limit 400.00 outside 10% band of ref 516.17 (live:yfinance)"
20260927-09:37:55.916 --> agent44            4 New Order              Buy 300 KO Limit @60.00 11=DEMO-1790501875915-3
20260927-09:37:56.879 <-- agent44            4 Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-1790501875915-3 103=Broker/Exchange Option "Rejected by OrderEcho rule"
20260927-09:37:59.918 --> agent44            5 Logout                 58=Demo client done
20260927-09:37:59.990 <-- agent44            5 Logout                 58=Logout acknowledged
20260927-09:38:39.684 --> agent44            1 Logon                  141=Y 108=30
20260927-09:38:39.726 <-- agent44            1 Logon                  141=Y 108=30
20260927-09:38:59.524 --> agent44            2 New Order              Buy 400 CSCO Market 11=DEMO-1790501939524-1
20260927-09:38:59.751 <-- agent44            2 Execution Report       New/New cum=0 lv=400 avg=0.0000 11=DEMO-1790501939524-1
20260927-09:39:01.800 <-- agent44            3 Execution Report       Trade/Filled 400@106.70 cum=400 lv=0 avg=106.7000 11=DEMO-1790501939524-1
20260927-09:39:03.527 --> agent44            3 Logout                 58=Demo client done
20260927-09:39:03.530 <-- agent44            4 Logout                 58=Logout acknowledged
20260927-09:38:02.103 --> strict-broker      1 Logon                  141=Y 108=30
20260927-09:38:02.251 <-- strict-broker      1 Logon                  141=Y 108=30
20260927-09:38:21.970 --> strict-broker      2 New Order              Buy 100 AAPL Limit @227.50 11=DEMO-1790501901970-1
20260927-09:38:22.078 <-- strict-broker      2 Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-1790501901970-1 103=Broker/Exchange Option "Strict broker rejects everything"
20260927-09:38:25.971 --> strict-broker      3 Logout                 58=Demo client done
20260927-09:38:26.009 <-- strict-broker      3 Logout                 58=Logout acknowledged
```

Both 4.2 and 4.4 read naturally: `Fill/Filled` in 4.2 and `Trade/Filled` in
4.4 are the same event named the way each version names it.

### `view --clordid <the replaced order> --decode`

Asking for the *replacement* ClOrdID pulls in the original and the cancel, so
the whole life of the order is decoded. Abridged to the first two messages and
the replace; the full output is 6 messages:

```
$ .venv/bin/python orderecho_LogView.py view logs/fix/*_20260927.log \
      --no-color --clordid DEMO-1790501832816-4-R --decode
20260927-09:37:08.817 --> agent42            4 New Order              Buy 500 ZWZZT Limit @10.00 11=DEMO-1790501828816-3
          8  BeginString              = FIX.4.2  (FIX version 4.2)
          9  BodyLength               = 146
         35  MsgType                  = D  (New Order)
         49  SenderCompID             = AGENT
         56  TargetCompID             = ORDERECHO
         34  MsgSeqNum                = 4
         52  SendingTime              = 20260927-09:37:08.816
         11  ClOrdID                  = DEMO-1790501828816-3
         21  HandlInst                = 1  (Automated execution, private)
         55  Symbol                   = ZWZZT
         54  Side                     = 1  (Buy)
         38  OrderQty                 = 500
         40  OrdType                  = 2  (Limit)
         60  TransactTime             = 20260927-09:37:08.816
         44  Price                    = 10.00
         10  CheckSum                 = 209
20260927-09:37:09.661 <-- agent42            7 Execution Report       New/New cum=0 lv=500 avg=0.0000 11=DEMO-1790501828816-3
          8  BeginString              = FIX.4.2  (FIX version 4.2)
          9  BodyLength               = 238
         35  MsgType                  = 8  (Execution Report)
         49  SenderCompID             = ORDERECHO
         56  TargetCompID             = AGENT
         34  MsgSeqNum                = 7
         52  SendingTime              = 20260927-09:37:09.661
         37  OrderID                  = O-20260927-093636-3
         11  ClOrdID                  = DEMO-1790501828816-3
         17  ExecID                   = E-20260927-093636-6
         20  ExecTransType            = 0  (New)
        150  ExecType                 = 0  (New)
         39  OrdStatus                = 0  (New)
         55  Symbol                   = ZWZZT
         54  Side                     = 1  (Buy)
         38  OrderQty                 = 500
         40  OrdType                  = 2  (Limit)
         44  Price                    = 10.00
         32  LastShares               = 0
         31  LastPx                   = 0.00
        151  LeavesQty                = 500
         14  CumQty                   = 0
          6  AvgPx                    = 0.0000
         60  TransactTime             = 20260927-09:37:09.660
         10  CheckSum                 = 172
20260927-09:37:12.816 --> agent42            5 Order Cancel/Replace Request ZWZZT Buy 11=DEMO-1790501832816-4-R 41=DEMO-1790501828816-3 qty=800 @10.50
          8  BeginString              = FIX.4.2  (FIX version 4.2)
          9  BodyLength               = 172
         35  MsgType                  = G  (Order Cancel/Replace Request)
         49  SenderCompID             = AGENT
         56  TargetCompID             = ORDERECHO
         34  MsgSeqNum                = 5
         52  SendingTime              = 20260927-09:37:12.816
         11  ClOrdID                  = DEMO-1790501832816-4-R
         41  OrigClOrdID              = DEMO-1790501828816-3
         21  HandlInst                = 1  (Automated execution, private)
         55  Symbol                   = ZWZZT
         54  Side                     = 1  (Buy)
         38  OrderQty                 = 800
         40  OrdType                  = 2  (Limit)
         60  TransactTime             = 20260927-09:37:12.816
         44  Price                    = 10.50
         10  CheckSum                 = 074
```

Tag 32 prints as **LastShares** because the message is 4.2; the same tag in
the 4.4 CSCO fill prints as LastQty. That is the overlay's `tags` section.

### `timeline` for that order

```
$ .venv/bin/python orderecho_LogView.py timeline logs/fix/*_20260927.log \
      --no-color --clordid DEMO-1790501832816-4-R
Order chain for DEMO-1790501832816-4-R
  ClOrdIDs: DEMO-1790501828816-3, DEMO-1790501832816-4-R, DEMO-1790501836816-5-C
  OrderID : O-20260927-093636-3

  time                  dir  type                 exec/status                      qty           last     cum  leaves        avg
  2026-09-27T09:37:08.817 -->  New Order            - / -                            500              -       -       -          -
  2026-09-27T09:37:09.661 <--  Execution Report     0 (New) / 0 (New)                500              -       0     500     0.0000
  2026-09-27T09:37:12.816 -->  Order Cancel/Replace Request - / -                    800              -       -       -          -
  2026-09-27T09:37:12.819 <--  Execution Report     5 (Replace) / 0 (New)            800              -       0     800     0.0000
  2026-09-27T09:37:16.817 -->  Order Cancel Request - / -                              -              -       -       -          -
  2026-09-27T09:37:16.975 <--  Execution Report     4 (Canceled) / 4 (Canceled)      800              -       0       0     0.0000

Checks
  [PASS] cum_qty_monotonic: CumQty rose to 0 without ever falling
  [PASS] working_quantities: 2 working report(s) balanced
  [PASS] terminal_quantities: 1 terminal report(s) consistent
  [PASS] fill_quantities_sum: no fills in this chain
  [PASS] avg_px: no fills to average
  [PASS] exec_ids_unique: 3 ExecID(s), all distinct
  [PASS] order_id_constant: OrderID O-20260927-093636-3 throughout
  [PASS] nothing_after_terminal: terminal 39=4 was the last word
  [PASS] version_rules: every report matches its version's conventions
  [PASS] requests_answered: all 3 request(s) answered

  verdict: PASS
$ echo $?
0
```

### `stats`

```
$ .venv/bin/python orderecho_LogView.py stats logs/fix/*_20260927.log --no-color
42 message(s) from 3 file(s)
  first: 2026-09-27T09:36:40.922000+00:00
  last : 2026-09-27T09:39:03.530000+00:00

By session
  agent42        19
  agent44        17
  strict-broker  6

By message type
  8 Execution Report              15
  D New Order                     9
  5 Logout                        8
  A Logon                         8
  F Order Cancel Request          1
  G Order Cancel/Replace Request  1

By direction
  out  23
  in   19

Rejects by reason
  8: 103=0 (Broker/Exchange Option)  3
  8: 103=3 (Order Exceeds Limit)     2

Other
  resend requests   0
  gap fills         0
  injected          0
  bad checksum      0
  bad body length   0
  unparseable lines 0
```

Every one of the 42 lines parsed, and the reject reasons agree with the rules
that produced them: three rule rejects (two `K-M`, one strict-broker) and two
band rejects.

### `/viewer` served by the engine

```
$ curl -s -o /tmp/page.html -w "HTTP %{http_code}  %{size_download} bytes  %{content_type}\n" \
       http://127.0.0.1:8090/viewer
HTTP 200  14318 bytes  text/html; charset=utf-8
$ grep -o '<title>.*</title>' /tmp/page.html
<title>FIX log viewer</title>

$ curl -s http://127.0.0.1:8090/viewer/stats
{
    "messages": 46,
    "files": ["ORDERECHO-AGENT_20260926.log", "agent42_20260927.log",
              "agent44_20260927.log", "strict-broker_20260927.log"],
    "by_session": {"ORDERECHO-AGENT": 4, "agent42": 19, "agent44": 17,
                   "strict-broker": 6},
    "by_msg_type": {"A": 10, "5": 10, "D": 9, "8": 15, "G": 1, "F": 1},
    "by_direction": {"in": 21, "out": 25},
    "injected": 0, "rejects": 5, "bad_checksum": 0, "bad_length": 0,
    "unparseable_lines": 0,
    "first": "2026-09-26T03:05:00.052000+00:00",
    "last": "2026-09-27T09:39:03.530000+00:00"
}
```

46, not 42: the engine's viewer reads the whole `logs/fix` directory, so
yesterday's file is there too — which is what §8 asks for.

`GET /orders/{order_id}/timeline` over the control API:

```
$ curl -s http://127.0.0.1:8090/orders/O-20260927-093636-3/timeline
session    : agent42
seed       : O-20260927-093636-3
cl_ord_ids : DEMO-1790501828816-3, DEMO-1790501832816-4-R, DEMO-1790501836816-5-C
order_ids  : O-20260927-093636-3
steps      : 6
verdict    : PASS
  [PASS] cum_qty_monotonic: CumQty rose to 0 without ever falling
  [PASS] working_quantities: 2 working report(s) balanced
  [PASS] terminal_quantities: 1 terminal report(s) consistent
  [PASS] fill_quantities_sum: no fills in this chain
  [PASS] avg_px: no fills to average
  [PASS] exec_ids_unique: 3 ExecID(s), all distinct
  [PASS] order_id_constant: OrderID O-20260927-093636-3 throughout
  [PASS] nothing_after_terminal: terminal 39=4 was the last word
  [PASS] version_rules: every report matches its version's conventions
  [PASS] requests_answered: all 3 request(s) answered
```

That call is also the proof of decision 14 below: the engine had been
restarted since those orders were placed, so the live order book had never
heard of `O-20260927-093636-3` — the timeline came from the logs, and the
session name with it.

### Standalone: a QuickFIX-style log from somewhere else

A generated QuickFIX/J `messages.log` — their `<SendingTime> : <message>`
shape, SOH separators, FIX 4.4, CompIDs this emulator has never seen and a
filename that says nothing about a session:

```
20260927-13:02:11.002 : 8=FIX.4.4^A9=129^A35=D^A49=BUYSIDE^A56=BROKER^A34=2^A...
```

```
$ .venv/bin/python orderecho_LogView.py view quickfix_FIX.4.4-BUYSIDE-BROKER.messages.log --no-color
20260927-13:02:10.114  ?  -                  1 Logon                  108=30
20260927-13:02:10.180  ?  -                  1 Logon                  108=30
20260927-13:02:11.002  ?  -                  2 New Order              Buy 300 IBM Limit @212.40 11=QF-77
20260927-13:02:11.090  ?  -                  2 Execution Report       New/New cum=0 lv=300 avg=0 11=QF-77
20260927-13:02:12.441  ?  -                  3 Execution Report       Trade/Partially Filled 100@212.40 cum=100 lv=200 avg=212.40 11=QF-77
20260927-13:02:13.905  ?  -                  4 Execution Report       Trade/Filled 200@212.50 cum=300 lv=0 avg=212.4667 11=QF-77
```

The direction is `?` because nothing in a QuickFIX log records it. Naming our
side fixes that:

```
$ .venv/bin/python orderecho_LogView.py view quickfix_....log --no-color --me BROKER
20260927-13:02:10.114 --> -                  1 Logon                  108=30
20260927-13:02:10.180 <-- -                  1 Logon                  108=30
20260927-13:02:11.002 --> -                  2 New Order              Buy 300 IBM Limit @212.40 11=QF-77
20260927-13:02:11.090 <-- -                  2 Execution Report       New/New cum=0 lv=300 avg=0 11=QF-77
20260927-13:02:12.441 <-- -                  3 Execution Report       Trade/Partially Filled 100@212.40 cum=100 lv=200 avg=212.40 11=QF-77
20260927-13:02:13.905 <-- -                  4 Execution Report       Trade/Filled 200@212.50 cum=300 lv=0 avg=212.4667 11=QF-77

$ .venv/bin/python orderecho_LogView.py timeline quickfix_....log --no-color --me BROKER --clordid QF-77
Order chain for QF-77
  ClOrdIDs: QF-77
  OrderID : BRK-1001
  ...
Checks
  [PASS] cum_qty_monotonic: CumQty rose to 300 without ever falling
  [PASS] working_quantities: 2 working report(s) balanced
  [PASS] terminal_quantities: 1 terminal report(s) consistent
  [PASS] fill_quantities_sum: 2 fill(s) totalling 300 match CumQty
  [PASS] avg_px: AvgPx 212.4667 matches the fills to within 0.0001
  [PASS] exec_ids_unique: 3 ExecID(s), all distinct
  [PASS] order_id_constant: OrderID BRK-1001 throughout
  [PASS] nothing_after_terminal: terminal 39=2 was the last word
  [PASS] version_rules: every report matches its version's conventions
  [PASS] requests_answered: all 1 request(s) answered

  verdict: PASS
$ echo $?
0
```

AvgPx 212.4667 over 100@212.40 and 200@212.50 is checked in Decimal, not
float: the true average is 212.466̄, and the check accepts it to 0.0001.

### `serve`

```
$ .venv/bin/python orderecho_LogView.py serve logs/fix/*_20260927.log --port 8099
FIX log viewer on http://127.0.0.1:8099  (42 messages from 3 file(s))
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/
200
$ curl -s http://127.0.0.1:8099/stats | ...
messages 42 files ['agent42_20260927.log', 'agent44_20260927.log', 'strict-broker_20260927.log']
```

### Everything stopped

```
$ lsof -nP -iTCP:9878 -iTCP:9879 -iTCP:8090 -iTCP:8091 -iTCP:8099 -sTCP:LISTEN
(nothing)
$ pgrep -fl 'orderecho_Main|LogView'
(nothing)
```

Engine shutdown was clean: `Acceptor stopped` / `Shutdown complete`.

---

## 4. Decisions I made

1. **The overlay wins over `fix_tags.json`.** Where the base file and the
   overlay give different names or meanings for the same tag, the overlay is
   authoritative — it is the one that knows about versions, and it is the one
   we maintain. The base file supplies the other 165 tags.
2. **A missing base file is a warning, not an error.** `FixDictionary` runs on
   the overlay alone and records the reason in `.warnings`. Same for an
   unreadable file and for a file whose shape is not the list of tag objects
   we expect. §4 asks for this for the missing case; I extended it to the
   other two failure shapes because they are the same problem.
3. **`base_path=None` means "the default path", not "no base file".** Loading
   overlay-only is done by pointing at a path that does not exist. This is the
   scenario §4 describes (someone has not got FIXReader's file), and it keeps
   `None` meaning what it means everywhere else in this codebase.
4. **Session-level Rejects join the chain by RefSeqNum.** A `35=3` or `35=j`
   carries no ClOrdID, so ID-following alone never finds it — yet §5's check
   10 names a session Reject a valid answer to a request. `build_chain` now
   also pulls in any `3`/`j` whose tag 45 matches the MsgSeqNum of a request
   already in the chain. This was a code change, not a test change: the test
   was right and the code had a hole.
5. **Replays are shown but not checked.** An ExecutionReport with `43=Y`
   repeating an ExecID already seen is marked `replay` and excluded from all
   ten checks, so a Cook 4 resend does not read as a duplicate fill. It still
   appears in the timeline, greyed, so you can see it happened.
6. **Only check 10 can WARN; the other nine FAIL.** "A request went
   unanswered" is often just a log that ends mid-conversation. The other nine
   describe states that cannot be right. Exit codes follow: PASS 0, WARN 1,
   FAIL 2.
7. **A check that throws is a FAIL, not a crash.** `run_checks` wraps each one
   and turns an exception into a FAIL naming the check. A malformed log should
   not take the tool down.
8. **Placeholder OrderIDs do not join chains.** `NONE`, `UNKNOWN`, `0` and the
   empty string are excluded from the fixed-point expansion; otherwise every
   rejected order in the file would be one chain.
9. **The viewer holds a bounded window.** `MessageSource` keeps the newest
   20,000 messages. A day of heavy traffic would otherwise grow without limit
   in a long-running engine. The CLI has no such limit — it streams.
10. **Loopback only, refused rather than warned.** `serve` and `serve_files`
    exit 2 on a non-loopback `--host`. There is no auth and the logs contain
    every order that went through the emulator.
11. **The page is one file, fixed light theme, no stored state.** Persisting
    viewer state is out of scope per §2, so filters live in the URL only for
    the life of the tab.
12. **Reworded three prose comments rather than narrowing the build guard.**
    `test_only_one_module_names_the_build` flagged "Cook 6" in
    `orderecho_FixVersion.py` and `orderecho_Transport.py`. The guard has
    always counted prose, and it caught the same thing in Cook 5; the comments
    were stale anyway ("Cook 6 will run" → "the engine runs").
13. **`logon_timeout_sec` reads through `getattr` with `ROUTE_TIMEOUT` as the
    fallback.** A config object built before the field existed still routes.
14. **`/orders/{id}/timeline` falls back to the logs.** The live order book
    only holds the current run, but an order from an earlier run is still ours
    and still in our `logs/fix`. Rather than 404 an order we can plainly see,
    the endpoint builds the chain from the log and takes the session name from
    it. A 404 now means neither the book nor the logs know the ID. (I noticed
    this during the real run — see §3.)
15. **`/viewer/stats` now reports its source files.** The field existed and
    was always `[]`, because the incremental reader calls `parse_line`, which
    knows nothing about files. It records them itself now.
16. **The performance test times without `tracemalloc`.** Tracing costs about
    4× and would have been measuring the profiler: 5.2 s real, 22.7 s traced,
    against a 15 s budget. Memory is measured in a second pass.
17. **The demo run got a clean slate.** Today's pre-existing `logs/fix` files
    were moved to `logs/fix/pre-cook7/` (nothing deleted, and the viewer's
    glob does not recurse) and the engine was started once with
    `--reset-seqnums`, so the pasted output is one run and not a pile-up.
    Point `view` at `logs/fix/pre-cook7/*.log` to read the older ones.

---

## 5. Questions for you

1. **Should the `/orders/{id}/timeline` log fallback stay?** Decision 14 makes
   the endpoint answer for orders from earlier runs. The alternative reading
   of §8's "an engine order" is that only the live order book counts and an
   old ID should 404. I chose the useful one; say the word and it goes back
   to strict.
2. **Should `timeline` warn about an order that is still working?** Today the
   FDX partial (`cum=500 leaves=500`, nothing wrong with it) is a clean PASS,
   because every request was answered and every quantity adds up. A "this
   order never reached a terminal state" WARN would be easy, but it would fire
   constantly on a log tailed mid-session, so I did not add it.
3. **How far should the overlay grow?** It currently guarantees the 16 tags
   this emulator sends. Tags outside that set fall back to `fix_tags.json`,
   which has no version awareness — so a 4.4-only tag we never send could be
   named with 4.2 prose. Worth filling in, or is the emulator's own traffic
   the right boundary?
4. **20,000 messages is the viewer's window.** Right number? A busy day on
   three sessions would pass it, and the page would silently start at the
   newest 20,000.
5. **`fix_tags.json` is FIXReader's file, read-only and unversioned here.**
   Should Cook 8 vendor it with a version stamp and a check that the copy
   matches, or keep treating it as an optional input that may or may not be
   present?
