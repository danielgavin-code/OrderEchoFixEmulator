# OrderEchoFixEmulator — Cook 8 Build Spec
**Documentation site + log viewer fixes + shared visual style**

## 0. Ground rules
- No git commands. Build only §2 scope. No Go work.
- All existing tests keep passing; golden fixtures untouched; isolation guard applies.
- Bump `ORDERECHO_VERSION` to `0.8.0`, `ORDERECHO_BUILD` to `cook8` (update /health assertions).
- Report per §9 into REPORT_Cook8.md.

## 1. Context
The emulator is feature-complete. Cook 8 makes it understandable and pleasant to use: a documentation site served by the engine and readable offline, and fixes to the log viewer found in live testing. Docs must not drift from the code: reference sections are **generated** from the code, and tests fail if the committed docs are stale.
Look: clean, calm, fixed light theme (no theme switching), generous whitespace, readable. It will likely be redesigned later, so all styling lives in one stylesheet driven by CSS custom properties.

## 2. Scope
In: §3 shared style, §4 viewer fixes, §5 docs content, §6 docs build + serving, §7 drift protection, §8 tests, slim README.
Out: search engine, versioned docs, dark mode, external fonts/CDNs, Go agent docs.

## 3. Shared style — `web/orderecho.css`
- One stylesheet used by both `/guide` and `/viewer`. At the top, a single `:root` block of tokens: background, surface, text, muted text, border, one accent, success/warn/fail colors, in/out direction colors, sans font stack (system UI), mono font stack (system monospace), base size, spacing scale, radius, content max-width (~860px for prose).
- Components styled from tokens only (no hard-coded colors elsewhere): page shell with left sidebar nav + content column, headings, tables (zebra, sticky header where scrollable), code blocks with a copy button, callouts (note/warn), badges (PASS/WARN/FAIL, IN/OUT, INJECTED, POSSDUP, BAD CHECKSUM), key-value lists.
- Accessible contrast (WCAG AA for text). Works at 1280px and down to ~900px wide.
- The viewer's `index.html` drops its inline styles in favour of this file (still self-contained when served: the engine serves the CSS at `/assets/orderecho.css`; `serve` mode serves it too).

## 4. Log viewer fixes (from live testing)
4.1 **Chronological merge across files**: when several files are open (CLI `view`, `--rejects`, `stats` first/last, web list), messages are merged by timestamp; stable for equal timestamps; messages without timestamps keep their position relative to the preceding stamped message of the same file.
4.2 **One summary formatter** shared by CLI and web: the web list shows names exactly as the CLI does (e.g. `Buy 1000 AAPL Limit @227.50`, `New/New`, `PartialFill/Partially Filled`, `Trade/Filled` in 4.4). Web list gets a **seq** column.
4.3 **Replace/cancel rows show their substance**: G → `Replace <41>→<11> qty <old>→<new> px <old>→<new>` (old values from the chain when known, else just the new ones); F → `Cancel <41>→<11>`; 35=9 → response-to and reason names.
4.4 **Timeline surfaces message flags** (bad checksum, bad body length, injected, PossDup) as badges on the row, and adds an 11th check `framing_intact`: WARN if any message in the chain has bad checksum/length (explanation names the seq and file:line). Update the check list wherever it's documented/tested.

## 5. Docs content — `docs/src/*.md`
Pages (sidebar order):
1. **Overview** — what the emulator is, why (FIX certification with an LLM operator; emulator is one target), architecture (pure core, session/order book/profiles, transport, control API, evidence/logs) with one simple inline SVG diagram, design principles.
2. **Quick start** — install, run, first order with the demo client, see it in the viewer. Copy-paste blocks only (no interactive typing required; use the piped `session` pattern).
3. **Configuration** — concepts (legacy single-session vs multi-session, defaults vs per-session overrides) + **generated** full key reference.
4. **Sessions & FIX versions** — routing by BeginString+CompIDs, per-session ports, 4.2 vs 4.4 differences (**generated** ER field table per version).
5. **Order behavior** — rules and behaviors, scheduling, pricing (live/static/fallback/warm symbols), price band, instant acks; **generated** reject-reason table per version.
6. **Session protocol** — logon, heartbeats/TestRequest, sequence rules, resend replay vs gap fill, BeginString mismatch, what's rejected how.
7. **Control API** — overview, error codes, legacy vs session-scoped routes; **generated** endpoint reference (method, path, body, responses) from the FastAPI OpenAPI schema; curl examples for common tasks (fill, hold, inject, seq-gap).
8. **Evidence & logs** — FIX log format, engine log, evidence JSONL schema, message store, file locations.
9. **Log viewer** — CLI commands and filters, web viewer, timelines; **generated** list of the 11 checks from the check functions' docstrings; exit codes.
10. **Demo client** — commands, session mode, piped usage.
11. **Troubleshooting** — port in use, stale sequence numbers (reset), yfinance offline/fallback, Logon refused (CompIDs, BeginString), band skipped, leftover processes (`pkill -f orderecho_`).
12. **Changelog** — cooks 1–8, a few lines each.

## 6. Docs build and serving
- `orderecho_BuildDocs.py` renders `docs/src/*.md` → `docs/site/*.html` (plus `docs/site/assets/orderecho.css` copied from `web/`), using the `markdown` package (add to requirements). Generated sections are placeholders in the Markdown, e.g. `<!-- generate:config-reference -->`, filled at build time from the code.
- Output is **committed** and viewable offline by opening `docs/site/index.html`; all links relative.
- Build is deterministic (same input → byte-identical output; no timestamps).
- The engine serves the built site at `/guide` (and `/guide/<page>.html`); `/` redirects to `/guide`. The startup banner shows the guide URL.
- Slim `README.md`: one paragraph, quick start, link to `docs/site/index.html` and `/guide`.

## 7. Drift protection
- Test: building the docs into a temp dir produces output byte-identical to the committed `docs/site/` (fails with a message to run the build).
- Test: every API path mentioned in the docs exists in the OpenAPI schema; every CLI subcommand/flag mentioned exists in the argparse parsers (LogView, DemoClient, Main); every config key mentioned exists in the config schema.
- Test: every internal link and anchor in `docs/site/` resolves; no external URLs except none (offline-safe).

## 8. Tests
1. §4.1 merge order across files (incl. equal and missing timestamps); §4.2 CLI and web summaries identical for a corpus of messages; seq column present; §4.3 G/F/9 row text; §4.4 flags on rows and `framing_intact` WARN with seq + file:line (use the tamper scenario: sed-edit a CumQty, which breaks the checksum).
2. `/assets/orderecho.css` served; `/viewer` and `/guide` reference it; no hard-coded colors outside `:root` in the CSS (simple lint test).
3. Docs build deterministic; committed site fresh (§7); link check; drift checks.
4. `/guide` serves index and every page; `/` redirects; banner shows the URL.
All pass with `pytest -q`. No real network; isolation guard green.

## 9. Report → REPORT_Cook8.md
1. Files created/changed. 2. Full `pytest -q` output.
3. List of doc pages with word counts and which sections are generated.
4. A **design notes** section: the token values chosen and the component inventory — written so a designer can critique and propose a new token set without reading code.
5. Real run: start the engine (multi config), curl `/guide` (status + title), one generated page excerpt (reject-code table), viewer list JSON showing merged order + names + seq, and the tamper timeline showing `framing_intact` WARN. Stop everything afterwards.
6. Decisions I made. 7. Questions for me.
