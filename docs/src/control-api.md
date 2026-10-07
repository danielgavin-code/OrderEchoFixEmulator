# Control API

A local HTTP API for making the emulator do things a real broker would do on
its own: fill an order by hand, drop the connection, break a checksum on
purpose.

```yaml
control_api:
  enabled: true
  host: 127.0.0.1
  port: 8090
```

<div class="callout warn" markdown="1">
<span class="label">Local and unauthenticated</span>
Loopback addresses only — a non-loopback `host` is refused at startup. There
are no credentials and no TLS. It is a development tool, and it can move money
around in your test environment as freely as you can.
</div>

It is **emulator-only**. A real venue has nothing like it, and a client under
test must never need it for core behavior. It is for putting the client in a
situation, not for being part of the situation.

Nothing here bypasses the engine. Order actions go through the same pure order
book as scheduled fills, and every message goes out through the session, so
sequence numbers, evidence and logs stay correct.

Three things are served on this port besides the API: this guide at `/guide`,
the log viewer at `/viewer`, and FastAPI's own generated reference at `/docs`.

## Errors

Failures are 4xx with a body of the same shape:

```json
{"error": "not_found", "detail": "No such order: O-20260927-093636-9"}
```

| Code | Means |
|---|---|
| `not_found` | No such order, session, or symbol |
| `session_not_active` | The session is not logged on, so it cannot send |
| `order_closed` | The order has reached a terminal state |
| `invalid_request` | The body or query is wrong |
| `conflict` | Including `ambiguous_session` and `price_pending` |

Order and session **actions** need an ACTIVE session. The read endpoints work in
any state, including before anybody has connected.

## Scoped and unscoped routes

With several sessions, every session-scoped route takes an id:

```sh
curl -s localhost:8090/sessions
curl -s localhost:8090/sessions/agent44/status
curl -sX POST localhost:8090/sessions/agent44/test-request
```

The unscoped routes — `/status`, `/session/*`, `/inject/*`, `/messages`,
`/rules` — act on the **default session**: the only one if there is only one,
otherwise `engine.default_session`. With several sessions and no default they
answer `409 ambiguous_session` and list the ids rather than guess.

Order routes are always engine-wide, because an OrderID names its own session.

<div class="callout note" markdown="1">
<span class="label">Which to use</span>
Prefer the scoped routes in anything you write down. The unscoped ones are kept
so that one-session setups and older scripts keep working.
</div>

## Common tasks

**See what is going on:**

```sh
curl -s localhost:8090/health
curl -s localhost:8090/sessions
curl -s 'localhost:8090/orders?status=open'
curl -s localhost:8090/orders/O-20260927-093636-1
```

**Fill an order by hand:**

```sh
curl -sX POST localhost:8090/orders/O-20260927-093636-1/fill \
     -H 'content-type: application/json' -d '{"qty": 300}'

curl -sX POST localhost:8090/orders/O-20260927-093636-1/fill \
     -H 'content-type: application/json' -d '{"qty": 300, "price": "227.55"}'

curl -sX POST localhost:8090/orders/O-20260927-093636-1/fill-rest
```

A market order whose quote has not landed yet needs an explicit `price`;
without one the answer is `409 price_pending`.

**Hold an order, then let it go:**

```sh
curl -sX POST localhost:8090/orders/O-20260927-093636-1/hold      # stop the schedule
curl -sX POST localhost:8090/orders/O-20260927-093636-1/fill-rest # finish it by hand
```

**Cancel it as the counterparty would:**

```sh
curl -sX POST localhost:8090/orders/O-20260927-093636-1/cancel \
     -H 'content-type: application/json' -d '{"text": "pulled by hand"}'
```

**Lifecycle states, as a venue would send them:**

```sh
curl -sX POST localhost:8090/orders/O-20260927-093636-1/done-for-day  # 150=3 39=3
curl -sX POST localhost:8090/orders/O-20260927-093636-1/expire        # 150=C 39=C
curl -sX POST localhost:8090/orders/O-20260927-093636-1/lock          # cancels now get 102=0
curl -sX POST localhost:8090/orders/O-20260927-093636-1/unlock
```

Like the other order routes, these check the order first: an unknown order is
`404`, a closed one `409`, whether or not a session is logged on.

**Restart the engine without stopping the process:**

```sh
curl -sX POST localhost:8090/admin/restart
```

Every live session is logged out, the listeners stop, the config and every
session's state are reloaded from disk — open orders too, with
`orders.persist` — and the listeners start again on the same ports. The call
returns once they are back, with how many open orders each session has.

**Drive the session:**

```sh
curl -sX POST localhost:8090/sessions/agent42/test-request
curl -sX POST localhost:8090/sessions/agent42/logout \
     -H 'content-type: application/json' -d '{"text": "end of test"}'
curl -sX POST localhost:8090/sessions/agent42/disconnect   # no Logout at all
curl -sX POST localhost:8090/sessions/agent42/reset-seqnums
```

## Deliberate mischief

To test how a client copes with a badly behaved counterparty:

```sh
# the next ExecutionReport carries a bogus tag
curl -sX POST localhost:8090/sessions/agent42/inject/next \
     -H 'content-type: application/json' \
     -d '{"msg_type": "8", "set": {"9999": "FOO"}}'

# ... or arrives with a broken CheckSum, so the client must discard it
curl -sX POST localhost:8090/sessions/agent42/inject/next \
     -H 'content-type: application/json' \
     -d '{"msg_type": "8", "corrupt_checksum": true}'

# ... or without its TransactTime
curl -sX POST localhost:8090/sessions/agent42/inject/next \
     -H 'content-type: application/json' \
     -d '{"msg_type": "8", "remove": ["60"]}'

# burn three outbound sequence numbers, so the client must ask for a resend
curl -sX POST localhost:8090/sessions/agent42/inject/seq-gap \
     -H 'content-type: application/json' -d '{"skip": 3}'

# send the last message again, marked PossDup
curl -sX POST localhost:8090/sessions/agent42/inject/duplicate-last \
     -H 'content-type: application/json' -d '{"poss_dup": true}'

# what is queued, and forget it
curl -s localhost:8090/sessions/agent42/inject
curl -sX DELETE localhost:8090/sessions/agent42/inject
```

Mutations queued with `inject/next` are one-shot — `count` applies one to
several messages — and hit the next outbound message of the given type, or any
type if `msg_type` is omitted. BodyLength and CheckSum are recomputed after a
mutation unless `corrupt_checksum` asks for a broken one. Tags 8, 9 and 10
cannot be set or removed.

<div class="callout note" markdown="1">
<span class="label">Nothing deliberate is ever mistaken for a bug</span>
Every injected message is recorded with `injected: true` in the evidence log,
and its FIX log line ends with `# injected: <what was done>`. The log viewer
shows it as an `INJECTED` badge.
</div>

## Endpoint reference

Generated from the API's own OpenAPI document, so it lists what is actually
served. A `?` after a body field means it is optional.

<!-- generate:control-api -->
