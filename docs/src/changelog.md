# Changelog

The emulator was built in a series of overnight runs, each from a written
spec. Each one is a version.

## 0.8.0 — cook8

Documentation and polish.

- This guide: twelve pages in `docs/src/`, built to `docs/site/` by
  `orderecho_BuildDocs.py` and served by the engine at `/guide`. The reference
  tables — config keys, ExecutionReport fields, reject codes, API endpoints,
  timeline checks — are **generated from the code**, and a test fails if the
  committed site is stale.
- One stylesheet, `web/orderecho.css`, shared by the guide and the log viewer,
  built entirely from tokens in a single `:root` block.
- Log viewer fixes found in live testing: several files are merged by
  timestamp instead of read one after another; the web list shows exactly what
  the CLI shows, from the same code, with a seq column; replaces and cancels
  say what they are changing (`Replace A->B qty 500->800 px 10.00->10.50`).
- An eleventh timeline check, `framing_intact`, and message flags as badges on
  timeline rows.
- `engine.logon_timeout_sec` is configurable (default 30).

## 0.7.0 — cook7

The log viewer: CLI and web, usable standalone on any FIX log.

- `orderecho_LogParse.py`, a parser that reads this emulator's logs, evidence
  JSONL, QuickFIX-style logs and pasted lines, with SOH, `|` or `^A`
  delimiters. A badly framed message is kept and flagged rather than dropped.
- `orderecho_Timeline.py`: order chains reconstructed from any ClOrdID or
  OrderID, and ten consistency checks over them.
- `orderecho_LogView.py`: `view`, `timeline`, `stats` and `serve`, with
  filters, `--decode` and `--follow`.
- A web viewer, served standalone or by the engine at `/viewer`, and
  `GET /orders/{id}/timeline` on the control API.
- Version-aware tag names, from FIXReader's dictionary plus a hand-written
  overlay.

## 0.6.0 — cook6

Multiple concurrent sessions.

- One engine runs many sessions, each with its own CompIDs, version, sequence
  numbers, message store, order book, rules, band and injection queue.
- A connection finds its session by (BeginString, SenderCompID,
  TargetCompID), so two sessions can share a port if their versions differ.
- Session-scoped control API routes; the unscoped ones act on a default
  session.
- FIX 4.4 golden fixtures captured before the refactor, and unchanged after it.

## 0.5.0 — cook5

FIX 4.4, per session.

- Version profiles: the order book decides what happened, a profile decides how
  it is written. `150=F` and no tag 20 in 4.4, different reject codes, HandlInst
  optional.
- Golden fixtures pinning the 4.2 rendering byte for byte, captured before any
  of it moved.
- A test-isolation guard that fails the run if anything writes to the
  repository's own `data/` or `logs/`.

## 0.4.0 — cook4

Everything live testing turned up, plus three features.

- Instant acks: a market order is acked immediately and its fills wait for the
  quote, instead of the ack waiting.
- The price band, a sell-side fat-finger collar checked before the rules.
- ResendRequests answered by replaying the real messages from a message store
  that survives a restart.
- Demo client session mode.

## 0.3.0 — cook3

The control API.

- Manual order actions — fill, fill-rest, cancel, hold — going through the same
  order book as everything else.
- Session controls: TestRequest, Logout, disconnect, reset sequence numbers.
- Deliberate FIX mischief: set, remove or corrupt fields on the next outbound
  message, burn sequence numbers, duplicate the last message. Everything it
  does on purpose is labelled as such in the evidence and the logs.

## 0.2.0 — cook2

Orders.

- A pure order book: quantities, states, ExecIDs and a schedule.
- Behavior rules from config, matched first-wins on symbol or first letter.
- Pricing, live or static, with a fallback that never raises.
- The demo client.

## 0.1.0 — cook1

The session layer.

- A FIX codec, and a pure session core with no sockets and no wall clock:
  Logon, Heartbeat, TestRequest, sequence numbers, ResendRequest,
  SequenceReset, Logout and session Reject.
- An asyncio acceptor around it.
- The evidence log, the FIX log and the engine log.
