# Log viewer

`orderecho_LogView.py` reads FIX logs and explains them. It is a separate
program: it never connects to anything, never writes to a log, and works just
as well on a log some other engine wrote — a QuickFIX `messages.log`, a
counterparty's file, or a handful of lines pasted into a file.

```sh
.venv/bin/python orderecho_LogView.py view logs/fix/*.log
```

Four subcommands: `view`, `timeline`, `stats` and `serve`.

## view

One line per message, in the order things happened:

```
20260927-09:36:40.922 --> agent42            1 Logon                  141=Y 108=30
20260927-09:37:00.813 --> agent42            2 New Order              Buy 1000 AAPL Limit @227.50 11=DEMO-1
20260927-09:37:00.836 <-- agent42            2 Execution Report       New/New cum=0 lv=1000 avg=0.0000 11=DEMO-1
20260927-09:37:01.411 <-- agent42            3 Execution Report       Fill/Filled 1000@227.50 cum=1000 lv=0 11=DEMO-1
```

The columns are the timestamp, the direction, the session, MsgSeqNum, the
message type by name, and a summary that depends on the type. `-->` is inbound
(the client to us), `<--` is outbound.

Several files are **merged by time**, not read one after another, so two
sessions logging at once read as one conversation. A line with no timestamp of
its own stays with the stamped line above it.

A replace or a cancel says what it is changing:

```
--> agent42  5 Order Cancel/Replace Request  Replace DEMO-1->DEMO-2-R qty 500->800 px 10.00->10.50
--> agent42  6 Order Cancel Request          Cancel DEMO-2-R->DEMO-3-C
```

The old values come from the earlier messages in the log; when the log does not
go back that far, the row just states what is being asked for.

### Filters

| Flag | Keeps |
|---|---|
| `--session agent42` | one session |
| `--msg-type D,8` | those message types |
| `--dir in` / `out` / `disc` | one direction |
| `--clordid C-1` | the whole chain that ClOrdID belongs to |
| `--order-id O-...` | the same, by OrderID |
| `--symbol AAPL` | one symbol |
| `--since 09:00` / `--until 09:05` | a time window |
| `--injected` | only deliberate mischief |
| `--rejects` | only `3`, `j`, `9` and `8` with `39=8` |
| `--grep TEXT` | raw substring match |

`--clordid` and `--order-id` follow the chain: give any ClOrdID from a
new → replace → replace → cancel sequence and you get all of it, including the
reports and any cancel rejects.

### Decoding

`--decode` expands every field with its name and, where the tag is enumerated,
the meaning of the value:

```
         11  ClOrdID                  = DEMO-1790501832816-4-R
         41  OrigClOrdID              = DEMO-1790501828816-3
         39  OrdStatus                = 0  (New)
        150  ExecType                 = 5  (Replace)
         32  LastShares               = 0
```

Names are version-aware: tag 32 is `LastShares` in 4.2 and `LastQty` in 4.4.
See [Tag names](#tag-names).

### Flags

A message that is unusual says so at the end of its line:

| Flag | Means |
|---|---|
| `INJECTED` | the emulator broke this on purpose |
| `POSSDUP` | `43=Y`, a resend |
| `BAD-CHECKSUM` | tag 10 does not match the body |
| `BAD-LENGTH` | tag 9 does not match the body |

A message with broken framing is **kept and flagged**, never dropped — it is
usually the thing you were looking for.

### Following

`--follow` keeps printing as the engine writes, picks up the next day's file
when the date rolls over, and survives a truncated file. `--interval` sets the
poll, default one second.

`--no-color` drops the ANSI codes; colour is off automatically when the output
is not a terminal or `NO_COLOR` is set. `--me OUR-COMPID` lets the parser infer
direction from SenderCompID and TargetCompID in logs that do not record one.

## timeline

```sh
.venv/bin/python orderecho_LogView.py timeline logs/fix/*.log --clordid DEMO-1
```

This prints one order's whole life — new, replaced, replaced again, cancelled,
rejected, however it went — and then runs consistency checks over it.

```
Order chain for DEMO-1790501832816-4-R
  ClOrdIDs: DEMO-...-3, DEMO-...-4-R, DEMO-...-5-C
  OrderID : O-20260927-093636-3

  time                  dir  type                 exec/status            qty   cum  leaves
  2026-09-27T09:37:08.817 -->  New Order          - / -                  500     -       -
  2026-09-27T09:37:09.661 <--  Execution Report   0 (New) / 0 (New)      500     0     500
  2026-09-27T09:37:12.819 <--  Execution Report   5 (Replace) / 0 (New)  800     0     800
  2026-09-27T09:37:16.975 <--  Execution Report   4 (Canceled) / 4 (…)   800     0       0

Checks
  [PASS] cum_qty_monotonic: CumQty rose to 0 without ever falling
  ...
  verdict: PASS
```

Each check prints the rule it enforces and, when it fails, where in the log it
went wrong — `seq=7 17=E-4 agent42_20260927.log:22`.

A replayed message — `43=Y` repeating an ExecID already seen — is shown but
excluded from the checks, so answering a ResendRequest does not look like a
duplicate fill.

### Exit codes

| Code | Means |
|---|---|
| `0` | every check passed |
| `1` | at least one warning |
| `2` | at least one failure, or no such order |

So a script can just look at the exit status.

### The checks

<!-- generate:timeline-checks -->

Only `requests_answered` and `framing_intact` can warn rather than fail. A log
that stops mid-conversation has unanswered requests through no fault of the
emulator, and a broken message means the contents cannot be trusted rather than
that they are wrong. Everything else describes a state that cannot be right.

A check that raises is itself a failure, named as one. A malformed log should
not take the tool down.

## stats

```sh
.venv/bin/python orderecho_LogView.py stats logs/fix/*.log
```

Messages per session, per type and per direction, the time span, how many lines
could not be parsed, and the reject reasons with their counts.

```
42 message(s) from 3 file(s)
  first: 2026-09-27T09:36:40.922000+00:00
  last : 2026-09-27T09:39:03.530000+00:00

By message type
  8 Execution Report              15
  D New Order                     9

Rejects by reason
  8: 103=0 (Broker/Exchange Option)  3
  8: 103=3 (Order Exceeds Limit)     2
```

It streams, so the size of the file does not decide the memory it uses: a
100,000-message log is a few seconds and a fraction of a megabyte.

## The web viewer

```sh
.venv/bin/python orderecho_LogView.py serve logs/fix/*.log
# FIX log viewer on http://127.0.0.1:8091  (42 messages from 3 file(s))
```

The same thing in a browser: a filter bar, a live-updating message list, a
decoded field table for whichever message you click, and a timeline with its
checks. It polls once a second, so it keeps up with a running engine.

The list shows exactly what the CLI shows — the same code produces both, so
they cannot drift.

<div class="callout warn" markdown="1">
<span class="label">Loopback only</span>
It binds to loopback and **refuses any other host**. There is no
authentication, and the logs contain every order that went through the
emulator.
</div>

When the control API is enabled the engine serves the same viewer over its own
logs at `http://127.0.0.1:8090/viewer`, and

```sh
curl -s localhost:8090/orders/O-20260927-093636-1/timeline
```

returns that order's timeline and checks as JSON.

## Tag names

Names and enum meanings come from `dictionaries/fix_tags.json` — FIXReader's
table, read and never modified — plus `dictionaries/overlay.json`, a small
hand-written file that guarantees the tags this emulator actually uses and
records where 4.2 and 4.4 disagree.

The overlay wins wherever the two differ, because it is the one that knows
about versions: tag 32 is `LastShares` in 4.2 and `LastQty` in 4.4, and `150=F`
is Trade in 4.4 where 4.2 has `1` and `2`.

If the base file is missing, unreadable, or not the shape we expect, the viewer
runs on the overlay alone and says so rather than failing.

## Reading somebody else's log

Nothing above is specific to this emulator. A QuickFIX-style
`<timestamp> : <message>` log, an ISO-stamped log, `|` or SOH or `^A`
delimiters, several messages on one line, garbage lines in between — all of it
parses, and what cannot be parsed is counted rather than thrown away.

QuickFIX logs do not record direction, so name your side:

```sh
.venv/bin/python orderecho_LogView.py view FIX.4.4-BUYSIDE-BROKER.messages.log --me BROKER
```
