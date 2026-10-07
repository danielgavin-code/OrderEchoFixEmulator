# OrderEchoFixEmulator — Cook 9 (E9) Build Spec
**Close the certification gaps the OrderEcho agent found + Python check fixes for Go parity**

## 0. Ground rules
- No git commands. Build only §2 scope. You may READ `../OrderEcho` (e.g. its cert suite YAML, REPORT_A2/A3) to understand what the agent expects; never modify it.
- Bump to `0.9.0` / `cook9`. All existing tests keep passing; **golden fixtures stay byte-identical** — every new behavior defaults OFF in the base config, and is switched on only in `config/orderecho_multi.yaml` (§8).
- Report per §10 into REPORT_Cook9.md.

## 1. Context
The Go agent ran FIXReader's Order Entry certification against this emulator: 46 auto/assisted PASS, but 11 cases N/A because the emulator lacks behaviors, and one PASS-with-warning (7.8, BusinessMessageReject missing a recommended tag). The agent's Go port of the 11 timeline checks also exposed three bugs in our Python checks. This cook makes the emulator a fuller counterparty and fixes the checks.

## 2. Scope
In: §3 TimeInForce, §4 lifecycle states, §5 order persistence + restart, §6 protocol fixes, §7 Python check fixes, §8 config, §9 tests.
Out: market data/order book matching, new FIX versions, docs redesign (but regenerate the docs so drift tests pass).

## 3. TimeInForce (59) — remove the "Day only" restriction
- Accept `0` Day, `1` GTC, `3` IOC, `4` FOK, `5` GTX, `6` GTD (needs `126` ExpireTime or `432` ExpireDate; missing → session Reject 373=1 371=126).
- Liquidity is rule-driven, so:
  - **IOC**: execute the rule's *immediate* outcome at once (full_fill → one full fill; partial_fill → its first fill only; ack_only/hold → nothing), then cancel any remainder immediately (`150=4 39=4`, `58=IOC remainder canceled`). Rule `reject` still rejects.
  - **FOK**: if the rule would fill the whole quantity (full_fill, or partial_fill with `then: fill_rest`) → one immediate full fill; otherwise → immediate `150=4 39=4`, `58=FOK not fully fillable`, CumQty 0.
  - **GTC / GTX**: behave like Day for fills; GTC orders persist (§5).
  - **GTD**: behaves like Day until its expiry, then `150=C 39=C` (Expired), LeavesQty 0, scheduled events dropped.
- Price band and rule order unchanged (band → rule → TIF handling).

## 4. Lifecycle states
- **Pending New**: `orders.send_pending_new` (default false). When true, every accepted D first gets `150=A 39=A`, then the normal `150=0` ack.
- **Done For Day**: `POST /orders/{id}/done-for-day` → `150=3 39=3`, LeavesQty 0, order closed.
- **Expire**: `POST /orders/{id}/expire` → `150=C 39=C` (manual expiry of any open order).
- **Too late to cancel**: `POST /orders/{id}/lock` puts an open order in a "fill in progress" state: cancel/replace requests get 35=9 `102=0` (Too late to cancel) until `POST /orders/{id}/unlock` or the next fill/terminal event clears it.
- **Account validation**: `orders.valid_accounts: []` (empty = no validation). When non-empty, a D with a missing or unlisted `1` → reject ER: 4.2 `103=0`, 4.4 `103=15` (Unknown account(s)), `58=Unknown account <x>`.
- All new endpoints follow Cook 3/4 rules (order checks before session checks, errors, engine log `API` lines, session-scoped equivalents under `/sessions/{id}/…` where orders are listed).
- Version profiles render `150=3/C/A` and `39=3/C/A` correctly for 4.2 and 4.4.

## 5. Order persistence and engine restart
- `orders.persist` (default false). When true, every open order (all TIFs) is written to `data/orders/<session_id>.jsonl` on every change (atomic append + periodic compaction); on startup, open orders are reloaded with their state, ClOrdID chains and OrderIDs, and scheduled events are rescheduled relative to now. Day orders older than the current UTC date are expired on load (`150=C`) when the session next logs on.
- `POST /admin/restart` — graceful in-process restart: Logout all sessions, persist, stop listeners, reload config and state from disk, start listeners again. Returns after listeners are back. (This lets a cert step "exchange restart" run without killing the process.)
- `OrderStatusRequest` (35=H): reply with an ER reflecting current state — 4.2: `20=3` (Status) with ExecType = current status; 4.4: `150=I` (Order Status). Unknown order → ER with `39=8`… per FIX convention for status of an unknown order (`58=Unknown order`).

## 6. Protocol fixes
6.1 **BusinessMessageReject recommended tags**: read the agent's case 7.8 in `../OrderEcho/certs/order_entry_fix42.yaml` to see which recommended tag it flagged; include it per the FIX spec (e.g. `379` BusinessRejectRefID when the rejected message carries a business-level ID).
6.2 **Gap queue + closed-range ResendRequest** (mirror the agent's A3 behavior): on an inbound gap, request `7=expected 16=<received−1>`, hold the triggering and later messages (bounded 1000, overflow → Logout + disconnect), process them in order once the gap is closed. A TestRequest behind a gap is answered after the fill.
6.3 **Negative price cache**: a failed/empty live lookup is cached (as `static:fallback`) for `pricing.negative_cache_seconds` (default 300) so only the first order per unpriceable symbol waits for the timeout.

## 7. Python timeline check fixes (Go parity)
Read `../OrderEcho/REPORT_A2.md` (Python-side findings) and `REPORT_A3.md` (§3.2 duplicate-reject chains) and fix, with tests:
1. A Reject only answers a request when it was **sent by the other side** (direction-aware: on a log the request's sender and the reject's sender must differ).
2. The parser must not end a message at `10=` embedded in another tag (e.g. `110=`): frame by BodyLength, then verify the trailer.
3. The framing check must compute checksums over the **original bytes** (no UTF-8 → latin-1 re-encoding): non-ASCII values must not be flagged.
4. **Duplicate-request rejects form their own chain**: a reject (39=8) of a ClOrdID reused from an existing chain, carrying a different OrderID, is split into its own chain so the original order's verdict is unaffected.
Add a regression test per item using the agent-style scenarios that exposed them.

## 8. Config
- New keys with defaults: `orders.send_pending_new: false`, `orders.valid_accounts: []`, `orders.persist: false`, `pricing.negative_cache_seconds: 300`.
- `config/orderecho_multi.yaml` defaults: `send_pending_new: true`, `send_pending_acks: true`, `persist: true`, `valid_accounts: [CERT1, CERT2]` (document that the agent's cert suite must send one of these on 7.6), so a cert run against the multi config can exercise everything.
- Regenerate the docs site (Cook 8 build) so new keys/endpoints/reason codes appear and drift tests pass; changelog entry for Cook 9.

## 9. Tests
- TIF: IOC/FOK/GTC/GTX/GTD per rule type, both versions; GTD expiry by FakeClock.
- Lifecycle: Pending New sequence; Done For Day; Expire; lock → 102=0 → unlock → cancel OK; account validation per version.
- Persistence: open orders survive a restart (process restart in an integration test and `/admin/restart`), chains intact, scheduled fills resume; Day orders from a previous date expire on next logon.
- OrderStatusRequest both versions, known and unknown.
- 35=j recommended tag; gap queue + closed-range resend (incl. TestRequest behind a gap answered); negative price cache.
- Python check fixes 1–4 with regressions; Cook 7 viewer tests still pass.
- Golden fixtures byte-identical; isolation guard green; `pytest -q` passes.

## 10. Report → REPORT_Cook9.md
1. Files. 2. `pytest -q` output. 3. Real run with `config/orderecho_multi.yaml`: demo client IOC, FOK, GTD-expiry, Pending New, `/admin/restart` with an open GTC order surviving, OrderStatusRequest; paste outputs + FIX log lines. Stop everything afterwards. 4. For each of the agent's 11 N/A cases + 7.8: what now makes it testable and which control endpoint (if any) the agent's emulator target should use. 5. Decisions. 6. Questions.
