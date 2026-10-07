# Demo client

`orderecho_DemoClient.py` is a small FIX initiator for driving the emulator
before a real client exists. It is not the thing under test — it is the thing
that saves you writing one to see whether the emulator works.

Start the emulator first, then in another terminal:

```sh
.venv/bin/python orderecho_DemoClient.py order AAPL 1000 buy mkt
```

It logs on as the configured `target_comp_id` with `141=Y`, so a demo run never
fights persisted sequence numbers. Every inbound message prints as one readable
line, and it answers TestRequests.

## Subcommands

| Command | Does |
|---|---|
| `order <SYM> <QTY> <side> <type> [PX]` | Send one NewOrderSingle and wait for it to settle |
| `cancel-demo <SYM> <QTY> <side> <type> [PX]` | Send an order, then cancel it |
| `query <ClOrdID> <SYM> <side>` | Send an OrderStatusRequest (`35=H`) and print the answer |
| `session` | Stay connected and take typed commands |

Side is `buy`, `sell`, `sellshort` or `sellshortexempt`. Type is `limit`/`lmt`
or `market`/`mkt`; a limit order needs a price.

```sh
.venv/bin/python orderecho_DemoClient.py order AAPL 1000 buy mkt
.venv/bin/python orderecho_DemoClient.py order EFG 1000 sell lmt 10.25 --wait 5
.venv/bin/python orderecho_DemoClient.py cancel-demo HJK 500 buy mkt
```

`--wait` is a **maximum**, not a sleep (default 10 seconds). `order` returns as
soon as its order is Filled, Canceled or Rejected; `cancel-demo` as soon as the
cancel is acked or rejected.

`--no-exit` keeps the connection open after a one-shot command settles, and
takes typed commands instead of logging out.

`order` and `cancel-demo` also take:

| Option | Sends |
|---|---|
| `--tif day\|gtc\|ioc\|fok\|gtx\|gtd` | TimeInForce (59) |
| `--expire-time YYYYMMDD-HH:MM:SS` | ExpireTime (126), for `--tif gtd` |
| `--expire-date YYYYMMDD` | ExpireDate (432), for `--tif gtd` |
| `--account ACCT` | Account (1), for an engine with `orders.valid_accounts` |

```sh
.venv/bin/python orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 10.00 --tif ioc --account CERT1
.venv/bin/python orderecho_DemoClient.py --session agent42 order ZWZZT 100 buy lmt 10.00 --tif gtd --expire-time 20261001-15:00:00 --account CERT1
.venv/bin/python orderecho_DemoClient.py --session agent42 query DEMO-1790477068566-1 ZWZZT buy
```

## Global options

| Option | Does |
|---|---|
| `--config PATH` | which config to read the session details from |
| `--session ID` | use this session from a multi-session config |
| `--fix 4.2` / `4.4` | speak this version |
| `--host` / `--port` | override the address |

`--session` picks a session out of a multi-session config and mirrors it: the
client's SenderCompID becomes that session's TargetCompID, and it takes that
session's version and port. It cannot be combined with `--fix`, because the
session already says which version it speaks.

All of these may be given **before or after** the subcommand.

```sh
.venv/bin/python orderecho_DemoClient.py --session agent44 order AAPL 100 buy mkt
.venv/bin/python orderecho_DemoClient.py --fix 4.4 session
.venv/bin/python orderecho_DemoClient.py --port 9879 order AAPL 100 buy lmt 227.50
```

## Session mode

`session` keeps the connection open and reads commands, printing inbound
messages as they arrive:

```
> order AAPL 1000 buy mkt
> cancel DEMO-1790477068566
> replace DEMO-1790477068566 2000 10.50
> resend 1 0
> status
> help
> quit
```

| Command | Does |
|---|---|
| `order <SYM> <QTY> <buy\|sell> <mkt\|lmt> [PX] [tif=..] [expire=..] [account=..]` | Send a NewOrderSingle |
| `query <ClOrdID>` | OrderStatusRequest for one of your orders |
| `cancel <ClOrdID>` | Cancel one of your orders |
| `replace <ClOrdID> <QTY> [PX]` | Replace quantity, and price if given |
| `resend <begin> [end]` | Send a ResendRequest |
| `status` | Every order this client has sent, and its state |
| `quit` | Log out and exit |

`status` lists an order from the moment it is sent, not from the first report,
so an order that gets no answer is visible as such. A message that arrives with
`43=Y` is marked `(PossDup)`.

Ctrl+C logs out cleanly; press it twice to leave at once.

## Piped usage

Session mode reads standard input, so a scenario can be a single paste with no
typing:

```sh
.venv/bin/python orderecho_DemoClient.py session <<'EOF'
order AAPL 1000 buy lmt 227.50
order FDX 1000 buy lmt 250.00
order ZWZZT 500 buy lmt 10.00
status
quit
EOF
```

<div class="callout note" markdown="1">
<span class="label">Replacing a piped order</span>
`cancel` and `replace` need a ClOrdID, and the client generates those at send
time, so a fixed script cannot know them in advance. Either read them off the
output and send a second block, or use the
[control API](control-api.html#common-tasks), where cancels and fills are
addressed by OrderID and the engine tells you what those are.
</div>

## What it is not

It does not implement a full FIX session. It does the parts needed to be a
useful counterparty to the emulator: logon, heartbeats, TestRequest replies,
orders, cancels, replaces and ResendRequests. Sequence-number recovery,
persistence and reconnection are the emulator's job here, not the demo's.
