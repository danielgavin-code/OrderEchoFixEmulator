# OrderEchoFixEmulator — Cook 3 Build Spec
**Cook 2 follow-ups + local control API (manual order actions, session controls, deliberate FIX mischief)**

## 0. Ground rules
- No git commands. Build only §2 scope. Do not start Cook 4 (docs) or any Go work.
- All existing tests keep passing (update only where §3 changes behavior).
- Report per §10 into REPORT_Cook3.md.

## 1. Context
The control API is the emulator's backstage intercom. The OrderEcho agent (or a human with curl) uses it to make the emulator do things a real broker would do on its own: fill an order by hand, cancel it, hold it, log out, and deliberately break FIX when a test calls for it.
- It is **emulator-only**. Real FIX engines won't have one; the agent must never need it for core behavior.
- It never bypasses the pure core. Every order action goes through `OrderBook`; every outbound message goes through `Session` (so seq numbers, evidence and logs stay correct).
- Deliberate mischief is always recorded with `injected: true`.

## 2. Scope
In: §3 follow-ups, §4 API server, §5 order endpoints, §6 session endpoints, §7 injection endpoints, §8 read endpoints, §9 tests, README.
Out: auth/TLS (localhost only), docs site, rules hot-reload, order persistence across restarts, Go agent.

## 3. Cook 2 follow-ups
3.1 `pricing.timeout_sec` default → **10**. At startup, if `mode: live`, warm up yfinance in a background thread (one lookup of `SPY`, result discarded, failure only logged). Startup never waits for it.
3.2 Demo client: `--wait` becomes a **maximum**; the `order` command returns as soon as its order reaches a terminal state (Filled, Canceled, Rejected) or the max elapses. Default `--wait 10`. `cancel-demo` waits for the cancel ack or reject the same way.
3.3 Cook 2 open questions not answered here: keep your conservative choices.

## 4. API server — `orderecho_ControlApi.py`
- FastAPI app served by uvicorn **inside the engine's existing asyncio event loop** (`uvicorn.Server(config).serve()` as a task started by `orderecho_Main.py`). No threads; handlers call the engine directly and safely because everything runs on one loop.
- Config:
```yaml
control_api:
  enabled: true
  host: 127.0.0.1
  port: 8090
```
- Refuse to start if host is not a loopback address (clear config error).
- JSON in/out. Errors: HTTP 4xx with `{"error": "<code>", "detail": "<human text>"}`. Codes: `not_found`, `session_not_active`, `order_closed`, `invalid_request`, `conflict`.
- FastAPI's auto docs stay at `/docs` (the future human docs will live at `/guide`).
- Actions produced by API calls execute exactly like timer/inbound actions: through the session's send path, evidence, FIX log, engine log. Engine log line per API call: `API   POST /orders/O-…-1/fill qty=300 -> 200`.
- Add `fastapi`, `uvicorn`, `httpx` to requirements.

## 5. Order endpoints (pure-core additions in `orderecho_OrderBook.py`)
All require session ACTIVE → else 409 `session_not_active`. Unknown order → 404. Closed order → 409 `order_closed`.
| Endpoint | Body | Effect |
|---|---|---|
| `POST /orders/{order_id}/fill` | `{"qty": int, "price": "decimal-string"?}` | Manual fill via §5.5 rules of Cook 2. `qty` > 0 and ≤ leaves else 400. `price` defaults to the order's `fill_price`; must be > 0. AvgPx must account for fills at differing prices. |
| `POST /orders/{order_id}/cancel` | `{"text": str?}` | Unsolicited cancel (Cook 2 §5.6), `58` = text or `Canceled via control API`. |
| `POST /orders/{order_id}/hold` | — | Drops all scheduled events for the order; it stays open for manual control. Evidence records it. |
| `POST /orders/{order_id}/fill-rest` | `{"price": ...?}` | Fills all remaining leaves. |
Response for all: `{"order": <snapshot>, "sent": [<outbound msg summaries>]}` where a summary is `{"seq", "msg_type", "raw"}`.

## 6. Session endpoints
| Endpoint | Effect |
|---|---|
| `POST /session/test-request` | Send TestRequest now (TestReqID from the session counter). 409 if not ACTIVE. |
| `POST /session/logout` `{"text"?}` | `initiate_logout(text)`. 409 if not ACTIVE. |
| `POST /session/disconnect` | Drop the TCP connection without Logout (tests abrupt disconnects). Evidence: `injected: true`. |
| `POST /session/reset-seqnums` | Only when DISCONNECTED (else 409): reset store to 1/1. |

## 7. Injection endpoints (deliberate mischief)
Implemented in the session/transport send path. Every affected outbound message gets `injected: true` in evidence plus a `detail` describing the mutation; its FIX log line ends with `  # injected: <description>`.
| Endpoint | Body | Effect |
|---|---|---|
| `POST /inject/next` | `{"msg_type"?: "8", "set"?: {"58":"x","9999":"FOO"}, "remove"?: ["60"], "corrupt_checksum"?: bool, "count"?: 1}` | Queue a one-shot mutation applied to the next `count` outbound messages matching `msg_type` (any type if omitted). `set` overrides or appends tags; `remove` deletes tags. BodyLength and CheckSum are recomputed after mutation **unless** `corrupt_checksum` is true (then 10 is deliberately wrong). 8/9/10 cannot be set or removed (400). |
| `POST /inject/seq-gap` | `{"skip": int}` | Advance our outbound seq by `skip` without sending anything, so the counterparty sees a gap. Subsequent ResendRequests are answered by the normal gap-fill logic. |
| `POST /inject/duplicate-last` | `{"poss_dup": bool}` | Resend our last outbound message with the same seq; if `poss_dup`, add `43=Y` and `122=<original 52>`. |
| `GET /inject` | — | List pending mutations. |
| `DELETE /inject` | — | Clear pending mutations. |
Injection endpoints require session ACTIVE, except `GET`/`DELETE /inject`.

## 8. Read endpoints (no side effects, work in any state)
| Endpoint | Returns |
|---|---|
| `GET /health` | `{"ok": true, "version": "cook3"}` |
| `GET /status` | session state, CompIDs, peer address, next_in/next_out, HeartBtInt, last sent/received times, pending TestRequest, open-order count, run_id, evidence/log paths |
| `GET /orders?status=open\|closed\|all` | order snapshots (default all) |
| `GET /orders/{order_id}` | snapshot plus `cl_ord_id_chain` and its ExecutionReports sent (summaries) |
| `GET /messages?limit=50&direction=in\|out\|all` | most recent in/out messages for this run from an in-memory ring buffer (max 1000), newest last, each with `ts, kind, seq, msg_type, raw, injected` |
| `GET /price/{symbol}` | `{"symbol", "price", "source"}` via the pricing module (non-blocking; honors cache/timeout/fallback) |
| `GET /rules` | the loaded rules as JSON, in match order |

## 9. Tests
- Start the full engine (FIX acceptor + control API) on random free ports in each integration test; use `httpx.AsyncClient` and the Cook 1 test FIX client together. No network except localhost; static pricing.
- `test_ControlApi.py`:
  1. `/health`, `/status` before and after Logon.
  2. Order on `ZWZZT` (ack_only) → `POST fill 300` → client receives `150=1 39=1 32=300`; `POST fill-rest` → `150=2`; further fill → 409 `order_closed`.
  3. Manual fills at two different prices → AvgPx correct.
  4. `hold` on an E–G partial order → no scheduled fills arrive afterwards.
  5. `cancel` → unsolicited cancel received.
  6. Fill qty > leaves → 400; unknown order → 404; while logged out → 409 `session_not_active`.
  7. `inject/next` with `set {"9999":"FOO"}` on msg_type 8 → next ER carries 9999=FOO, valid checksum, evidence `injected: true`, FIX log line has `# injected:`; the following ER is clean.
  8. `inject/next` with `corrupt_checksum` → client-side codec discards the frame.
  9. `inject/next` removing `60` → ER arrives without 60.
  10. Setting tag 10 → 400.
  11. `inject/seq-gap skip 3` → client sees a gap; client sends ResendRequest → receives correct gap fill.
  12. `inject/duplicate-last poss_dup=true` → client receives same seq with `43=Y` and `122`.
  13. `session/test-request` → TestRequest arrives; `session/logout` → Logout arrives; `session/disconnect` → socket closes without Logout.
  14. `reset-seqnums` while ACTIVE → 409; while DISCONNECTED → 200 and store is 1/1.
  15. `/messages` ring buffer returns the right order and respects `limit`/`direction`.
  16. `/price/AAPL` in static mode → static price and source.
  17. Control API refuses a non-loopback host in config.
- §3 tests: yfinance warm-up runs in background (monkeypatched, slow) without delaying startup; demo client returns early on terminal state.
- All tests pass with `pytest -q`.

## 10. Report → REPORT_Cook3.md
1. Files created/changed.
2. Full `pytest -q` output.
3. A real run: start `orderecho_Main.py`; with the demo client or test client send an order on `ZWZZT`; then via `curl` show `/status`, `POST fill 300`, `POST inject/next` (set 9999=FOO), `POST fill-rest`, `/messages?limit=10`. Paste the curl commands and outputs, plus the matching FIX log lines. Stop everything afterwards.
4. Decisions I made. 5. Questions for me.
