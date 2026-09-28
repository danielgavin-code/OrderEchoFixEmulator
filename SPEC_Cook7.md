# OrderEchoFixEmulator — Cook 7 Build Spec
**FIX log viewer: CLI + web, order timelines with consistency checks — usable standalone on any FIX log**

## 0. Ground rules
- No git commands. Build only §2 scope. Do not start Cook 8 (docs) or any Go work.
- All existing tests keep passing; golden fixtures untouched; isolation guard applies to all new storage.
- Report per §10 into REPORT_Cook7.md.

## 1. Context
The emulator writes excellent FIX logs; reading them is still raw `tag=value` soup. Cook 7 adds a viewer that decodes, filters, follows and explains FIX traffic. It must work **standalone on any FIX log** (ours, QuickFIX `messages.log`, other engines, raw lines pasted into a file) — not only the emulator's — because it may become a separate tool. Its order-timeline consistency checks are the prototype for the OrderEcho agent's future cert assertions, so keep them pure and well-specified.

## 2. Scope
In: §3 pure core, §4 dictionary, §5 timeline checks, §6 CLI, §7 web viewer, §8 control API integration, §9 tests, README section.
Out: editing/sending messages, remote log access, auth, persisting viewer state, FIX 5.0.

## 3. Pure core — `orderecho_LogParse.py`
- Input: text lines. Output: `ParsedMessage` objects: `ts` (if known), `direction` (`in`/`out`/`disc`/`unknown`), `session` (if known), `raw`, ordered `fields` (list of pairs, repeated tags preserved), `msg_type`, `begin_string`, `seq`, `injected` flag, `source_line_no`.
- Recognized formats, auto-detected per line:
  1. **OrderEcho FIX log** (Cook 1 §10A format, incl. `DISC` lines and `# injected:` suffix).
  2. **OrderEcho evidence JSONL** (message records only; events available separately for context).
  3. **Generic**: any line containing a FIX message — extract every `8=FIX…` through `10=NNN<delim>` occurrence, delimiter SOH, `|`, or `^A`. A leading timestamp is captured if it matches common forms (QuickFIX `YYYYMMDD-HH:MM:SS.sss : `, ISO 8601). Direction `unknown` unless inferable (§3 rule below).
- Direction inference for generic logs: if the user passes `--me <CompID>`, messages with `49=<me>` are `out`, `56=<me>` are `in`.
- Never raises on bad input: unparseable lines are skipped and counted; a message with a bad checksum or body length is kept and flagged `bad_checksum` / `bad_length` (the viewer shows it; it's evidence).
- Streaming: files are read line by line; memory proportional to what's displayed/retained, not file size.

## 4. Dictionary — `orderecho_FixDict.py`
- Load `dictionaries/fix_tags.json` (copied from FIXReader). Inspect its actual structure and adapt the loader to it; do not modify the file. Document its structure in the report.
- Provide: tag → name; (tag, value) → enum name where known; version awareness where it matters (e.g. `150=1/2` names in 4.2, `150=F` Trade in 4.4; tag 32 LastShares vs LastQty).
- A small hand-written overlay `dictionaries/overlay.json` guarantees enum names for the tags the viewer relies on even if the FIXReader file lacks them: 35, 39, 150, 54, 40, 59, 20, 21, 103, 102, 434, 373, 380, 141, 123, 43. Overlay wins on conflict only where the base file has no value.
- If `fix_tags.json` is missing or unreadable: fall back to overlay-only with a clear warning (never crash).

## 5. Order timelines — `orderecho_Timeline.py` (PURE)
- Group messages into **order chains**: start from any ClOrdID/OrderID; follow `41` (OrigClOrdID) links both directions and shared `37` (OrderID) to collect the full chain (new → replaces → cancel), including cancel rejects (35=9).
- Produce an ordered list of steps: ts, dir, MsgType, ExecType/OrdStatus (named), OrderQty, LastQty@LastPx, CumQty, LeavesQty, AvgPx, ClOrdID, text.
- **Consistency checks** (each yields `PASS`/`WARN`/`FAIL` with an explanation; never throws). Apply to outbound-from-the-sell-side ERs of the chain:
  1. CumQty never decreases.
  2. Working states (39 = 0, 1, 6, E, A): `CumQty + LeavesQty == OrderQty` (OrderQty as of that report).
  3. Terminal states (39 = 2, 4, 8, C, 3): `LeavesQty == 0`; if 39=2 then `CumQty == OrderQty`.
  4. Sum of LastQty over fill reports == final CumQty.
  5. AvgPx == Σ(LastQty×LastPx)/CumQty within 0.0001 (Decimal math).
  6. ExecIDs unique within the chain.
  7. OrderID constant across the chain.
  8. No fill or state change after a terminal state (except a replayed `43=Y` duplicate of an earlier report, which is ignored for checks but shown).
  9. Version rules: in 4.4, fills use `150=F` and tag 20 is absent; in 4.2, fills use `150=1/2`.
  10. Every inbound D/F/G in the chain received a response (ER or 35=9 or session Reject referencing its seq) — WARN if not, with the request's seq.
- Overall verdict = worst check. These checks are the spec for the future agent assertions: keep them in one module with one function per check and docstrings stating the rule.

## 6. CLI — `orderecho_LogView.py`
```
orderecho_LogView.py view <files...> [filters] [--decode] [--follow] [--no-color] [--me COMPID]
orderecho_LogView.py timeline <files...> (--clordid X | --order-id X) [--me COMPID]
orderecho_LogView.py stats <files...> [filters]
orderecho_LogView.py serve <files...> [--port 8091] [--me COMPID]
```
- **view**: one line per message — time, dir arrow, session, seq, MsgType name, and key fields by type (D: side qty sym ordtype px clordid; 8: exectype/ordstatus names, last@px, cum/leaves/avg, clordid; 9: response-to, reason name; 3/j: ref seq, reason name, text; admin: short). `--decode` adds an indented block of every field as `tag Name = value (EnumName)`.
- Filters (all combinable): `--session`, `--msg-type D,8`, `--dir in|out`, `--clordid X` (whole chain via §5), `--order-id`, `--symbol`, `--since/--until` (times), `--injected`, `--rejects` (35=3, 35=j, 35=9, 39=8), `--grep TEXT`.
- `--follow`: tail the file(s) and print new matching messages live; handles daily rollover of OrderEcho logs (new date file appears) and truncation.
- Color by default when stdout is a TTY: in vs out distinct, rejects red, fills green, injected/bad-checksum highlighted. `--no-color` or non-TTY → plain.
- **timeline**: the chain table (§5) followed by the check results and verdict. Exit code 0 PASS, 1 WARN, 2 FAIL (useful for scripts/agents).
- **stats**: counts by session, MsgType, direction; rejects by reason; resend/gap-fill counts; injected count; first/last timestamp; unparseable-line count.
- **serve**: start the §7 web viewer standalone over the given files (loopback only), no engine required.

## 7. Web viewer
- A FastAPI router `orderecho_LogViewer.py` + one self-contained page `viewer/index.html` (inline CSS/JS, no external requests, no build step).
- Page: message list (newest at bottom, auto-scroll toggle), filter bar mirroring §6 filters, click a message → decoded detail panel (§6 `--decode` content), "Timeline" button on any order message → chain table + check results with PASS/WARN/FAIL badges. Live mode polls for new messages (≈1s) when following files.
- Look: clean, fixed light theme, readable monospace for FIX, no theme switching.
- API under the router prefix: `GET /messages?after=<cursor>&<filters>`, `GET /message/{id}`, `GET /timeline?clordid=|order_id=`, `GET /stats`.
- Bind loopback only; refuse otherwise.

## 8. Control API integration
- The engine mounts the viewer router at `/viewer` over **its own** `logs/fix` files (all sessions, today's and previous days' files present), so `http://127.0.0.1:8090/viewer` works whenever the engine runs.
- `GET /orders/{order_id}/timeline` returns the §5 chain + checks for an engine order (JSON).

## 9. Tests
1. Parser: OrderEcho log lines (IN/OUT/DISC/injected), evidence JSONL, QuickFIX-style lines, `|`/SOH/`^A` delimiters, several messages in one line, garbage lines, bad checksum flagged not dropped.
2. Dictionary: loads the real `dictionaries/fix_tags.json`; overlay guarantees listed enums; version-aware names for 150 and 32; missing file → overlay-only with warning.
3. Chains: new→replace→replace→cancel followed from any ClOrdID in the chain; cancel reject included; unrelated orders excluded.
4. Checks: one passing chain per scenario (full fill, partials, replace, cancel, reject) from **real emulator output** (drive the pure core or a live session in-test); plus hand-crafted failing logs for each of the 10 checks, each producing FAIL/WARN with the right explanation; replayed PossDup reports ignored by checks.
5. CLI: `view` filters, `--decode`, `timeline` exit codes 0/1/2, `stats` numbers, `--follow` picks up appended lines and a new day-file (temp files), `--no-color` output has no ANSI codes.
6. Web: router endpoints (cursoring, filters, message detail, timeline), `/viewer` served by the engine, loopback enforcement, `serve` subcommand starts and serves a temp log.
7. Performance: `stats` over a generated 100k-message log completes in < 15 s on this machine and memory stays bounded (streaming).
All pass with `pytest -q`. No real network; isolation guard green.

## 10. Report → REPORT_Cook7.md
1. Files created/changed; structure of `fix_tags.json` and how it was loaded. 2. Full `pytest -q` output.
3. Real run: start the engine with `config/orderecho_multi.yaml`, drive a few orders through two sessions with the demo client (a fill, partials, a replace, a cancel, a band reject), then show: `view` of today's logs (plain), `view --clordid <replaced order> --decode`, `timeline` for that order with its checks, `stats`, and confirm `/viewer` loads (curl the page + one API call). Also run `view` on a generated QuickFIX-style log to show standalone use. Paste outputs. Stop everything afterwards.
4. Decisions I made. 5. Questions for me.
