# Sessions & FIX versions

One engine can run several sessions at once. Each is fully independent: its own
CompIDs, FIX version, sequence numbers, message store, order book, rules, price
band and injection queue.

Shared engine-wide: pricing and its cache, and the ID generator. An OrderID is
unique across the engine, so it identifies an order without saying which
session it came from — which is why the order routes of the control API need no
session id.

## How a connection finds its session

FIX identifies a session by three things, and so does this:

**(BeginString, our SenderCompID, our TargetCompID)**

The first message on a new connection must be a Logon. Its `(8, 56, 49)` — the
version, and the CompIDs seen from our side — is matched against the sessions
bound to the port it arrived on.

| What arrived | What happens |
|---|---|
| An exact match | The connection is handed that session. |
| CompIDs match, version does not | A Logout saying `Incorrect BeginString, expected …`, naming every version those CompIDs could have used, then the door. |
| CompIDs match nothing | Dropped with **no reply**, a WARNING in the engine log and an evidence record. |
| A session that is already connected | The second connection is refused. |

<div class="callout note" markdown="1">
<span class="label">Why no reply for unknown CompIDs</span>
A session-level Reject needs a session to reject in, and there isn't one: we do
not know who this is or what dialect they speak. Saying nothing is what a real
venue does, and it is also what tells you your CompIDs are wrong rather than
your message.
</div>

A connection that says nothing at all is dropped after
`engine.logon_timeout_sec` (30 seconds by default).

Two sessions may share a port and even share CompIDs, as long as their versions
differ — the Logon's tag 8 tells them apart. `config/orderecho_multi.yaml` does
exactly this with `agent42` and `agent44`.

Ctrl+C logs every live session out at once and waits for their replies.

## Per-session files

| What | Where |
|---|---|
| Sequence numbers | `data/seqnums/<id>.json` |
| Outbound messages, for a real resend | `data/msgstore/<id>.jsonl` |
| FIX message log | `logs/fix/<id>_<YYYYMMDD>.log` |

The evidence file and the engine log stay one per engine run; every record and
every line names the session it came from.

For a single-session config the id is `<SENDER>-<TARGET>`, so the files keep the
names they always had.

## Choosing a version

The version belongs to the session:

```yaml
session:
  fix_version: FIX.4.2      # or FIX.4.4
```

The order book decides *what happened*; a profile in `orderecho_FixVersion.py`
decides *how it is written*. Nothing else in the engine knows which version it
is speaking, which is why adding one is a profile rather than a sweep through
the code.

## What differs between 4.2 and 4.4

Everything below is generated from the profiles themselves — the same objects
the engine renders with.

<!-- generate:er-fields -->

Beyond the ExecutionReport body:

| | FIX 4.2 | FIX 4.4 |
|---|---|---|
| Side values | 1–9 | 1–9 and A–G |
| OrdType values | 1–9, A–I, P | 1–9, A–I, J–M, P |
| TimeInForce values | 0–6 | 0–7 (adds At the Close) |
| HandlInst (21) on D and G | required | optional |

The reject codes also differ; they are on
[Order behavior](order-behavior.html#reject-codes).

Everything else — the ack, cancel, replace and pending ExecTypes, every other
field, and the whole session layer — is identical in both.

<div class="callout note" markdown="1">
<span class="label">Pinned</span>
The 4.2 and 4.4 renderings are held by golden fixtures under `tests/golden/`,
captured before the refactors that could have changed them and compared
byte-for-byte on every test run. If a change alters what goes on the wire, the
suite says so.
</div>
