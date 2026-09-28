# Session protocol

The session layer lives in `orderecho_Session.py` and is pure: it takes a
message and its current state, and returns a list of actions — send this,
record that, disconnect. It owns the sequence numbers and persists every change.

## Logon

The first message on a connection must be a Logon; anything else is not
answered. See [how a connection finds its
session](sessions.html#how-a-connection-finds-its-session) for what happens
when the identity does not match.

On a good Logon the emulator replies with its own, echoing `HeartBtInt` (108).
If the incoming Logon carries `141=Y`, both sequence numbers reset to 1 and the
stored message store is archived aside with a timestamp before a fresh one
starts.

A Logon whose MsgSeqNum is lower than expected — without a reset flag — is
refused with a Logout saying `MsgSeqNum too low, expecting N but received M`.
A Logon whose MsgSeqNum is higher starts the session and then asks for a
resend, because the gap is real and recoverable.

## Heartbeats and TestRequest

Driven entirely by the timer, using `HeartBtInt` from the Logon:

- Nothing sent for `HeartBtInt` seconds → send a Heartbeat.
- Nothing **received** for `HeartBtInt × (1 + heartbeat_grace_pct/100)` → send a
  TestRequest with a fresh `112`, once.
- A TestRequest outstanding for another `HeartBtInt` with no answer →
  disconnect.

An incoming TestRequest is answered with a Heartbeat carrying the same `112`.
A TestRequest with no `112` is rejected.

`heartbeat_grace_pct` exists so a client that is merely slow is not dropped for
it: at the default 20, a 30-second heartbeat interval tolerates 36 seconds of
silence.

## Sequence numbers

Each session keeps an inbound expectation and an outbound counter in
`data/seqnums/<id>.json`, written on every change, so they survive a restart.

| Incoming MsgSeqNum | What happens |
|---|---|
| Exactly what was expected | Processed; the expectation advances. |
| Higher than expected | A gap. An evidence record, then a ResendRequest for the missing range. The message itself is not processed. |
| Higher, with a resend already outstanding | Recorded, but no second ResendRequest. |
| Lower than expected, with `43=Y` | Ignored as a duplicate, and recorded. |
| Lower than expected, without `43=Y` | Logout and disconnect. This is unrecoverable. |

Resetting: a Logon with `141=Y`, `--reset-seqnums` at startup, or
`POST /sessions/{id}/reset-seqnums`.

## ResendRequest

By default a ResendRequest is answered by replaying **the actual messages that
were sent**, not by gap-filling the range.

```yaml
session:
  resend_mode: replay      # replay | gapfill
storage:
  msgstore_dir: data/msgstore
```

Every outbound message is written to `data/msgstore/<id>.jsonl` exactly as it
went on the wire, so a replay survives an engine restart. A replayed
application message comes back with its original `34`, `43=Y`, `122` set to the
original SendingTime, and a fresh `52`; everything else is identical and the
framing is recomputed.

Admin messages and sequence numbers we no longer hold are worthless to replay,
so consecutive runs of them collapse into a single SequenceReset-GapFill.
Nothing replayed consumes a new sequence number. One engine-log line says what
happened:

```
RESEND 2..9 -> replayed 6, gap-filled 1 run(s)
```

`resend_mode: gapfill` gap-fills the whole range instead.

## SequenceReset

`36` (NewSeqNo) below the current expectation is rejected with
`373=5` (value is incorrect). Otherwise the expectation moves to `36`, whether
or not `123=Y` is set.

## What gets rejected, and how

Three different things all get called "reject" in FIX, and they mean different
things here:

| | When | Carries |
|---|---|---|
| **Session Reject** `35=3` | The message is malformed as FIX — a required tag missing, a value we cannot use, a CompID problem | `45` RefSeqNum, `373` SessionRejectReason, `58` |
| **Business Reject** `35=j` | The message is well-formed but is a type we do not handle | `45`, `380=3` unsupported message type |
| **ExecutionReport reject** `35=8 39=8` | The order itself is refused — a rule, a price band, a bad quantity | `103` OrdRejReason, `58` |
| **OrderCancelReject** `35=9` | A cancel or replace is refused | `434` what it answers, `102` why |

The session-level reason codes in use:

| 373 | Meaning | Sent when |
|---|---|---|
| `1` | Required tag missing | A required tag for that message type is absent |
| `5` | Value is incorrect | A tag's value is not one we accept |
| `6` | Incorrect data format | A value will not parse as its type |
| `9` | CompID problem | The CompIDs on a message do not match the session |

A message with no usable MsgSeqNum cannot be sequenced at all, so it is
rejected with `373=1` and the session carries on.

## Logout

Either side may start it. The emulator sends a Logout with a `58` saying why,
moves to LOGOUT_SENT, and waits up to `logout_timeout_sec` for the reply before
disconnecting. An incoming Logout is answered with `Logout acknowledged` and
then the socket closes.

Ctrl+C logs out every live session at once and waits for their replies.

## Which BeginString

A counterparty that logs on with the wrong BeginString gets a Logout saying so,
and then the door. It never gets a session Reject, because a Reject is a message
in a dialect, and there is no shared dialect to send it in.
