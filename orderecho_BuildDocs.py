"""Build the documentation site: docs/src/*.md -> docs/site/*.html.

    .venv/bin/python orderecho_BuildDocs.py            # write docs/site
    .venv/bin/python orderecho_BuildDocs.py --check     # is it up to date?
    .venv/bin/python orderecho_BuildDocs.py --out DIR   # build somewhere else

Two things matter here.

*It is deterministic.*  The same sources and the same code produce the same
bytes, every time: no timestamps, no build ids, no dictionary iteration order
leaking out.  A test builds into a temporary directory and compares the result
with the committed `docs/site`, so documentation that has gone stale fails the
suite instead of quietly misleading somebody.

*Reference sections are generated from the code.*  A page says
`<!-- generate:config-reference -->` and this fills it in by reading the
config schema, the FIX profiles, the check functions and the API's own OpenAPI
document.  Prose is written by hand; tables of keys, codes and endpoints are
not, because those are exactly the things that drift.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import sys

import markdown

from orderecho_Config import CONFIG_SCHEMA
from orderecho_FixDict import default_dictionary
from orderecho_FixVersion import (
    PROFILES,
    ExecReport,
    Reason,
    ReportKind,
)
from orderecho_Timeline import CHECKS, Chain
from orderecho_Version import ORDERECHO_BUILD, ORDERECHO_VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "docs", "src")
SITE_DIR = os.path.join(HERE, "docs", "site")
CSS_SRC = os.path.join(HERE, "web", "orderecho.css")

#: The sidebar, in order.  (source slug, output file)
PAGES = (
    ("overview", "index.html"),
    ("quick-start", "quick-start.html"),
    ("configuration", "configuration.html"),
    ("sessions", "sessions.html"),
    ("order-behavior", "order-behavior.html"),
    ("session-protocol", "session-protocol.html"),
    ("control-api", "control-api.html"),
    ("evidence-logs", "evidence-logs.html"),
    ("log-viewer", "log-viewer.html"),
    ("demo-client", "demo-client.html"),
    ("troubleshooting", "troubleshooting.html"),
    ("changelog", "changelog.html"),
)

PLACEHOLDER = re.compile(r"^<!--\s*generate:([a-z0-9-]+)\s*-->\s*$",
                         re.MULTILINE)


# ---------------------------------------------------------------- generators


def _table(headers, rows) -> str:
    """A GitHub-flavoured Markdown table."""
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        cells = [str(cell).replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _code(value) -> str:
    return f"`{value}`" if value not in (None, "") else ""


def generate_config_reference() -> str:
    """Every key the loader reads, from `orderecho_Config.CONFIG_SCHEMA`."""
    blocks = []
    for section in CONFIG_SCHEMA:
        rows = []
        for key in section.keys:
            notes = []
            if key.choices:
                notes.append("one of "
                             + ", ".join(f"`{c}`" for c in key.choices))
            if key.note:
                notes.append(key.note)
            rows.append([_code(key.name), _code(key.type),
                         _code(key.default_text), "; ".join(notes)])
        blocks.append(f"### `{section.path}`\n\n{section.summary}\n")
        blocks.append(_table(["key", "type", "default", "notes"], rows))
        if section.nests:
            nested = ", ".join(f"`{name}`" for name in section.nests)
            blocks.append(f"\nNested sections: {nested}.")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def _sample_report(full: bool) -> ExecReport:
    """One report with every optional field, or one with none of them."""
    common = dict(
        kind=ReportKind.FILL, order_id="O-1", cl_ord_id="C-1", exec_id="E-1",
        symbol="AAPL", side="1", order_qty="100", ord_type="2",
        ord_status="2", leaves_qty="0", cum_qty="100", avg_px="10.0000",
        transact_time="20260927-09:00:00.000", last_qty="100",
        last_px="10.00",
    )
    if not full:
        return ExecReport(**common)
    return ExecReport(price="10.00", orig_cl_ord_id="C-0", account="ACCT",
                      reason=Reason.RULE_REJECT, text="because", **common)


def generate_er_fields() -> str:
    """The ExecutionReport body each profile renders, in tag order."""
    dictionary = default_dictionary()
    blocks = []
    for name in sorted(PROFILES):
        profile = PROFILES[name]
        full = [tag for tag, _ in profile.render_exec_report(_sample_report(True))]
        always = {tag for tag, _ in
                  profile.render_exec_report(_sample_report(False))}
        rows = []
        for tag in full:
            rows.append([
                f"`{tag}`",
                dictionary.tag_name(tag, name),
                "always" if tag in always else "when it applies",
            ])
        blocks.append(f"### {profile.label}\n")
        blocks.append(f"BeginString `{profile.begin_string}`, "
                      f"{len(full)} body fields at most.\n")
        blocks.append(_table(["tag", "name", "sent"], rows))
        blocks.append("")

    # What actually differs, rather than leaving the reader to diff two tables.
    left, right = PROFILES["FIX.4.2"], PROFILES["FIX.4.4"]
    diff_rows = []
    for kind in ReportKind:
        a, b = left.exec_type_for(kind), right.exec_type_for(kind)
        if a != b:
            diff_rows.append([kind.value, f"`150={a}`", f"`150={b}`"])
    blocks.append("### Where they differ\n")
    blocks.append(_table([ "report", left.label, right.label], diff_rows))
    blocks.append("")
    rows = [["ExecTransType (20)",
             "sent" if left.include_exec_trans_type else "not sent",
             "sent" if right.include_exec_trans_type else "not sent"]]
    for msg_type in sorted(set(left.required_tags) | set(right.required_tags)):
        rows.append([
            f"required tags on `35={msg_type}`",
            ", ".join(str(tag) for tag in left.required_for(msg_type)),
            ", ".join(str(tag) for tag in right.required_for(msg_type)),
        ])
    blocks.append(_table(["", left.label, right.label], rows))
    return "\n".join(blocks).rstrip() + "\n"


def generate_reject_reasons() -> str:
    """Which code each profile sends for each reason we can reject for."""
    dictionary = default_dictionary()
    left, right = PROFILES["FIX.4.2"], PROFILES["FIX.4.4"]

    def named(tag, code, version):
        name = dictionary.enum_name(tag, code, version)
        return f"`{code}`" + (f" {name}" if name else "")

    rows = []
    for reason in Reason:
        a = left.reject_reasons.get(reason)
        b = right.reject_reasons.get(reason)
        if a is None and b is None:
            continue
        rows.append([reason.value,
                     named(103, a, left.name) if a else "-",
                     named(103, b, right.name) if b else "-"])
    blocks = ["### ExecutionReport rejects (tag 103)\n",
              _table(["reason", left.label, right.label], rows), ""]

    rows = []
    for reason in Reason:
        a = left.cancel_reject_reasons.get(reason)
        b = right.cancel_reject_reasons.get(reason)
        if a is None and b is None:
            continue
        rows.append([reason.value,
                     named(102, a, left.name) if a else "-",
                     named(102, b, right.name) if b else "-"])
    blocks += ["### OrderCancelReject rejects (tag 102)\n",
               _table(["reason", left.label, right.label], rows), ""]

    responses = _table(
        ["responding to", "CxlRejResponseTo"],
        [[response.value, f"`434={code}`"]
         for response, code in sorted(_response_to_values().items(),
                                      key=lambda pair: pair[1])])
    blocks += ["### What the cancel reject is answering\n", responses, ""]
    return "\n".join(blocks).rstrip() + "\n"


def _response_to_values() -> dict:
    from orderecho_FixVersion import RESPONSE_TO_VALUES
    return RESPONSE_TO_VALUES


def generate_timeline_checks() -> str:
    """The timeline checks, from the check functions themselves."""
    rows = []
    for check in CHECKS:
        result = check(Chain(seed="-"))
        doc = (check.__doc__ or "").strip().splitlines()[0].strip()
        rows.append([f"`{result.name}`", doc, result.rule])
    return (f"There are {len(CHECKS)} checks.\n\n"
            + _table(["check", "what it means", "the rule it enforces"], rows)
            + "\n")


def generate_control_api() -> str:
    """Every route, from the API's own OpenAPI document."""
    schema = _openapi()
    definitions = schema.get("components", {}).get("schemas", {})

    def body_of(operation) -> str:
        body = operation.get("requestBody")
        if not body:
            return ""
        content = body.get("content", {}).get("application/json", {})
        ref = content.get("schema", {}).get("$ref", "")
        model = definitions.get(ref.rsplit("/", 1)[-1], {})
        required = set(model.get("required", ()))
        names = []
        for name in model.get("properties", {}):
            names.append(f"`{name}`" + ("" if name in required else "?"))
        return ", ".join(names) or "(empty)"

    rows = []
    for path in sorted(schema.get("paths", {})):
        for method, operation in sorted(schema["paths"][path].items()):
            if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue
            # The docstring says something; FastAPI's auto summary only
            # repeats the function name.
            summary = ((operation.get("description") or "").strip()
                       .split("\n")[0]
                       or operation.get("summary") or "")
            codes = ", ".join(sorted(operation.get("responses", {})))
            rows.append([f"`{method.upper()}`", f"`{path}`", summary.strip(),
                         body_of(operation), codes])
    return (_table(["method", "path", "what it does", "body", "responses"],
                   rows) + "\n")


def _openapi() -> dict:
    """The OpenAPI document, built without starting an engine.

    `build_app` only needs the transport to close over, so a stand-in with a
    config is enough to describe the routes.  Nothing is started and no
    socket is opened.
    """
    from orderecho_Config import load_config
    from orderecho_ControlApi import build_app

    class _Stub:
        def __init__(self, config):
            self.config = config
            self.engine_log = None
            self.runtimes = {}

        def runtime_for_order(self, order_id):
            return None

    config = load_config(os.path.join(HERE, "config", "orderecho.yaml"))
    return build_app(_Stub(config)).openapi()


GENERATORS = {
    "config-reference": generate_config_reference,
    "er-fields": generate_er_fields,
    "reject-reasons": generate_reject_reasons,
    "timeline-checks": generate_timeline_checks,
    "control-api": generate_control_api,
}


# -------------------------------------------------------------------- render


COPY_SCRIPT = """
document.querySelectorAll("pre").forEach(function (pre) {
  var wrap = document.createElement("div");
  wrap.className = "codeblock";
  pre.parentNode.insertBefore(wrap, pre);
  wrap.appendChild(pre);
  var button = document.createElement("button");
  button.className = "copy";
  button.type = "button";
  button.textContent = "Copy";
  button.addEventListener("click", function () {
    var text = pre.innerText;
    var done = function () {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy"; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
      return;
    }
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); done(); } catch (e) {}
    document.body.removeChild(area);
  });
  wrap.appendChild(button);
});
""".strip()


def expand(text: str) -> str:
    """Fill every `<!-- generate:NAME -->` placeholder."""
    def replace(match):
        name = match.group(1)
        if name not in GENERATORS:
            raise KeyError(f"no generator called {name!r}")
        return GENERATORS[name]()
    return PLACEHOLDER.sub(replace, text)


def title_of(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "OrderEchoFixEmulator"


def render_page(slug: str, source: str, titles: dict, current: str) -> str:
    body = markdown.markdown(
        expand(source),
        extensions=["tables", "fenced_code", "sane_lists", "toc",
                    "attr_list", "md_in_html"],
        output_format="html",
    )
    nav = []
    for other_slug, other_file in PAGES:
        current_attr = ' aria-current="page"' if other_slug == slug else ""
        nav.append(f'<li><a href="{other_file}"{current_attr}>'
                   f'{html.escape(titles[other_slug])}</a></li>')
    return TEMPLATE.format(
        title=html.escape(titles[slug]),
        version=f"{ORDERECHO_VERSION} ({ORDERECHO_BUILD})",
        nav="\n".join(nav),
        body=body,
        script=COPY_SCRIPT,
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - OrderEchoFixEmulator</title>
<link rel="stylesheet" href="assets/orderecho.css">
</head>
<body>
<div class="shell">
<aside class="sidebar">
<a class="brand" href="index.html">OrderEchoFixEmulator</a>
<span class="version">{version}</span>
<nav aria-label="Guide">
<ol>
{nav}
</ol>
</nav>
</aside>
<main class="content">
{body}
<div class="footer">
OrderEchoFixEmulator {version}. Reference tables on this page are generated
from the code by <code>orderecho_BuildDocs.py</code>.
</div>
</main>
</div>
<script>
{script}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- build


def build(out_dir: str = SITE_DIR) -> list:
    """Render every page into *out_dir*.  Returns the files written."""
    sources = {}
    for slug, _ in PAGES:
        path = os.path.join(SRC_DIR, f"{slug}.md")
        with open(path, "r", encoding="utf-8") as handle:
            sources[slug] = handle.read()
    titles = {slug: title_of(text) for slug, text in sources.items()}

    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    written = []
    for slug, filename in PAGES:
        target = os.path.join(out_dir, filename)
        rendered = render_page(slug, sources[slug], titles, slug)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        written.append(target)

    css_target = os.path.join(out_dir, "assets", "orderecho.css")
    shutil.copyfile(CSS_SRC, css_target)
    written.append(css_target)
    return written


def check(out_dir: str = SITE_DIR) -> int:
    """Is the committed site what the sources and the code produce now?"""
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        build(temporary)
        stale = []
        for _, filename in PAGES + (("assets", "assets/orderecho.css"),):
            fresh = os.path.join(temporary, filename)
            committed = os.path.join(out_dir, filename)
            if not os.path.exists(committed):
                stale.append(f"{filename}: missing")
                continue
            with open(fresh, "rb") as a, open(committed, "rb") as b:
                if a.read() != b.read():
                    stale.append(f"{filename}: out of date")
    if stale:
        print("docs/site is stale:", file=sys.stderr)
        for line in stale:
            print(f"  {line}", file=sys.stderr)
        print("run: .venv/bin/python orderecho_BuildDocs.py", file=sys.stderr)
        return 1
    print(f"docs/site is up to date ({len(PAGES)} pages)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line, on its own, so the docs can be checked against it."""
    parser = argparse.ArgumentParser(
        prog="orderecho_BuildDocs.py",
        description="Render docs/src/*.md into docs/site/*.html")
    parser.add_argument("--out", default=SITE_DIR,
                        help="where to write (default: docs/site)")
    parser.add_argument("--check", action="store_true",
                        help="report whether the committed site is up to date")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return check(args.out)
    written = build(args.out)
    print(f"wrote {len(written)} file(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
