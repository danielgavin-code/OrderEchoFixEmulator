# OrderEchoFixEmulator — Cook 5 Build Spec
**FIX 4.4 support (per session) + Cook 4 carry-overs**

## 0. Ground rules
- No git commands. Build only §2 scope. Do not start Cook 6 (multiple sessions) or any Go work.
- All existing tests keep passing, updated only where this spec changes behavior.
- Report per §10 into REPORT_Cook5.md.

## 1. Context
The emulator speaks FIX 4.2 only. Real counterparties are split between 4.2 and 4.4, so the emulator must speak both. The FIX version is a **per-session** property (Cook 6 will run several sessions at once, possibly mixed versions), so nothing about the version may be global.
Design rule: the order book decides *what happened* (ack, partial fill, fill, cancel, replace, reject); a per-version profile decides *how it is written in FIX*. The pure core stays pure.

## 2. Scope
In: §3 carry-overs, §4 version profiles, §5 FIX 4.4 behavior, §6 refactor with golden-output protection, §7 config, §8 demo/test client, §9 tests, README.
Out: multiple sessions, FIX 5.0/FIXT, repeating groups beyond what's listed, log viewer, docs site.

## 3. Carry-overs from Cook 4
3.1 **Band reference hole.**
- `price_band.lookup_timeout_ms` default → **3000**.
- New `pricing.warm_symbols: [AAPL, MSFT, SPY]` (default list; user-editable): fetched in the background at startup (alongside the existing SPY cookie warm-up), results cached. Startup never waits.
- New `price_band.on_no_reference: skip | reject` (default `skip`). `skip`: as Cook 4, plus an engine log **WARNING** each time. `reject`: D → reject ER with `58=No reference price available` (reason code per §5.4); G → 35=9 `102` per §5.4 with the same `58`.
3.2 **Band before rule.** The band check must run **before** rule matching, and the engine log lines must appear in that order. A symbol whose rule is `reject` but whose limit is outside the band gets the band reason (103=3 and band text), not the rule's.
3.3 **Version reporting.** A single constant `ORDERECHO_VERSION = "0.5.0"` and `ORDERECHO_BUILD = "cook5"` in one module; `/health` returns `{"ok": true, "version": ..., "build": ...}`; the startup banner shows both. No other place hard-codes a build name.
3.4 **Reset response.** `POST /session/reset-seqnums` returns `{next_out, next_in, archived_store}` (path or null).
3.5 **Test isolation (mandatory).** Every test that uses storage (seqnums, evidence, logs, msgstore, anything with a default path) must point it into `tmp_path`. Add a single helper that builds a fully isolated test config, and an autouse session-level guard in `conftest.py` that **fails the run** if any test creates or modifies files under the repo's `data/` or `logs/`. Any future storage key with a default must be covered by the helper.
3.6 **Demo client fixes.**
- ClOrdID = `DEMO-<unix ms>-<per-process counter>` — unique even when commands are pasted in bulk.
- `status` lists every order from the moment it is sent (state `SENT` until the first report arrives).
- Messages received with `43=Y` are printed with a `(PossDup)` marker.

## 4. Version profiles — `orderecho_FixVersion.py` (PURE)
- A `FixVersionProfile` per supported version: `FIX42`, `FIX44`. Looked up from the session's `fix_version` (`FIX.4.2` / `FIX.4.4`); unknown value → config load error.
- A profile owns: BeginString; required tags per inbound MsgType (D, F, G, and session messages); allowed enumerations (Side, OrdType, HandlInst, TimeInForce); how each report kind renders (ExecType/OrdStatus, whether tag 20 is present); reject-reason code mapping (§5.4).
- The order book emits **version-neutral** report objects, e.g. `ExecReport(kind=ACK|PARTIAL_FILL|FILL|CANCELED|REPLACED|PENDING_CANCEL|PENDING_REPLACE|REJECTED, reason=<neutral reason>, ...)` and `CancelReject(response_to=CANCEL|REPLACE, reason=<neutral reason>, ...)`. The session's profile renders them to body fields. Validation also consults the profile.
- Session-layer messages (Logon, Heartbeat, TestRequest, ResendRequest, SequenceReset, Logout, Reject, BusinessMessageReject) use the profile's BeginString; their tag content is unchanged between 4.2 and 4.4 for our scope.

## 5. FIX 4.4 behavior
5.1 **BeginString** `8=FIX.4.4` on everything the 4.4 session sends.
5.2 **ExecutionReport differences**
- No tag `20` (ExecTransType) — must never appear in 4.4 output.
- Fills: `150=F` (Trade) for both partial and full fills; `39=1` or `39=2` distinguishes them. (4.2 keeps `150=1` / `150=2`.)
- `32` is LastQty (same tag number, same value semantics).
- Ack `150=0`, cancel `150=4`, replace `150=5`, pending cancel `150=6`, pending replace `150=E`, reject `150=8` — same as 4.2.
- Replace ack `39` still follows `orders.replace_ack_ordstatus`.
- All other ER fields as in Cook 2 §5.3.
5.3 **Inbound validation differences**
- D: required `11, 55, 54, 60, 38, 40` (+`44` if limit). **`21` HandlInst is optional in 4.4** (still required in 4.2); if present it must be a valid value.
- G: required `11, 41, 55, 54, 60, 38, 40` (+`44` if limit); `21` optional.
- F: same as 4.2.
- Enumeration sets per the 4.4 spec for Side and OrdType (4.4 has more values; values valid in 4.4 but unsupported by us are business rejects, not structural).
5.4 **Reason codes**
| Neutral reason | 4.2 | 4.4 |
|---|---|---|
| Unsupported side / OrdType / TIF | `103=0` | `103=11` (Unsupported order characteristic) |
| Bad quantity (≤0, fractional) | `103=0` | `103=13` (Incorrect quantity) |
| Price ≤ 0 | `103=0` | `103=0` |
| Unknown symbol (rule) | rule's `reject_code` | rule's `reject_code` |
| Duplicate ClOrdID on D | `103=6` | `103=6` |
| Band / no reference | `103=3` / `103=0` | `103=3` / `103=99` (Other) |
| Cancel/replace: duplicate ClOrdID | `102=2` | `102=6` (Duplicate ClOrdID received) |
| Cancel/replace: other codes | as Cook 2 | same values |
5.5 **BeginString mismatch**: a Logon whose `8` doesn't match the session's version → Logout with `58=Incorrect BeginString, expected FIX.4.x`, then disconnect. Mid-session mismatch → same, plus session Reject is **not** sent (Logout + disconnect only). Evidence records it.
5.6 **Message store & version change**: stored messages carry the version they were sent with. If the engine starts with a different `fix_version` than the stored messages, archive the store at startup (engine log WARNING), so replay never sends another version's bytes.

## 6. Refactor safety — golden outputs
Before changing any rendering code, capture **golden outputs** from the current (Cook 4) code: a scripted scenario through the pure core (acks, partials, fills, cancel, replace, pending acks on, each reject type, cancel rejects, session rejects) with a FakeClock and static prices, saved as fixtures in `tests/golden/fix42/*.txt` (body fields in order, SendingTime/TransactTime normalized). After the refactor, the 4.2 profile must reproduce them **byte-for-byte**. Do not regenerate the golden files after the refactor starts; if one differs, the code is wrong.

## 7. Config
- `session.fix_version: FIX.4.2 | FIX.4.4` (existing key; now accepts 4.4).
- Ship `config/orderecho_fix44.yaml`: identical to the default config except `fix_version: FIX.4.4`.
- `/status` and the startup banner show the session's FIX version; evidence records `fix_version` in the startup event.

## 8. Demo client and test client
- Demo client: `--fix 4.2|4.4` (default: from the config it reads). Its parsing/printing handles both (e.g. prints `exec=TRADE` for `150=F`, plus status to show partial vs full).
- Test FIX client (`tests/fix_TestClient.py`): accepts a version parameter.

## 9. Tests
**Profiles & 4.4**
1. Golden 4.2 fixtures reproduced byte-for-byte after the refactor.
2. 4.4 ack/partial/fill/cancel/replace/pending/reject bodies: correct ExecType/OrdStatus, `150=F` on both fill kinds, **no tag 20 anywhere** (assert across every outbound 4.4 message in the scenario).
3. 4.4 D without `21` → accepted; 4.2 D without `21` → session Reject `373=1 371=21`.
4. 4.4 reason-code table (§5.4), each row, both versions.
5. BeginString mismatch on Logon (4.2 session gets 4.4 Logon and vice versa) → Logout text + disconnect; mid-session mismatch → same.
6. Store archived at startup when versions differ; replay in a 4.4 session sends 4.4 bytes with `43=Y`.
7. Parametrize the existing order-book and order-integration suites over both versions where the behavior is version-neutral.
**Carry-overs**
8. `warm_symbols` fetched in background at startup (monkeypatched, slow) without delaying startup; later lookups hit cache.
9. `on_no_reference: reject` → reject with the right code per version; `skip` → WARNING logged.
10. Band-before-rule: a `reject`-rule symbol with an out-of-band limit gets 103=3 band text; engine log shows BAND before rule matched.
11. `/health` shows version + build; reset response includes `archived_store`.
12. Isolation guard: a deliberately misconfigured test fixture writing to repo `data/` makes the guard fail (test the guard itself with a subprocess or by invoking its check function directly).
13. Demo client: bulk-pasted orders get unique ClOrdIDs; `status` shows `SENT` orders immediately; PossDup marker printed on replays; `--fix 4.4` session end-to-end (order, fill with `exec=TRADE`, quit).
All pass with `pytest -q`. No test touches the real network or repo `data/`/`logs/`.

## 10. Report → REPORT_Cook5.md
1. Files created/changed. 2. Full `pytest -q` output (and the count per version for parametrized suites).
3. Real run with `config/orderecho_fix44.yaml` and live pricing: demo client `session --fix 4.4` → market order on AAPL (ack + `exec=TRADE` fill), E–G partial order (two `150=F` partials), a band reject, then `resend 1 0` (PossDup markers). Paste client output, FIX log lines (show `8=FIX.4.4` and absence of `20=`), and engine log lines. Then the same market order against the default 4.2 config to show both versions from one codebase. Stop everything afterwards.
4. Decisions I made. 5. Questions for me.
