# OrderEchoFixEmulator — Cook 6 Build Spec
**Multiple concurrent sessions + Cook 5 carry-overs**

## 0. Ground rules
- No git commands. Build only §2 scope. Do not start Cook 7 (log viewer) or any Go work.
- All existing tests keep passing, updated only where this spec changes behavior. The Cook 5 golden fixtures (4.2) must still reproduce byte-for-byte; add 4.4 golden fixtures from current code BEFORE refactoring and hold them to the same rule.
- Test isolation guard (Cook 5 §3.5) stays mandatory for every new storage path.
- Report per §10 into REPORT_Cook6.md.

## 1. Context
Real counterparties connect many sessions to one engine — often mixed FIX versions. The emulator must run several sessions at once, each fully independent: CompIDs, FIX version, sequence numbers, message store, order book, rules, price band, injections. Pricing (and its cache) is shared engine-wide.

Session identity is the FIX standard triple: **(BeginString, our SenderCompID, our TargetCompID)**. Two sessions may share CompIDs if their versions differ.

## 2. Scope
In: §3 carry-overs, §4 config, §5 routing, §6 per-session state, §7 control API, §8 demo client, §9 tests, README.
Out: log viewer, docs site, TLS, dynamic add/remove of sessions at runtime (restart to change), session schedules.

## 3. Carry-overs from Cook 5
3.1 Demo client `session` mode: **Ctrl+C logs out cleanly** (same path as `quit`), even while waiting on stdin. A second Ctrl+C exits immediately.
3.2 Demo client global options (`--config --host --port --fix --session …`) are accepted **before or after** the subcommand.

## 4. Config
New multi-session shape:
```yaml
engine:
  host: 127.0.0.1
  fix_port: 9878              # default port for sessions without their own
  default_session: agent42    # used by legacy API routes (§7.3); optional

defaults:                     # applied to every session unless overridden
  heartbeat_grace_pct: 20
  logout_timeout_sec: 10
  resend_mode: replay
  orders: { ... }             # as today
  rules: [ ... ]              # as today
  price_band: { ... }         # as today

sessions:
  - id: agent42
    fix_version: FIX.4.2
    sender_comp_id: ORDERECHO
    target_comp_id: AGENT
  - id: agent44
    fix_version: FIX.4.4
    sender_comp_id: ORDERECHO
    target_comp_id: AGENT
  - id: strict-broker
    fix_version: FIX.4.2
    sender_comp_id: STRICTBRK
    target_comp_id: AGENT
    port: 9879                # optional own port
    price_band: { pct: 2, on_no_reference: reject }   # partial override, merged over defaults
    rules:                    # full replacement of the defaults list
      - { name: reject-all, match: { any: true }, behavior: reject, reject_code: 0, text: "Strict broker rejects everything" }
```
Rules:
- `orders` and `price_band` overrides **merge** key-by-key over `defaults`; `rules` **replaces** the list entirely.
- `pricing`, `storage`, `logging`, `control_api` stay top-level (engine-wide).
- Validation at load (clear errors naming the session): unique `id`s (`[A-Za-z0-9_-]+`); unique (fix_version, sender, target) per port; no FIX port equal to the control API port; `default_session` must exist.
- **Legacy config still works unchanged**: a top-level `session:` block (Cook 1–5 shape) loads as one session whose `id` defaults to `<SENDER>-<TARGET>` — so existing file names (seqnums, msgstore, FIX log) are unchanged for legacy configs. `config/orderecho.yaml` and `config/orderecho_fix44.yaml` keep working as-is.
- Ship `config/orderecho_multi.yaml` with exactly the three sessions above (rules/band defaults copied from the current default config).

## 5. Routing
- One acceptor per distinct port. On a new connection the first message must be a Logon (else disconnect, no reply — as today).
- Match the Logon's (`8`, `56` = our SenderCompID, `49` = our TargetCompID) against sessions bound to that port:
  - Exact match → hand the connection to that session.
  - CompIDs match a session but `8` doesn't → Logout `Incorrect BeginString, expected <v>` then disconnect (Cook 5 behavior). If CompIDs match sessions of several versions and none matches `8`, list the expected versions in the text.
  - No CompID match → disconnect **without reply**; engine log WARNING `unknown session: 8=… 49=… 56=… from <peer>`; engine-level evidence event.
- A session already connected refuses a second connection (as today). Different sessions connect concurrently.
- Each session's timers tick independently.
- Shutdown (Ctrl+C): initiate Logout on **all** ACTIVE sessions concurrently, wait up to each `logout_timeout_sec`, then exit.

## 6. Per-session state and files
- Per session: `Session`, `OrderBook`, rules, band config, injection queue, seq store, message store, `/messages` ring buffer.
- Engine-wide: pricing source + cache; one **ID generator** so OrderIDs (`O-<run_id>-<n>`) and ExecIDs are unique across all sessions.
- File names keyed by **session id**: `data/seqnums/<id>.json`, `data/msgstore/<id>.jsonl`, `logs/fix/<id>_<YYYYMMDD>.log`.
- Evidence: one file per engine run (as today); every record's `session` field = session id. Engine log: one file; the session column = session id.
- The message-store version-change archive rule (Cook 5 §5.6) applies per session.
- Startup banner: a table of sessions (id, version, sender→target, port, rules count, band pct).

## 7. Control API
7.1 **Session-scoped routes** (new):
| Route | Notes |
|---|---|
| `GET /sessions` | list: id, version, CompIDs, port, state, next_in/out, open orders |
| `GET /sessions/{id}/status` | as today's `/status`, for that session |
| `GET /sessions/{id}/orders?status=` | that session's orders |
| `GET /sessions/{id}/messages?limit=&direction=` | that session's ring buffer |
| `GET /sessions/{id}/rules` | effective rules + band for that session |
| `POST /sessions/{id}/test-request`, `/logout`, `/disconnect`, `/reset-seqnums` | as Cook 3 §6 |
| `POST/GET/DELETE /sessions/{id}/inject/...` | as Cook 3 §7, scoped |
Unknown session id → 404 `not_found`.
7.2 **Order routes stay global**: `/orders/{order_id}/...` finds the order in whichever session owns it (OrderIDs are unique engine-wide). Session-not-active checks use the owning session. `GET /orders` lists all sessions' orders, each with a `session` field.
7.3 **Legacy routes** (`/status`, `/session/*`, `/inject/*`, `/messages`, `/rules`) act on the **default session**: the only session if there is one; else `engine.default_session`; else 409 `ambiguous_session` with the list of session ids.
7.4 `/health` adds `"sessions": <count>`. `/price/{symbol}` unchanged (engine-wide).

## 8. Demo client
- `--session <id>`: read that session from the engine config and use its CompIDs (mirrored: client Sender = session target), version, and port. Mutually exclusive with `--fix`; explicit `--host/--port` still override.
- Without `--session`, behavior as today (first/only session, or the legacy block).

## 9. Tests
1. Legacy configs load as one session; existing file names unchanged; entire pre-Cook-6 suite passes; 4.2 and 4.4 golden fixtures byte-identical.
2. Two sessions on one port (4.2 + 4.4, same CompIDs) logged on concurrently: routed by BeginString; orders flow independently; seqnums independent; each FIX log contains only its own messages.
3. Unknown CompIDs → disconnect with no bytes sent; WARNING + evidence.
4. CompID match, wrong BeginString → Logout text lists expected version(s).
5. Per-session port (9879-style, random in tests) works alongside the shared port.
6. Rules override: the strict session rejects everything while the others fill; band override (2%) rejects a limit the default band (10%) accepts.
7. Injection queued on session A never touches session B; disconnecting A drops only A's injections.
8. OrderIDs/ExecIDs unique across sessions; `/orders/{id}/fill` works for an order in a non-default session; session-not-active uses the owning session.
9. `/sessions`, `/sessions/{id}/…` routes; unknown id → 404; legacy routes with one session work; with several and no `default_session` → 409 `ambiguous_session`; with `default_session` → that session.
10. Config validation errors (duplicate id, duplicate triple on a port, port clash with control API, bad default_session) each name the offending session.
11. Shutdown logs out all active sessions concurrently.
12. Demo client `--session agent44` connects with the right version/CompIDs; Ctrl+C (SIGINT) in session mode sends Logout; options accepted after the subcommand.
All pass with `pytest -q`. No real network; isolation guard green.

## 10. Report → REPORT_Cook6.md
1. Files created/changed. 2. Full `pytest -q` output.
3. Real run with `config/orderecho_multi.yaml`: start the engine; run three demo clients **at the same time** (`--session agent42`, `--session agent44`, `--session strict-broker`), each sending an AAPL market order; show `curl /sessions`, one `POST /orders/{id}/fill` on an agent44 `ZWZZT` order, and a connection with unknown CompIDs being dropped. Paste client outputs, the three FIX log files' relevant lines, and engine log lines. Stop everything afterwards.
4. Decisions I made. 5. Questions for me.
