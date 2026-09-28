# Configuration

One YAML file describes everything: which sessions exist, what they answer
with, where files go. Three ship with the emulator.

| File | What it is |
|---|---|
| `config/orderecho.yaml` | One FIX 4.2 session. The default. |
| `config/orderecho_fix44.yaml` | The same, one line different: FIX 4.4. |
| `config/orderecho_multi.yaml` | Three sessions, two of them sharing a port. |

```sh
.venv/bin/python orderecho_Main.py --config config/orderecho_multi.yaml
```

A bad config is a startup error naming the key, not a surprise at run time.

## Two shapes

**One session** uses a `session:` block. This is the original shape and it still
works unchanged:

```yaml
session:
  fix_version: FIX.4.2
  sender_comp_id: ORDERECHO
  target_comp_id: AGENT
  host: 127.0.0.1
  port: 9878
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10
```

**Several sessions** use an `engine:` block and a `sessions:` list. Use one
shape or the other; both together is an error.

```yaml
engine:
  host: 127.0.0.1
  fix_port: 9878              # the port a session without its own uses
  default_session: agent42    # which session the unscoped API routes act on

sessions:
  - id: agent42
    fix_version: FIX.4.2
    sender_comp_id: ORDERECHO
    target_comp_id: AGENT

  - id: agent44
    fix_version: FIX.4.4      # same CompIDs, same port, different dialect
    sender_comp_id: ORDERECHO
    target_comp_id: AGENT
```

Session ids may contain letters, digits, `_` and `-`. They name files, so keep
them short. See [Sessions & FIX versions](sessions.html) for how a connection
finds its session.

## Defaults and overrides

`defaults:` is what every entry in `sessions:` starts from. An entry's own keys
win over it.

```yaml
defaults:
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10
  resend_mode: replay
  orders: { default_delay_ms: 500 }
  price_band: { enabled: true, pct: 10 }
  rules: [ ... ]

sessions:
  - id: strict-broker
    fix_version: FIX.4.2
    sender_comp_id: STRICTBRK
    target_comp_id: AGENT
    port: 9879                    # a port of its own
    price_band: { pct: 2 }        # merged over the defaults
    rules: [ ... ]                # replaces the default list outright
```

The two merge rules are worth remembering because they differ:

<div class="callout note" markdown="1">
<span class="label">How overrides combine</span>
`orders` and `price_band` **merge key by key** — a session that sets only
`pct` keeps the default `enabled`, `mode` and everything else. `rules`
**replaces** the whole list, because a half-overridden rule set would match in
an order nobody intended.
</div>

Anything not under `defaults:` or a session entry — `storage`, `logging`,
`control_api`, `pricing` — belongs to the engine and is shared by every session.
Pricing and its cache are engine-wide on purpose: two sessions asking about the
same symbol should not disagree.

## Full key reference

Generated from the config schema in `orderecho_Config.py`, so it cannot drift
from what the loader actually reads.

<!-- generate:config-reference -->

## A note on paths

`storage` and `logging` paths are relative to the working directory, and the
directories are created at startup. `data/` and `logs/` are gitignored. See
[Evidence & logs](evidence-logs.html) for what ends up in them.
