# Cook 8 — the guide, the stylesheet, and the viewer fixes

OrderEchoFixEmulator **0.8.0 (cook8)**. Everything in SPEC_Cook8.md §3–§8 is
built: a shared stylesheet, four log-viewer fixes found in live testing, a
twelve-page documentation site whose reference tables are generated from the
code, and the tests that stop any of it drifting.

`pytest -q`: **706 passed**, up from 651 at the end of Cook 7.

---

## 1. Files

### Created

| File | Lines | What it is |
|---|---|---|
| `web/orderecho.css` | 560 | §3 the one stylesheet, 41 tokens and nothing else |
| `orderecho_Summary.py` | 237 | §4.2 the one summary formatter, shared by the CLI and the web |
| `orderecho_BuildDocs.py` | 479 | §6 `docs/src/*.md` → `docs/site/*.html`, with the generators |
| `docs/src/*.md` | 12 pages | §5 the guide, ~9,500 words of prose |
| `docs/site/*` | 13 files, 172 KB | §6 the built site, committed, offline-safe |
| `tests/test_Docs.py` | 335 | §7, §8.2–8.3, 21 tests |
| `tests/test_Merge.py` | 135 | §4.1, 10 tests |

### Changed

| File | Change |
|---|---|
| `orderecho_Version.py` | `0.8.0` / `cook8` |
| `orderecho_LogParse.py` | `merge_chronologically()` — §4.1 |
| `orderecho_LogView.py` | uses the shared summary, merges before printing, carries a `SummaryContext`, shows flags on timeline rows |
| `orderecho_LogViewer.py` | merges on refresh, sends the shared summary with each message, serves `/assets/orderecho.css` and `/guide`, cursor fix |
| `orderecho_Timeline.py` | `check_framing_intact` (the 11th check), flags on `Step.as_dict` |
| `orderecho_Config.py` | `CONFIG_SCHEMA` and `config_key_names()` — one description of the YAML, for the generated reference and the drift test |
| `orderecho_ControlApi.py` | a docstring on every route, mounts `/guide` and the assets router, `/` redirects to `/guide` |
| `orderecho_Main.py` | banner shows the guide and viewer URLs; `build_parser()` split out |
| `orderecho_DemoClient.py` | `build_parser()` split out of `parse_args` |
| `viewer/index.html` | 328 → 251 lines: inline styles gone, no client-side formatter, seq column |
| `README.md` | cut to 60 lines; everything else moved into the guide |
| `requirements.txt` | `markdown` |
| `tests/test_CarryOvers.py`, `test_Timeline.py`, `test_LogView.py`, `test_Viewer.py` | version assertions, 10 → 11 checks, and 25 new tests |

---

## 2. `pytest -q`

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 40%]
........................................................................ [ 50%]
........................................................................ [ 61%]
........................................................................ [ 71%]
........................................................................ [ 81%]
........................................................................ [ 91%]
..........................................................               [100%]
706 passed in 105.94s (0:01:45)
```

No warnings, no skips, no xfails. The isolation guard is green and the golden
fixtures are untouched.

---

## 3. The guide

Twelve pages, ~9,500 words of hand-written prose. The word counts below are
prose only — the generated tables are not written by hand, so they are not
counted.

| # | Page | Words | Built | Generated section |
|---|---|---|---|---|
| 1 | Overview | 989 | 10.7 KB | — |
| 2 | Quick start | 591 | 7.9 KB | — |
| 3 | Configuration | 464 | 17.8 KB | `config-reference` |
| 4 | Sessions & FIX versions | 634 | 11.4 KB | `er-fields` |
| 5 | Order behavior | 1,016 | 12.6 KB | `reject-reasons` |
| 6 | Session protocol | 880 | 9.4 KB | — |
| 7 | Control API | 833 | 17.2 KB | `control-api` |
| 8 | Evidence & logs | 710 | 8.8 KB | — |
| 9 | Log viewer | 1,289 | 14.8 KB | `timeline-checks` |
| 10 | Demo client | 684 | 8.0 KB | — |
| 11 | Troubleshooting | 752 | 8.4 KB | — |
| 12 | Changelog | 655 | 7.3 KB | — |
| | **Total** | **9,497** | **172 KB** | 5 generators |

Note how the generated pages are the heavy ones: Configuration is 464 words of
prose and 17.8 KB of HTML, because 59 key rows across 11 sections came out of
the code rather than out of me.

### What each generator reads

| Placeholder | Source | Produces |
|---|---|---|
| `config-reference` | `orderecho_Config.CONFIG_SCHEMA` — built from the dataclass fields | 11 sections, every key with its type, default and allowed values |
| `er-fields` | `FixVersionProfile.render_exec_report()` on a full and an empty report | The ExecutionReport body per version, in tag order, marking which fields are conditional, plus a diff of the two |
| `reject-reasons` | `profile.reject_reasons`, `cancel_reject_reasons`, `RESPONSE_TO_VALUES` | Tag 103 and 102 per reason per version, with the enum names from the FIX dictionary |
| `control-api` | `build_app(stub).openapi()` | Every route: method, path, docstring, body fields, response codes |
| `timeline-checks` | `CHECKS`, each run against an empty chain | All 11 checks with their docstring and the rule string they report |

The OpenAPI document is produced without starting an engine: `build_app` only
closes over the transport, so a stand-in with a config describes the routes.
No socket is opened.

**Making the endpoint table worth having** meant the routes needed to say what
they do. Only one of them had a docstring, so the table was FastAPI's
auto-generated titles ("Legacy Clear Injections"). I wrote one line for each of
the other 38. They show at `/docs` too, so FastAPI's own reference improved
with it.

### Drift protection (§7)

Five tests, all of which fail loudly rather than quietly:

- **Deterministic build** — builds twice into two temp directories and compares
  bytes.
- **The committed site is fresh** — builds into a temp directory and compares
  with `docs/site`, with the fix in the failure message. There is also a test
  that `--check` actually notices a tampered page, so the freshness test cannot
  pass vacuously.
- **Every API path mentioned exists** in the OpenAPI schema, after normalising
  the example ids the pages use.
- **Every CLI flag and subcommand mentioned exists** in the argparse parsers of
  `LogView`, `DemoClient`, `Main` and `BuildDocs`. This is why those three grew
  a `build_parser()`.
- **Every config key mentioned exists** — both `section.key` references in
  prose and *every key of every ```yaml block in the docs*, walked recursively
  and checked against `config_key_names()`.

Plus: every internal link and anchor resolves, and there are no external URLs
anywhere, so the site works from a `file://` path.

---

## 4. Design notes

Written for someone who wants to redesign this without reading any code.

### The rules

**Everything is a token.** 41 custom properties in a single `:root` block at
the top of `web/orderecho.css`. Nothing below that block names a colour — a
test greps for `#hex`, `rgb(` and `hsl(` outside `:root` and fails the suite if
it finds one. A redesign is a new `:root` block.

**Fixed light theme.** No theme switching, by instruction. Anything that would
need a dark variant is a token, so adding one later is a media query that
redefines the block rather than a sweep.

**Nothing off this machine.** No web fonts, no CDN, no `@import`, no `url()`
anywhere in the stylesheet. The fonts are the system stacks. The site opens
from a file path with no network.

**WCAG AA throughout.** Every text/background pair in use was computed; the
worst is 5.2:1 against a 4.5:1 requirement, and body text is 15.8:1.

### The tokens

```css
/* surfaces and ink */
--bg: #f5f6f8;          /* the page behind everything */
--surface: #ffffff;     /* cards, panels, table bodies */
--surface-2: #eef1f5;   /* zebra stripes, code blocks, inset areas */
--surface-3: #e4e8ee;   /* hovered rows, pressed buttons, modal scrim */
--text: #16181d;        /* 15.8:1 on --surface */
--muted: #5a6371;       /* labels and captions, 5.7:1 on --surface */
--border: #dde1e7;

/* one accent */
--accent: #1a4fc4;      /* links, focus, the primary button, 7.1:1 */
--accent-ink: #ffffff;  /* text on the accent */
--accent-soft: #e9eefb; /* accent at rest: selected nav, note callout */

/* verdicts */
--pass: #0f6a37;   --pass-soft: #e6f2ea;
--warn: #7c5300;   --warn-soft: #faf0dc;
--fail: #b02218;   --fail-soft: #fbeae8;

/* FIX directions and flags */
--in:   #0a5f8f;   /* inbound: client -> us */
--out:  #454f5c;   /* outbound: us -> client */
--disc: #7a3d8f;   /* discarded */
--flag: #7c5300;   /* injected, possdup, bad framing */
--fill: #0f6a37;   /* a message that moved quantity */

/* type */
--font-sans: system-ui, -apple-system, "Segoe UI", Roboto, …
--font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, …
--size-base: 15px;  --size-small: 13px;  --size-tiny: 12px;  --size-code: 13px;
--line: 1.6;        --line-tight: 1.45;

/* space and shape */
--s1: 4px;  --s2: 8px;  --s3: 12px;  --s4: 16px;  --s5: 24px;  --s6: 40px;
--radius: 7px;  --radius-sm: 4px;

/* measure */
--content: 860px;   /* prose column */
--sidebar: 240px;
--shadow: 0 1px 2px rgba(16,20,30,.06);
--shadow-lifted: 0 8px 28px rgba(16,20,30,.16);
```

Three decisions worth arguing with:

1. **Six spacing steps on a 4px base**, not a strict geometric scale. `--s6` is
   40px rather than 48 because the prose column wanted a slightly tighter
   section break than a doubling gave.
2. **Two soft variants per verdict colour** (`--pass` / `--pass-soft`). Badges
   need a tinted background with an accessible foreground, and computing one
   from the other at runtime would mean `color-mix`, which is a colour outside
   `:root`.
3. **Direction colours are semantic, not decorative.** `--in` and `--out` carry
   meaning in the viewer — which way a message went — so they are their own
   tokens rather than shades of the accent.

### Component inventory

Every component in the file, what uses it, and the tokens it leans on.

| Component | Class | Used by | Leans on |
|---|---|---|---|
| Page shell | `.shell`, `.sidebar`, `.content` | guide | `--sidebar`, `--content`, `--surface`, `--border` |
| Sidebar nav | `.sidebar nav a`, `[aria-current]` | guide | `--accent-soft`, `--accent`, `--radius-sm` |
| Headings | `h1`–`h4` | both | `--text`, `--border`, `--line-tight` |
| Prose | `p`, `ul`, `ol`, `blockquote`, `.lede` | guide | `--s3`, `--muted` |
| Tables | `table`, `.scroll-x` | both | `--surface`, `--surface-2` (zebra), sticky `thead` |
| Inline code | `code` | both | `--surface-2`, `--font-mono`, `--size-code` |
| Code block | `pre`, `.codeblock`, `.copy` | guide | `--surface-2`, `--border`; copy button fades in on hover and on focus |
| Callouts | `.callout.note`, `.callout.warn` | guide | `--accent-soft` / `--warn-soft`, 3px left border |
| Badges | `.badge` + `.PASS/.WARN/.FAIL`, `.in/.out`, `.INJECTED/.POSSDUP/.BAD-CHECKSUM/.BAD-LENGTH` | both | the verdict, direction and flag tokens |
| Key-value list | `.kv` (a `dl` grid) | viewer stats | `--surface`, `--font-mono` |
| Controls | `button`, `button.primary`, `input`, `select`, `label.check` | both | `--accent`, `--border`, `--radius-sm` |
| Top bar | `.topbar`, `.bar` | viewer | `--surface`, `--border` |
| Message list | `.list`, `.msg`, `.msg .t/.sess/.seq/.name`, `.body-reject/-fill/-dim`, `.flag` | viewer | `--font-mono`, direction and flag tokens |
| Detail panel | `.detail`, `.detail .raw` | viewer | `--surface`, `--surface-2` |
| Modal | `.modal`, `.sheet`, `.check` | viewer | `--shadow-lifted`, `--surface-3` as the scrim |
| Card | `.card` | spare, for whatever comes next | `--shadow` |
| Utilities | `.empty`, `.visually-hidden`, `.footer` | both | `--muted` |

Responsive at two breakpoints: at 1000px the viewer's detail panel is hidden and
the list goes full width; at 900px the guide's sidebar stops being sticky and
sits above the content. Both were chosen from the content, not from device
sizes — 900px is where a 240px sidebar plus an 860px column stops fitting.

A row in the message list is a `<button>` with `role="option"`, not a `<div>`,
so the list is keyboard-reachable. Selection is `aria-selected`, not a class.

### What a designer would change first

The message list is the weakest part: it is a monospace grid with fixed column
widths inherited from the CLI, which is right for scanning and wrong for a long
ClOrdID. It clips rather than wraps. A design that gave ids their own treatment
— truncated middle, full value on hover — would be a real improvement, and it
is contained in `.msg`.

---

## 5. The real run

Started with the three-session config:

```
OrderEchoFixEmulator 0.8.0 (cook8) - 3 FIX sessions
  session          version  route                      port   rules  band
  ---------------- -------- -------------------------- ------ ------ ----
  agent42          FIX.4.2  ORDERECHO -> AGENT         9878   8      10%
  agent44          FIX.4.4  ORDERECHO -> AGENT         9878   8      10%
  strict-broker    FIX.4.2  STRICTBRK -> AGENT         9879   1      2%

  control api    : http://127.0.0.1:8090  (docs at /docs)
  guide          : http://127.0.0.1:8090/guide
  log viewer     : http://127.0.0.1:8090/viewer
  Ctrl+C to shut down.
```

The last three lines are §6's banner requirement.

Then two sessions were driven **concurrently**, alternating orders, so the two
log files would genuinely interleave: a resting order on agent42 that is then
replaced and cancelled, and three orders on agent44 (a 4.4 market fill, a limit
fill, and a rule reject).

### `/guide`

```
$ curl -s -o /tmp/g.html -w 'HTTP %{http_code}  %{size_download} bytes  %{content_type}\n' \
       http://127.0.0.1:8090/guide
HTTP 200  10691 bytes  text/html; charset=utf-8
$ grep -o '<title>[^<]*</title>' /tmp/g.html
<title>Overview - OrderEchoFixEmulator</title>

$ curl -s -o /dev/null -w 'HTTP %{http_code}  ->  %{redirect_url}\n' http://127.0.0.1:8090/
HTTP 307  ->  http://127.0.0.1:8090/guide

$ curl -s -o /dev/null -w 'HTTP %{http_code}  %{size_download} bytes  %{content_type}\n' \
       http://127.0.0.1:8090/assets/orderecho.css
HTTP 200  14608 bytes  text/css; charset=utf-8
```

### A generated page, as served

The reject-code table on Order behavior, fetched from the running engine and
flattened back out of its HTML:

```
$ curl -s http://127.0.0.1:8090/guide/order-behavior.html | ...

ExecutionReport rejects (tag 103)
reason                      | FIX 4.2                  | FIX 4.4
UNSUPPORTED_CHARACTERISTIC  | 0 Broker/Exchange Option | 11 Unsupported Order Characteristic
BAD_QUANTITY                | 0 Broker/Exchange Option | 13 Incorrect Quantity
BAD_PRICE                   | 0 Broker/Exchange Option | 0 Broker/Exchange Option
DUPLICATE_CL_ORD_ID         | 6 Duplicate Order        | 6 Duplicate Order
BAND_BREACH                 | 3 Order Exceeds Limit    | 3 Order Exceeds Limit
NO_REFERENCE                | 0 Broker/Exchange Option | 99 Other
RULE_REJECT                 | 0 Broker/Exchange Option | 0 Broker/Exchange Option
```

Every value there came out of `_FIX42_REJECT_REASONS` and
`_FIX44_REJECT_REASONS`, and every name out of the FIX dictionary.

### The viewer list: merged, named, with seq

`GET /viewer/messages`, rendered as a table. Two sessions, two files, one
conversation:

```
total 26, sessions ['ORDERECHO-AGENT', 'agent42', 'agent44']

stamp                 dir  session        seq  type                   summary
20260927-16:34:29.189 -->  agent42          1  Logon                  141=Y 108=30
20260927-16:34:29.191 <--  agent42          1  Logon                  141=Y 108=30
20260927-16:34:29.193 -->  agent44          1  Logon                  141=Y 108=30
20260927-16:34:29.195 <--  agent44          1  Logon                  141=Y 108=30
20260927-16:34:49.025 -->  agent42          2  New Order              Buy 500 ZWZZT Limit @10.00 11=DEMO-1790526889025-1
20260927-16:34:51.027 -->  agent44          2  New Order              Buy 400 CSCO Market 11=DEMO-1790526891027-1
20260927-16:34:51.260 <--  agent44          2  Execution Report       New/New cum=0 lv=400 avg=0.0000 11=DEMO-1790526891027-1
20260927-16:34:51.649 <--  agent42          2  Execution Report       New/New cum=0 lv=500 avg=0.0000 11=DEMO-1790526889025-1
20260927-16:34:52.638 <--  agent44          3  Execution Report       Trade/Filled 400@106.70 cum=400 lv=0 avg=106.7000 11=DEMO-…-1
20260927-16:34:53.028 -->  agent42          3  Order Cancel/Replace Request  Replace DEMO-1790526889025-1->DEMO-1790526893027-2-R qty 500->800 px 10.00->10.50
20260927-16:34:53.030 <--  agent42          3  Execution Report       Replace/New cum=0 lv=800 avg=0.0000 11=DEMO-1790526893027-2-R
20260927-16:34:55.029 -->  agent44          3  New Order              Buy 1000 AAPL Limit @227.50 11=DEMO-1790526895029-2
20260927-16:34:55.033 <--  agent44          4  Execution Report       New/New cum=0 lv=1000 avg=0.0000 11=DEMO-1790526895029-2
20260927-16:34:55.597 <--  agent44          5  Execution Report       Trade/Filled 1000@227.50 cum=1000 lv=0 avg=227.5000 11=DEMO-…-2
20260927-16:34:57.030 -->  agent42          4  Order Cancel Request   Cancel DEMO-1790526893027-2-R->DEMO-1790526897030-3-C
20260927-16:34:57.060 <--  agent42          4  Execution Report       Canceled/Canceled cum=0 lv=0 avg=0.0000 11=DEMO-…-3-C
20260927-16:34:59.031 -->  agent44          4  New Order              Buy 300 KO Limit @60.00 11=DEMO-1790526899031-3
20260927-16:35:00.058 <--  agent44          6  Execution Report       Rejected/Rejected cum=0 lv=0 avg=0.0000 11=DEMO-…-3 103=Broker/Exchange Option "Rejected by OrderEcho rule"
20260927-16:35:01.033 -->  agent44          5  Logout                 58=Demo client done
20260927-16:35:01.034 <--  agent44          7  Logout                 58=Logout acknowledged
20260927-16:35:01.035 -->  agent42          5  Logout                 58=Demo client done
20260927-16:35:01.037 <--  agent42          5  Logout                 58=Logout acknowledged
```

Four things in that output are Cook 8:

- **§4.1** the two sessions alternate by timestamp. Before this, agent42's
  whole conversation printed, then agent44's.
- **§4.2** `msg_type_name` and `summary` are the strings the CLI prints, from
  the same function — and there is a **seq** column.
- **§4.3** the replace row says `Replace …-1->…-2-R qty 500->800 px
  10.00->10.50`, with the old values recovered from the earlier messages; the
  cancel row names both ids.
- Session names, message-type names and reject reasons are all decoded, not
  raw.

### The tamper scenario

A careless `sed` on a real log — one digit of a CumQty — which also breaks the
CheckSum, because it must:

```
$ sed -i "" "s/|14=1000|/|14=1001|/" agent44_20260927.log
$ .venv/bin/python orderecho_LogView.py timeline agent44_20260927.log \
      --no-color --clordid DEMO-1790526895029-2

Order chain for DEMO-1790526895029-2
  ClOrdIDs: DEMO-1790526895029-2
  OrderID : O-20260927-163410-3

  time                  dir  type               exec/status              qty    last     cum leaves      avg
  2026-09-27T16:34:55.029 -->  New Order        - / -                   1000       -       -      -        -
  2026-09-27T16:34:55.033 <--  Execution Report 0 (New) / 0 (New)       1000       -       0   1000   0.0000
  2026-09-27T16:34:55.597 <--  Execution Report F (Trade) / 2 (Filled)  1000 1000@227.50 1001      0 227.5000 [BAD-CHECKSUM]

Checks
  [PASS] cum_qty_monotonic: CumQty rose to 1001 without ever falling
  [PASS] working_quantities: 1 working report(s) balanced
  [FAIL] terminal_quantities: 39=2 (Filled) with CumQty 1001 but OrderQty 1000, at seq=5 17=E-20260927-163410-6 agent44_20260927.log:8
  [FAIL] fill_quantities_sum: 1 fill(s) totalling 1000 but the last CumQty is 1001
  [PASS] avg_px: AvgPx 227.5000 matches the fills to within 0.0001
  [PASS] exec_ids_unique: 2 ExecID(s), all distinct
  [PASS] order_id_constant: OrderID O-20260927-163410-3 throughout
  [PASS] nothing_after_terminal: terminal 39=2 was the last word
  [PASS] version_rules: every report matches its version's conventions
  [PASS] requests_answered: all 1 request(s) answered
  [WARN] framing_intact: 1 message(s) with broken framing, so what they say cannot be trusted: bad CheckSum at seq=5 17=E-20260927-163410-6 agent44_20260927.log:8

  verdict: FAIL
$ echo $?
2
```

The `[BAD-CHECKSUM]` badge on the row is §4.4's first half; `framing_intact`
with the seq and `file:line` is the second. The two FAILs are the arithmetic
the tampered digit broke, which is the honest answer — and they are why the
verdict is FAIL rather than WARN.

### Everything stopped

```
$ lsof -nP -iTCP:9878 -iTCP:9879 -iTCP:8090 -iTCP:8091 -sTCP:LISTEN
(nothing)
$ pgrep -fl orderecho_Main
(nothing)
```

Shutdown was clean: `Acceptor stopped` / `Shutdown complete`.

---

## 6. Decisions I made

1. **`CONFIG_SCHEMA` is a new thing in `orderecho_Config.py`, not a table in
   the build script.** §7 wants a test that every config key in the docs
   exists, and §5.3 wants a generated key reference; both need to ask the code
   what keys there are. Key names, types and defaults come from
   `dataclasses.fields()`, so they cannot drift; only the YAML *nesting* is
   written out, because `defaults:` feeds three different dataclasses and no
   amount of introspection would work that out.
2. **A docstring on every control-API route** (38 written, 1 already there).
   The generated endpoint table was FastAPI's auto-titles, which say nothing. Docstrings are the idiomatic fix and they
   improve `/docs` too. This is the largest diff in the cook that the spec did
   not literally ask for; I judged a generated reference that reads like a
   function-name dump to be a failed requirement rather than a met one.
3. **The merge rule for unstamped lines.** A line with no timestamp inherits
   the last stamped line *in its own file*, so a continuation stays put. An
   unstamped run at the head of a file inherits that file's **first** stamp, so
   it lands just before the line it belongs to. A file with no timestamps
   anywhere goes **last** — it cannot be placed, and putting it first would be
   claiming it is the oldest.
4. **`stats` still streams.** §4.1 lists "stats first/last" among the places
   the merge matters, but first/last are `min()` and `max()`, which are already
   order-independent. Materialising the whole file to merge it would have blown
   the Cook 7 memory budget (100k messages, peak under 20 MB). The test asserts
   the span is right across interleaved files; the implementation stays
   streaming.
5. **`->` rather than `→` in replace and cancel rows.** §4.3 writes the shape
   with an arrow glyph. The CLI already uses `-->` for direction, and a
   non-ASCII character in a line that may be pasted into a ticket or a Windows
   terminal is a liability. ASCII everywhere.
6. **One flag vocabulary.** The CLI said `BAD-CHECKSUM` and `PossDup`; §3's
   badge list says `BAD CHECKSUM` and `POSSDUP`. Two spellings of one idea is
   exactly the drift this cook is about, so there is now one list in
   `orderecho_Summary.py`: `INJECTED`, `POSSDUP`, `BAD-CHECKSUM`,
   `BAD-LENGTH`. Hyphenated, because `[BAD CHECKSUM BAD LENGTH]` is ambiguous.
7. **`framing_intact` is the eleventh check, appended last.** §4.4 calls it an
   11th check, so the existing ten keep their numbers. It WARNs rather than
   FAILs: a broken message means its contents cannot be trusted, which is not
   the same as knowing they are wrong.
8. **The rejected-request rule in `SummaryContext`.** A replace that was
   rejected never took effect, so it is not what the order looks like. Without
   this, a second replace would measure itself against a request that was
   refused. There is a test for it.
9. **The viewer cursor now takes the highest index, not the last row.** Once
   the list is in time order, the last row is not necessarily the newest thing
   read, and the old code would have handed back a cursor that went backwards
   and replayed messages. Found by reasoning about the change rather than by a
   failure; there is now a test that pins it.
10. **`build_parser()` split out of `parse_args()`** in `Main`, `DemoClient`
    and `BuildDocs`, to match `LogView`. The drift test needs to ask a parser
    what flags it accepts without running it.
11. **The guide is served from the committed `docs/site`, never rendered on
    demand.** If the site is stale the fix is to run the build, and a test says
    so. Rendering at request time would have made the engine depend on the
    `markdown` package at run time and made "what is served" differ from "what
    is committed".
12. **Path traversal is refused in the guide router.** It serves files by name
    from one directory, so it checks that the resolved path is still inside it.
    Tested with encoded forms that a client does not normalise away.
13. **`/` redirects with 307, not 302.** It preserves the method, and nothing
    about this redirect is permanent enough for 301.
14. **The README is now 60 lines.** §2 asks for a slim README: a paragraph, a
    quick start, and a link. Everything else moved into the guide, where it can
    be generated and tested. Nothing was deleted without a home.
15. **Today's logs were moved aside again**, to `logs/fix/pre-cook8/`, so the
    pasted run is one run. Nothing deleted; the viewer's glob does not recurse.

---

## 7. Questions for you

1. **Is the guide's audience right?** I wrote it for someone who has to *use*
   the emulator — a person wiring a client up, or an agent being told what the
   target does. It is not a design document and it does not explain the
   internals beyond the architecture section. If the LLM operator is meant to
   read this as its manual, some pages (Session protocol, Order behavior) could
   go further into "here is what you should assert".
2. **Should the changelog page be generated too?** It is the one page that will
   certainly go stale, and nothing checks it. It could be built from the
   `REPORT_Cook*.md` headers, at the cost of tying the guide to files that are
   really build logs.
3. **How much should the docs promise about `/docs` versus `/guide`?** Both are
   served, and the generated endpoint table now overlaps FastAPI's own page. I
   kept both and cross-linked. If one should win, it is an easy change.
4. **The message list still clips long ClOrdIDs** (see the design notes). Worth
   a fix now, or left for whoever redesigns the tokens?
5. **`markdown` is a new runtime dependency**, but only for building the docs —
   the engine serves pre-built HTML and never imports it. Should
   `requirements.txt` split into runtime and build, or is one file still the
   right trade for a local tool?
