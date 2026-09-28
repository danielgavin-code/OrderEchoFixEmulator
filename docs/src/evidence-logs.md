# Evidence & logs

Three things are written while the emulator runs: a FIX log per session per
day, one engine log per day, and one evidence file per run. Between them they
answer "what did the emulator actually do", which is the whole point of a
certification target.

## Where files go

| What | Where |
|---|---|
| FIX message log | `logs/fix/<session-id>_<YYYYMMDD>.log` |
| Engine log | `logs/engine/orderecho_<YYYYMMDD>.log` |
| Evidence | `data/evidence/<run_id>.jsonl` |
| Sequence numbers | `data/seqnums/<session-id>.json` |
| Outbound messages, for a real resend | `data/msgstore/<session-id>.jsonl` |

`data/` and `logs/` are created at startup and are gitignored. The evidence
file and the engine log are one per engine, not one per session; every record
and every line names the session it came from.

For a single-session config the session id is `<SENDER>-<TARGET>`.

## The FIX log

One line per message, raw message last so a line can be pasted straight into a
decoder:

```
20260927-09:37:00.813 IN   seq=2    35=D  8=FIX.4.2|9=146|35=D|49=AGENT|...|10=209|
20260927-09:37:00.836 OUT  seq=2    35=8  8=FIX.4.2|9=238|35=8|49=ORDERECHO|...|10=172|
```

| Column | What |
|---|---|
| timestamp | when the emulator wrote it, to the millisecond |
| direction | `IN`, `OUT`, or `DISC` for a message we discarded |
| `seq=` | MsgSeqNum, or `-` |
| `35=` | message type, or `-` |
| the rest | the raw message |

`logging.fix_delimiter` chooses `|` or a real SOH between fields. `|` is the
default because it survives being pasted into a terminal, a ticket or a chat
window.

A message the emulator broke on purpose ends its line with a comment saying
what was done:

```
... |10=999|  # injected: corrupt checksum
```

## The engine log

One line per thing worth knowing, at `logging.engine_level`:

```
20260927-09:36:35.587 INFO  session  engine  Acceptor listening on 127.0.0.1:9878 for agent42, agent44
20260927-09:37:00.820 INFO  session  agent42 BAND  W1 BUY LMT 900.00 ref=341.07 (live:yfinance) band=±10% -> REJECT
20260927-09:37:06.101 INFO  session  agent42 ORDER O-20260927-093636-2 NEW  cum=0 leaves=1000
```

`logging.console: true` also prints it to the terminal.

Two kinds of line are worth knowing about:

- `BAND ...` — one line per price-band decision, with the reference price and
  where it came from. See [Price band](order-behavior.html#price-band).
- `RESEND a..b -> replayed N, gap-filled M run(s)` — what a ResendRequest was
  answered with.

## Evidence

One JSON object per line, flushed as it is written, in
`data/evidence/<run_id>.jsonl`. The run id is the engine's start time, so a run
is one file and files never collide.

```json
{
  "ts": "2026-09-27T09:37:00.836Z",
  "run_id": "20260927-093636",
  "kind": "out",
  "session": "agent42",
  "seq": 2,
  "msg_type": "8",
  "raw": "8=FIX.4.2|9=238|35=8|...|10=172|",
  "fields": [["8", "FIX.4.2"], ["9", "238"], ["35", "8"]],
  "detail": null,
  "order": {"order_id": "O-20260927-093636-2", "ord_status": "0"},
  "injected": false
}
```

| Field | Means |
|---|---|
| `kind` | `in`, `out`, `discarded` or `event` |
| `session` | which session it belongs to |
| `fields` | an **ordered** list of `[tag, value]` pairs |
| `detail` | free text, for events and discards |
| `order` | an order snapshot, or null for anything session-level |
| `injected` | true when the emulator did this on purpose |

`fields` is a list rather than an object so field order survives and a repeated
tag is not collapsed — both matter in FIX. `raw` uses `|` instead of SOH.

Every outbound `35=8` or `35=9` is followed by an `event` record holding the
order state that resulted, so the file answers "what did it think the order was"
as well as "what did it send".

<div class="callout note" markdown="1">
<span class="label">Evidence is a viewer input too</span>
The log viewer reads evidence JSONL as happily as it reads a FIX log, so
`orderecho_LogView.py view data/evidence/*.jsonl` works. See
[Log viewer](log-viewer.html).
</div>

## The message store

`data/msgstore/<id>.jsonl` holds every outbound message exactly as it went on
the wire, so a ResendRequest can be answered with the real thing after a
restart. When sequence numbers reset — a Logon with `141=Y`,
`--reset-seqnums`, or `POST /sessions/{id}/reset-seqnums` — the store is
renamed aside with a timestamp and a fresh one starts. Archives are never
deleted.

## Tests never write here

A session-scoped guard fails the test run if anything writes to the
repository's own `data/` or `logs/`. Tests build their config through
`tests/isolation.isolated_config`, which puts every storage path under the
test's temporary directory.
