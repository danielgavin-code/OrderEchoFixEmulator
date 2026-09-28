# Quick start

Everything here is copy-and-paste. Nothing needs you to type at a prompt.

## Install

Python 3.11 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run the emulator

```sh
.venv/bin/python orderecho_Main.py
```

That uses `config/orderecho.yaml`: one FIX 4.2 session on `127.0.0.1:9878`,
control API on `127.0.0.1:8090`. The banner tells you what is listening:

```
OrderEchoFixEmulator 0.8.0 (cook8) - 1 FIX session
  config         : config/orderecho.yaml
  evidence file  : data/evidence/20260927-093636.jsonl
  fix log        : logs/fix
  engine log     : logs/engine

  session          version  route                      port   rules  band
  ---------------- -------- -------------------------- ------ ------ ----
  ORDERECHO-AGENT  FIX.4.2  ORDERECHO -> AGENT         9878   8      10%

  control api    : http://127.0.0.1:8090  (docs at /docs)
  guide          : http://127.0.0.1:8090/guide
  Ctrl+C to shut down.
```

Other configs ship with it:

```sh
.venv/bin/python orderecho_Main.py --config config/orderecho_fix44.yaml
.venv/bin/python orderecho_Main.py --config config/orderecho_multi.yaml
.venv/bin/python orderecho_Main.py --reset-seqnums     # both counters at 1
```

Ctrl+C sends a Logout, waits for the reply, and exits.

## Send your first order

In another terminal:

```sh
.venv/bin/python orderecho_DemoClient.py order AAPL 1000 buy lmt 227.50
```

`AAPL` starts with A, and the shipped rules fill anything in `A`–`D`
completely, so you get an ack and then a fill:

```
Connecting to 127.0.0.1:9878 as AGENT [FIX.4.2]
<- Logon                 HeartBtInt=30
-> NewOrderSingle      AAPL 1000 BUY LMT 227.50  11=DEMO-1790501820812-1
<- ExecutionReport     AAPL exec=NEW status=NEW  cum=0 leaves=1000 avg=0.0000
<- ExecutionReport     AAPL exec=FILL status=FILLED  last=1000@227.50
                       cum=1000 leaves=0 avg=227.5000
<- Logout              "Logout acknowledged"
```

Different first letters behave differently on purpose — see
[Order behavior](order-behavior.html). Try a few:

```sh
.venv/bin/python orderecho_DemoClient.py order FDX 1000 buy lmt 250.00   # partials
.venv/bin/python orderecho_DemoClient.py order HPQ 500 buy mkt           # cancelled
.venv/bin/python orderecho_DemoClient.py order KO 300 buy lmt 60.00      # rejected
.venv/bin/python orderecho_DemoClient.py order ZWZZT 500 buy lmt 10.00   # just rests
```

## Several orders in one connection

The `session` subcommand takes commands on standard input, so a whole scenario
is one paste:

```sh
.venv/bin/python orderecho_DemoClient.py session <<'EOF'
order AAPL 1000 buy lmt 227.50
order FDX 1000 buy lmt 250.00
status
quit
EOF
```

A replace or a cancel needs the ClOrdID of the order it is changing, and the
client makes those up as it goes, so for those use the control API instead —
or read the ClOrdID off the output and paste a second block. See
[Demo client](demo-client.html).

## See it in the viewer

While the engine is running, open:

```
http://127.0.0.1:8090/viewer
```

That is the log viewer over the engine's own logs: a filter bar, a live list of
every message, a decoded field table for whichever one you click, and a
timeline with consistency checks for any order. The guide you are reading is at
`/guide` on the same port.

The same thing on the command line:

```sh
.venv/bin/python orderecho_LogView.py view logs/fix/*.log
.venv/bin/python orderecho_LogView.py timeline logs/fix/*.log --clordid DEMO-1790501820812-1
.venv/bin/python orderecho_LogView.py stats logs/fix/*.log
```

## Make the emulator do something awkward

The control API is there for the things a venue would do on its own. Fill an
order half way by hand:

```sh
curl -s localhost:8090/orders?status=open
curl -sX POST localhost:8090/orders/O-20260927-093636-1/fill \
     -H 'content-type: application/json' -d '{"qty": 300}'
```

Or break the next ExecutionReport's checksum, so the client has to discard it:

```sh
curl -sX POST localhost:8090/sessions/ORDERECHO-AGENT/inject/next \
     -H 'content-type: application/json' \
     -d '{"msg_type": "8", "corrupt_checksum": true}'
```

See [Control API](control-api.html) for the rest.

## Stop everything

Ctrl+C in the engine's terminal. If something was left behind:

```sh
pkill -f orderecho_
```

## Run the tests

```sh
.venv/bin/python -m pytest -q
```

No test touches the network, and a guard fails the run if anything writes to
the repository's own `data/` or `logs/`.
