"""The documentation site, and the tests that stop it drifting (spec 7).

Documentation that is wrong is worse than none, so these are deliberately
blunt: if the committed site is not what the sources and the code produce, or
if a page names a flag, a route or a config key that does not exist, the suite
fails.
"""

import os
import re
import tempfile

import pytest

import orderecho_BuildDocs as docs
from orderecho_Config import config_key_names

SITE = docs.SITE_DIR
SRC = docs.SRC_DIR

PAGE_FILES = [filename for _, filename in docs.PAGES]


def site_text(filename: str) -> str:
    with open(os.path.join(SITE, filename), "r", encoding="utf-8") as handle:
        return handle.read()


def all_site_html() -> dict:
    return {name: site_text(name) for name in PAGE_FILES}


def all_source_text() -> str:
    parts = []
    for slug, _ in docs.PAGES:
        with open(os.path.join(SRC, f"{slug}.md"), "r",
                  encoding="utf-8") as handle:
            parts.append(handle.read())
    return "\n".join(parts)


# --- the build -------------------------------------------------------------

def test_every_page_has_a_source_and_a_built_file():
    for slug, filename in docs.PAGES:
        assert os.path.exists(os.path.join(SRC, f"{slug}.md")), slug
        assert os.path.exists(os.path.join(SITE, filename)), filename


def test_the_build_is_deterministic():
    """Same sources, same code, same bytes -- no timestamps, no build ids."""
    with tempfile.TemporaryDirectory() as first, \
            tempfile.TemporaryDirectory() as second:
        docs.build(first)
        docs.build(second)
        for filename in PAGE_FILES + ["assets/orderecho.css"]:
            with open(os.path.join(first, filename), "rb") as a, \
                    open(os.path.join(second, filename), "rb") as b:
                assert a.read() == b.read(), filename


def test_the_committed_site_is_fresh():
    """If this fails: .venv/bin/python orderecho_BuildDocs.py"""
    assert docs.check(SITE) == 0, (
        "docs/site is stale -- run: .venv/bin/python orderecho_BuildDocs.py")


def test_check_notices_a_stale_page(tmp_path):
    out = str(tmp_path / "site")
    docs.build(out)
    assert docs.check(out) == 0
    with open(os.path.join(out, "index.html"), "a", encoding="utf-8") as handle:
        handle.write("<!-- meddling -->")
    assert docs.check(out) == 1


def test_no_placeholder_survives_the_build():
    for name, text in all_site_html().items():
        assert "<!-- generate:" not in text, name
        assert 'markdown="1"' not in text, name


def test_every_generator_is_used_and_every_placeholder_has_one():
    source = all_source_text()
    wanted = set(docs.PLACEHOLDER.findall(source))
    assert wanted <= set(docs.GENERATORS), wanted - set(docs.GENERATORS)
    assert set(docs.GENERATORS) == wanted, set(docs.GENERATORS) - wanted


def test_the_generated_sections_are_not_empty():
    for name, generate in docs.GENERATORS.items():
        text = generate()
        assert text.count("\n") > 4, f"{name} produced almost nothing"
        assert "|" in text, f"{name} produced no table"


# --- structure -------------------------------------------------------------

def test_every_page_carries_the_sidebar_and_the_stylesheet():
    for name, text in all_site_html().items():
        assert '<link rel="stylesheet" href="assets/orderecho.css">' in text, name
        assert '<aside class="sidebar">' in text, name
        for _, other in docs.PAGES:
            assert f'href="{other}"' in text, f"{name} does not link {other}"


def test_each_page_marks_itself_as_current():
    for slug, filename in docs.PAGES:
        text = site_text(filename)
        assert text.count('aria-current="page"') == 1, filename
        current = re.search(r'<a href="([^"]+)" aria-current="page">', text)
        assert current and current.group(1) == filename, filename


def test_the_pages_are_titled_from_their_own_headings():
    for slug, filename in docs.PAGES:
        with open(os.path.join(SRC, f"{slug}.md"), encoding="utf-8") as handle:
            heading = docs.title_of(handle.read())
        import html as html_module
        expected = html_module.escape(heading)
        assert f"<title>{expected} - OrderEchoFixEmulator</title>" in \
            site_text(filename)


# --- links and anchors (spec 7) -------------------------------------------

LINK = re.compile(r'href="([^"]+)"')


def test_every_internal_link_and_anchor_resolves():
    ids = {}
    for name, text in all_site_html().items():
        ids[name] = set(re.findall(r'id="([^"]+)"', text))

    broken = []
    for name, text in all_site_html().items():
        for href in LINK.findall(text):
            if href.startswith("#"):
                target, anchor = name, href[1:]
            elif "#" in href:
                target, anchor = href.split("#", 1)
            else:
                target, anchor = href, None
            if target and not target.endswith(".css"):
                if target not in ids:
                    broken.append(f"{name}: {href} (no such page)")
                    continue
            if anchor and anchor not in ids.get(target, set()):
                broken.append(f"{name}: {href} (no such anchor)")
    assert broken == [], broken


def test_the_site_is_offline_safe():
    """No external URLs anywhere: the whole thing opens from a file:// path."""
    offenders = []
    for name, text in all_site_html().items():
        for href in LINK.findall(text) + re.findall(r'src="([^"]+)"', text):
            if href.startswith(("http://", "https://", "//")):
                offenders.append(f"{name}: {href}")
    assert offenders == [], offenders


def test_the_css_travels_with_the_site():
    committed = os.path.join(SITE, "assets", "orderecho.css")
    assert os.path.exists(committed)
    with open(committed, encoding="utf-8") as a, \
            open(docs.CSS_SRC, encoding="utf-8") as b:
        assert a.read() == b.read()


# --- drift: what the docs claim must exist (spec 7) ------------------------

BACKTICKED = re.compile(r"`([^`]+)`")


def mentioned(pattern) -> set:
    found = set()
    for chunk in BACKTICKED.findall(all_source_text()):
        for match in re.finditer(pattern, chunk):
            found.add(match.group(0))
    return found


def test_every_api_path_mentioned_exists():
    schema = docs._openapi()
    known = set(schema["paths"])
    # The pages write real ids into example URLs; compare on the shape.
    def normalise(path):
        path = re.sub(r"O-\d{8}-\d{6}-\d+", "{order_id}", path)
        path = re.sub(r"/sessions/[A-Za-z0-9_-]+/", "/sessions/{session_id}/",
                      path)
        path = re.sub(r"/price/[A-Z.]+$", "/price/{symbol}", path)
        return path.rstrip("/")

    served = {normalise(path) for path in known}
    # Pages the engine serves that are deliberately not in the schema.
    served |= {"/guide", "/viewer", "/docs", "/assets/orderecho.css"}

    missing = []
    for path in mentioned(r"^/[a-zA-Z0-9_{}/.-]*$"):
        if path in ("/", "/guide", "/viewer", "/docs"):
            continue
        if normalise(path) not in served:
            missing.append(path)
    assert missing == [], missing


def every_parser():
    import orderecho_DemoClient
    import orderecho_LogView
    import orderecho_Main

    return (orderecho_LogView.build_parser(),
            orderecho_DemoClient.build_parser(),
            orderecho_Main.build_parser(),
            docs.build_parser())


def flags_of(parser) -> set:
    import argparse

    found = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found |= flags_of(sub)
    return found


def test_every_cli_flag_mentioned_exists():
    known = set()
    for parser in every_parser():
        known |= flags_of(parser)

    missing = [flag for flag in mentioned(r"--[a-z][a-z-]+")
               if flag not in known]
    assert missing == [], missing


def test_every_cli_subcommand_mentioned_exists():
    import orderecho_LogView

    subcommands = set()
    for action in orderecho_LogView.build_parser()._actions:
        if hasattr(action, "choices") and action.choices:
            subcommands |= {str(name) for name in action.choices}
    for name in ("view", "timeline", "stats", "serve"):
        assert name in subcommands
    source = all_source_text()
    for name in re.findall(r"orderecho_LogView\.py (\w+)", source):
        assert name in subcommands, name
    for name in re.findall(r"orderecho_DemoClient\.py (?:--\S+ \S+ )*([\w-]+)",
                           source):
        assert name in ("order", "cancel-demo", "session"), name


def test_every_config_key_mentioned_exists():
    known = config_key_names()
    # `section.key` and bare `key:` are both how the pages write them.
    mentions = set()
    for chunk in BACKTICKED.findall(all_source_text()):
        chunk = chunk.strip().rstrip(":")
        if re.fullmatch(r"[a-z_]+\.[a-z_]+", chunk):
            mentions.update(chunk.split("."))
    unknown = sorted(name for name in mentions if name not in known)
    assert unknown == [], unknown


def test_yaml_blocks_in_the_docs_only_use_real_keys():
    """Every key in every ```yaml block is one the loader reads."""
    import yaml

    known = config_key_names() | {"AAPL", "MSFT", "SPY", "default"}
    unknown = []
    for slug, _ in docs.PAGES:
        with open(os.path.join(SRC, f"{slug}.md"), encoding="utf-8") as handle:
            text = handle.read()
        for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
            if "..." in block:
                block = "\n".join(line for line in block.splitlines()
                                  if "..." not in line)
            try:
                loaded = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            for key in walk_keys(loaded):
                if key not in known:
                    unknown.append(f"{slug}: {key}")
    assert unknown == [], unknown


def walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_keys(item)


# --- the stylesheet (spec 8.2) --------------------------------------------

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def test_every_colour_in_the_stylesheet_is_a_token():
    with open(docs.CSS_SRC, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index(":root {")
    end = text.index("}", start)
    root = text[start:end]
    rest = text[:start] + text[end:]
    assert COLOUR.findall(rest) == [], (
        "colours outside :root -- a redesign must be one block, not a hunt")
    assert len(re.findall(r"--[\w-]+:", root)) > 25


def test_the_tokens_the_spec_asks_for_are_all_there():
    with open(docs.CSS_SRC, encoding="utf-8") as handle:
        root = handle.read().split(":root {", 1)[1].split("}", 1)[0]
    for token in ("--bg", "--surface", "--text", "--muted", "--border",
                  "--accent", "--pass", "--warn", "--fail", "--in", "--out",
                  "--font-sans", "--font-mono", "--size-base", "--s1",
                  "--radius", "--content"):
        assert f"{token}:" in root, token


def test_the_stylesheet_asks_for_nothing_off_this_machine():
    with open(docs.CSS_SRC, encoding="utf-8") as handle:
        text = handle.read()
    assert "@import" not in text
    assert "url(" not in text          # no web fonts, no background images
    assert "http" not in text.split("*/", 1)[1]
