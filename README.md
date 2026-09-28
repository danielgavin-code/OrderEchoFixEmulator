# OrderEchoFixEmulator

A local FIX counterparty emulator speaking **FIX 4.2 or FIX 4.4** over one or
many concurrent sessions. It runs as an acceptor — the broker/exchange side —
and answers orders the way a venue would: acks, partial fills, fills, cancels,
replaces and rejects, with a correct session layer underneath and a correct
order book behind it. The session core and the order book are pure, so the same
messages with the same config and prices produce the same output every time.
Everything it does is written to a FIX log, an engine log and an evidence file,
and a built-in viewer reads them back and checks them.

## Quick start

```sh
python3 -m venv .venv                       # Python 3.11 or newer
.venv/bin/pip install -r requirements.txt

.venv/bin/python orderecho_Main.py          # config/orderecho.yaml
```

In another terminal:

```sh
.venv/bin/python orderecho_DemoClient.py order AAPL 1000 buy lmt 227.50
```

Then look at what happened, in a browser at <http://127.0.0.1:8090/viewer> or
on the command line:

```sh
.venv/bin/python orderecho_LogView.py view logs/fix/*.log
```

## Documentation

The guide covers configuration, sessions and FIX versions, order behavior, the
session protocol, the control API, the logs, the log viewer, the demo client
and troubleshooting.

- **While the engine is running:** <http://127.0.0.1:8090/guide>
- **Offline:** open [`docs/site/index.html`](docs/site/index.html)

Its reference tables are generated from the code by
`orderecho_BuildDocs.py`; sources are in `docs/src/`. After changing the code
or those sources:

```sh
.venv/bin/python orderecho_BuildDocs.py
```

A test fails if the committed site is out of date.

## Tests

```sh
.venv/bin/python -m pytest -q
```

No test touches the network, and a guard fails the run if anything writes to
the repository's own `data/` or `logs/`.
