# Troubleshooting

## The port is already in use

```
error: cannot listen on 127.0.0.1:9878: [Errno 48] Address already in use
```

Usually a previous run that did not exit. Find it:

```sh
lsof -nP -iTCP:9878 -sTCP:LISTEN
```

and stop it, or stop everything this emulator left behind:

```sh
pkill -f orderecho_
```

The control API port (8090) and the standalone viewer port (8091) fail the same
way. A multi-session config binds one acceptor per distinct port, so the error
names whichever one clashed.

## Leftover processes

An engine started in the background outlives the terminal that started it.

```sh
pgrep -fl orderecho_          # what is running
pkill -f orderecho_           # stop all of it
```

## Logon is refused, or nothing happens at all

Three different symptoms, three different causes.

**A Logout saying `Incorrect BeginString, expected …`.** The CompIDs matched a
session but the version did not. The message names every version those CompIDs
could have used. Fix `fix_version` on your side or point at the right session.

**Nothing at all — the socket just closes.** The CompIDs matched no session on
that port. This is deliberate: there is no session to reject in. Check
`sender_comp_id` and `target_comp_id`, and remember they are **swapped** as
seen from each side — the emulator's `sender_comp_id` is your TargetCompID.
The engine log has a WARNING naming what arrived.

**A Logout saying `MsgSeqNum too low`.** See below.

To see what the sessions expect:

```sh
curl -s localhost:8090/sessions
```

## Stale sequence numbers

Sequence numbers persist across restarts, in `data/seqnums/<id>.json`. A client
that has forgotten its own numbers will be told its MsgSeqNum is too low.

Three ways to start clean, in increasing order of bluntness:

```sh
# the client asks for it, in its Logon
141=Y

# one session, while the engine runs
curl -sX POST localhost:8090/sessions/agent42/reset-seqnums

# every session, at startup
.venv/bin/python orderecho_Main.py --reset-seqnums
```

The demo client always sends `141=Y`, which is why it never hits this.

A reset archives the message store aside with a timestamp rather than deleting
it, so an earlier run's messages are still there if you need them.

## yfinance is offline, or slow

Live pricing needs the network. When it fails — exception, timeout, missing or
non-positive price — the emulator falls back to the static table with source
`static:fallback` and a WARNING in the engine log. It never raises and never
blocks; one order is slower, and that is all.

If you want no network at all:

```yaml
pricing:
  mode: static
  static:
    AAPL: 227.50
    default: 100.00
```

`yfinance` is imported lazily, so static mode works without it installed.

To see what price would be used for a symbol, and where it came from:

```sh
curl -s localhost:8090/price/AAPL
```

## The price band did not fire

The band is skipped, never failed, in several situations. The engine log says
which:

- the band is off for that session, or a rule set `price_band_pct: off`;
- the order is a market order — the band only checks limits;
- no reference price arrived inside `lookup_timeout_ms` and `on_no_reference`
  is `skip` (the default), which logs a WARNING;
- the only quote available came from the static fallback and
  `enforce_on_fallback` is false;
- the price was outside the band on the harmless side, and `mode` is
  `aggressive`.

`GET /rules` reports the effective band for each rule, which is usually faster
than reading the config.

## The API says `409 ambiguous_session`

An unscoped route was used with several sessions and no default. Either name
the session:

```sh
curl -s localhost:8090/sessions/agent42/status
```

or set `engine.default_session` in the config. The error body lists the ids it
could have meant.

## The order routes say `not_found` for an order I can see

Order state lives in memory for one engine run. After a restart the order book
is empty, even though the logs still hold everything.

The timeline route is the exception: it falls back to the logs, so
`GET /orders/{id}/timeline` still answers for an order from an earlier run.

## The docs are out of date

The reference tables are generated. If a test says `docs/site is stale`:

```sh
.venv/bin/python orderecho_BuildDocs.py
```

That rebuilds `docs/site/` from `docs/src/` and the current code.

## The test suite fails with a writing guard

```
tests wrote to the repository's own data/ or logs/
```

A test built its config without `tests/isolation.isolated_config`. Every test
must put its storage paths under `tmp_path`; the guard exists because a test
that writes to the real `data/` corrupts the next run's sequence numbers.
