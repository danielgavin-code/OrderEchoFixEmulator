"""The web viewer (spec 7) and its place in the engine (spec 8)."""

import io
import socket

import httpx
import pytest
from fastapi import FastAPI

from fix_TestClient import FixTestClient
from isolation import isolated_config
from orderecho_Clock import SystemClock
from orderecho_ControlApi import ControlApiServer
from orderecho_LogView import main as cli_main
from orderecho_LogViewer import MessageSource, build_router, serve_files
from orderecho_Transport import Transport
from test_Timeline import line, report

CHAIN = [
    line("D", 1, "IN", fields={11: "V1", 55: "AAPL", 54: "1", 38: "1000",
                               40: "2", 44: "10.00", 21: "1",
                               60: "20260927-09:00:00.000"}),
    report(2, "0", "0", cl_ord_id="V1"),
    report(3, "2", "2", cl_ord_id="V1", cum="1000", leaves="0",
           last_qty="1000", last_px="10.00", avg="10.0000"),
    line("D", 4, "IN", fields={11: "V2", 55: "MSFT", 54: "2", 38: "5",
                               40: "1", 21: "1",
                               60: "20260927-09:00:00.000"}),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_log(tmp_path, lines=None, name="agent42_20260927.log"):
    path = tmp_path / name
    path.write_text("\n".join(lines or CHAIN) + "\n", encoding="utf-8")
    return str(path)


def client_for(paths, prefix="/viewer"):
    source = MessageSource(paths)
    app = FastAPI()
    app.include_router(build_router(source, prefix=prefix))
    transport = httpx.ASGITransport(app=app)
    return source, httpx.AsyncClient(transport=transport,
                                     base_url="http://viewer")


# --- the page --------------------------------------------------------------

async def test_the_page_asks_only_this_server_for_anything(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        page = await api.get("/viewer/")
        assert page.status_code == 200
        body = page.text
        assert "<title>FIX log viewer</title>" in body
        # One stylesheet, served from here; the script is still inline.
        assert '<link rel="stylesheet" href="/assets/orderecho.css">' in body
        assert "<style>" not in body
        assert "style=" not in body
        assert "<script>" in body
        # Nothing off this origin: no build step, no CDN, no web fonts.
        assert "src=" not in body.replace('src=""', "")
        assert "http://" not in body and "https://" not in body
        assert 'href="//' not in body and 'src="//' not in body
        assert "cdn" not in body.lower()
        assert "font" not in body.lower()          # no web fonts either


# --- messages, cursoring, filters ------------------------------------------

async def test_messages_and_the_cursor(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        first = (await api.get("/viewer/messages")).json()
        assert first["total"] == 4
        assert len(first["messages"]) == 4
        assert first["sessions"] == ["agent42"]
        cursor = first["cursor"]

        # Nothing new yet.
        again = (await api.get(f"/viewer/messages?after={cursor}")).json()
        assert again["messages"] == []
        assert again["cursor"] == cursor


async def test_the_cursor_only_returns_what_is_new(tmp_path):
    path = write_log(tmp_path, CHAIN[:2])
    _source, api = client_for([path])
    async with api:
        first = (await api.get("/viewer/messages")).json()
        cursor = first["cursor"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(CHAIN[2] + "\n")
        fresh = (await api.get(f"/viewer/messages?after={cursor}")).json()
        assert len(fresh["messages"]) == 1
        assert fresh["messages"][0]["msg_type"] == "8"
        assert fresh["cursor"] > cursor


@pytest.mark.parametrize("query,expected", [
    ("msg_type=D", 2),
    ("msg_type=8", 2),
    ("direction=in", 2),
    ("symbol=MSFT", 1),
    ("clordid=V1", 3),
    ("grep=MSFT", 1),
    ("rejects=true", 0),
    ("injected=true", 0),
])
async def test_message_filters(tmp_path, query, expected):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        body = (await api.get(f"/viewer/messages?{query}")).json()
        assert len(body["messages"]) == expected


async def test_limit_keeps_the_newest(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        body = (await api.get("/viewer/messages?limit=2")).json()
        assert len(body["messages"]) == 2
        assert body["messages"][-1]["msg_type"] == "D"     # the MSFT one


# --- detail ----------------------------------------------------------------

async def test_message_detail_is_decoded(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        listed = (await api.get("/viewer/messages")).json()["messages"]
        index = listed[0]["index"]
        detail = (await api.get(f"/viewer/message/{index}")).json()
        assert detail["msg_type"] == "D"
        names = {row["name"] for row in detail["decoded"]}
        assert {"ClOrdID", "Symbol", "Side", "OrdType"} <= names
        side = next(row for row in detail["decoded"] if row["tag"] == 54)
        assert side["enum"] == "Buy"


async def test_an_unknown_message_is_404(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        response = await api.get("/viewer/message/9999")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"


# --- timeline --------------------------------------------------------------

async def test_timeline_endpoint(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        body = (await api.get("/viewer/timeline?clordid=V1")).json()
        assert body["verdict"] == "PASS"
        assert len(body["steps"]) == 3
        assert len(body["checks"]) == 11
        assert body["steps"][-1]["ord_status"].startswith("2 (")


async def test_timeline_needs_an_identifier(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        response = await api.get("/viewer/timeline")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"


async def test_timeline_for_an_unknown_order_is_404(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        response = await api.get("/viewer/timeline?clordid=GHOST")
        assert response.status_code == 404


# --- stats -----------------------------------------------------------------

async def test_stats_endpoint(tmp_path):
    _source, api = client_for([write_log(tmp_path)])
    async with api:
        body = (await api.get("/viewer/stats")).json()
        assert body["messages"] == 4
        assert body["by_session"] == {"agent42": 4}
        assert body["by_msg_type"]["8"] == 2
        assert body["by_direction"] == {"in": 2, "out": 2}
        assert body["files"] == ["agent42_20260927.log"]
        assert body["first"] and body["last"]


# --- the source itself -----------------------------------------------------

def test_the_source_notices_a_new_day_file(tmp_path):
    write_log(tmp_path, CHAIN[:2])
    source = MessageSource([str(tmp_path / "*.log")])
    assert len(source.messages) == 2

    (tmp_path / "agent42_20260928.log").write_text(
        line("D", 9, "IN", fields={11: "NEXTDAY", 55: "TSLA", 54: "1",
                                   38: "7", 40: "1", 21: "1",
                                   60: "20260928-09:00:00.000"}) + "\n",
        encoding="utf-8")
    assert source.refresh() == 1
    assert source.messages[-1].get(55) == "TSLA"
    assert source.messages[-1].session == "agent42"


def test_the_source_survives_truncation(tmp_path):
    path = write_log(tmp_path, CHAIN[:3])
    source = MessageSource([path])
    assert len(source.messages) == 3
    with open(path, "w", encoding="utf-8") as handle:     # truncate
        handle.write(CHAIN[3] + "\n")
    source.refresh()
    assert source.messages[-1].get(55) == "MSFT"


def test_the_source_keeps_a_bounded_window(tmp_path):
    lines = [report(seq, "0", "0", cl_ord_id=f"C{seq}")
             for seq in range(1, 60)]
    source = MessageSource([write_log(tmp_path, lines)], max_messages=25)
    assert len(source.messages) == 25              # the newest only


# --- 8: the engine mounts it ----------------------------------------------

@pytest.fixture
async def engine(tmp_path):
    config = isolated_config(tmp_path, fix_port=free_port(),
                             api_port=free_port(), control_api_enabled=True,
                             rules=[{"name": "full", "match": {"any": True},
                                     "behavior": "full_fill"}],
                             delay_ms=30)
    transport = Transport(config, clock=SystemClock())
    await transport.start()
    api = ControlApiServer(config, transport)
    await api.start()
    http = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api.bound_port}", timeout=10.0)
    try:
        yield transport, http
    finally:
        await http.aclose()
        await api.stop()
        await transport.stop()
        transport.close_logs()


async def test_the_engine_serves_the_viewer(engine):
    transport, http = engine
    fix = FixTestClient()
    await fix.connect("127.0.0.1", transport.port)
    await fix.logon(30)
    assert (await fix.recv()).msg_type == "A"
    await fix.send("D", [(11, "E1"), (21, "1"), (55, "AAPL"), (54, "1"),
                         (38, "100"), (40, "2"), (44, "10.00"),
                         (60, "20260927-09:00:00.000")])
    ack = await fix.recv_until("8")
    order_id = ack.get(37)
    await fix.recv_until("8")                     # the fill
    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    page = await http.get("/viewer/")
    assert page.status_code == 200
    assert "FIX log viewer" in page.text

    listed = (await http.get("/viewer/messages")).json()
    assert listed["total"] >= 4
    assert listed["sessions"] == ["ORDERECHO-AGENT"]

    timeline = (await http.get("/viewer/timeline?clordid=E1")).json()
    assert timeline["verdict"] == "PASS"

    health = (await http.get("/health")).json()
    assert health["build"] == "cook8"


async def test_the_engine_gives_a_timeline_for_one_of_its_orders(engine):
    transport, http = engine
    fix = FixTestClient()
    await fix.connect("127.0.0.1", transport.port)
    await fix.logon(30)
    assert (await fix.recv()).msg_type == "A"
    await fix.send("D", [(11, "E2"), (21, "1"), (55, "AAPL"), (54, "1"),
                         (38, "100"), (40, "2"), (44, "10.00"),
                         (60, "20260927-09:00:00.000")])
    ack = await fix.recv_until("8")
    order_id = ack.get(37)
    await fix.recv_until("8")
    await fix.close()
    await transport.wait_for_session_end(timeout=5.0)

    body = (await http.get(f"/orders/{order_id}/timeline")).json()
    assert body["session"] == "ORDERECHO-AGENT"
    assert body["verdict"] == "PASS"
    assert len(body["checks"]) == 11
    assert any(step["order_id"] == order_id for step in body["steps"])

    missing = await http.get("/orders/O-nope-1/timeline")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


async def test_a_timeline_for_an_order_from_an_earlier_run(engine, tmp_path):
    """The live book only holds this run; the log still knows the rest."""
    transport, http = engine
    old_log = (tmp_path / "logs" / "fix" / "OLDSESS_20260101.log")
    old_log.parent.mkdir(parents=True, exist_ok=True)
    old_log.write_text("\n".join([
        line("D", 1, "IN", fields={11: "OLD1", 55: "AAPL", 54: "1", 38: "50",
                                   40: "1", 21: "1",
                                   60: "20260101-09:00:00.000"}),
        report(2, "0", "0", cl_ord_id="OLD1", order_id="O-old-9",
               order_qty="50", leaves="50"),
        report(3, "2", "2", cl_ord_id="OLD1", order_id="O-old-9",
               order_qty="50", cum="50", leaves="0", last_qty="50",
               last_px="10.00", avg="10.0000"),
    ]) + "\n", encoding="utf-8")

    assert transport.runtime_for_order("O-old-9") is None
    body = (await http.get("/orders/O-old-9/timeline")).json()
    assert body["session"] == "OLDSESS"
    assert body["verdict"] == "PASS"


# --- 6: loopback and the serve subcommand ----------------------------------

def test_serve_refuses_a_non_loopback_host(tmp_path):
    out = io.StringIO()
    code = serve_files([write_log(tmp_path)], host="0.0.0.0", port=1, out=out)
    assert code == 2


def test_serve_subcommand_refuses_a_non_loopback_host(tmp_path):
    out = io.StringIO()
    code = cli_main(["serve", write_log(tmp_path), "--host", "10.0.0.1"],
                    out=out)
    assert code == 2


def test_serve_subcommand_reports_a_missing_file(tmp_path):
    out = io.StringIO()
    assert cli_main(["serve", str(tmp_path / "nope.log")], out=out) == 2


async def test_serve_subcommand_actually_serves(tmp_path):
    """Start `serve` on a free port in a thread and fetch from it."""
    import threading

    path = write_log(tmp_path)
    port = free_port()
    out = io.StringIO()
    thread = threading.Thread(
        target=lambda: cli_main(["serve", path, "--port", str(port)], out=out),
        daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=5.0) as api:
            for _ in range(100):
                try:
                    page = await api.get("/")
                    break
                except httpx.ConnectError:
                    await __import__("asyncio").sleep(0.1)
            else:
                pytest.fail("serve never came up")
            assert page.status_code == 200
            assert "FIX log viewer" in page.text
            body = (await api.get("/messages")).json()
            assert body["total"] == 4
            timeline = (await api.get("/timeline?clordid=V1")).json()
            assert timeline["verdict"] == "PASS"
    finally:
        assert "FIX log viewer on http://127.0.0.1" in out.getvalue()


# --- Cook 8: one summary formatter, shared (spec 4.2) ----------------------

CORPUS = [
    line("A", 1, "IN", fields={98: "0", 108: "30", 141: "Y"}),
    line("D", 2, "IN", fields={11: "S1", 55: "AAPL", 54: "1", 38: "1000",
                               40: "2", 44: "10.00", 21: "1",
                               60: "20260927-09:00:00.000"}),
    report(2, "0", "0", cl_ord_id="S1"),
    report(3, "1", "1", cl_ord_id="S1", cum="400", leaves="600",
           last_qty="400", last_px="10.00", avg="10.0000"),
    report(4, "2", "2", cl_ord_id="S1", cum="1000", leaves="0",
           last_qty="600", last_px="10.00", avg="10.0000"),
    line("G", 3, "IN", fields={11: "S2", 41: "S1", 55: "AAPL", 54: "1",
                               38: "800", 40: "2", 44: "10.50", 21: "1",
                               60: "20260927-09:00:01.000"}),
    line("F", 4, "IN", fields={11: "S3", 41: "S2", 55: "AAPL", 54: "1",
                               60: "20260927-09:00:02.000"}),
    line("9", 5, "OUT", fields={11: "S3", 41: "S2", 37: "O-1", 39: "2",
                                434: "1", 102: "0", 58: "Too late"}),
    line("3", 6, "OUT", fields={45: "9", 373: "1", 58: "Required tag missing"}),
    report(7, "8", "8", cl_ord_id="S4", cum="0", leaves="0",
           extra={103: "3", 58: "Band"}),
    line("5", 8, "IN", fields={58: "done"}),
]


def test_the_web_list_says_exactly_what_the_cli_says(tmp_path):
    """Two renderings of one corpus, from the same code, must not differ."""
    import io

    from orderecho_FixDict import default_dictionary
    from orderecho_LogView import Painter, format_line
    from orderecho_LogParse import LogParser, merge_chronologically
    from orderecho_Summary import SummaryContext

    path = write_log(tmp_path, CORPUS)
    dictionary = default_dictionary()

    parser = LogParser()
    messages = merge_chronologically(parser.parse_files([path]))
    context = SummaryContext()
    cli = []
    for message in messages:
        cli.append(format_line(message, dictionary, Painter(False), context))
        context.note(message)

    source = MessageSource([path])
    assert len(source.messages) == len(messages) == len(CORPUS)
    for rendered, message in zip(cli, source.messages):
        payload = source.as_json(message)
        assert payload["summary"] in rendered, payload["summary"]
        assert payload["msg_type_name"] in rendered
        assert payload["stamp"] in rendered
        for flag in payload["flags"]:
            assert flag in rendered


async def test_the_list_payload_carries_what_a_row_needs(tmp_path):
    _source, api = client_for([write_log(tmp_path, CORPUS)])
    async with api:
        body = (await api.get("/viewer/messages")).json()
    for message in body["messages"]:
        assert set(message) >= {"stamp", "seq", "msg_type_name", "summary",
                                "style", "flags", "direction", "session"}


async def test_the_seq_column_is_there(tmp_path):
    """Spec 4.2: the web list gets a seq column."""
    _source, api = client_for([write_log(tmp_path, CORPUS)])
    async with api:
        body = (await api.get("/viewer/messages")).json()
        page = (await api.get("/viewer/")).text
    assert [m["seq"] for m in body["messages"]][:3] == [1, 2, 2]
    assert 'class="seq"' in page


async def test_a_replace_in_the_web_list_says_what_changed(tmp_path):
    _source, api = client_for([write_log(tmp_path, CORPUS)])
    async with api:
        body = (await api.get("/viewer/messages?msg_type=G")).json()
    assert body["messages"][0]["summary"] == \
        "Replace S1->S2 qty 1000->800 px 10.00->10.50"


async def test_the_web_list_is_in_time_order_across_files(tmp_path):
    later = write_log(tmp_path, [line("D", 9, "IN", fields={
        11: "LATE", 55: "TSLA", 54: "1", 38: "5", 40: "1", 21: "1",
        60: "20260927-23:00:00.000"})], name="zeta_20260927.log")
    earlier = write_log(tmp_path, [line("D", 1, "IN", fields={
        11: "EARLY", 55: "IBM", 54: "1", 38: "5", 40: "1", 21: "1",
        60: "20260927-01:00:00.000"})], name="alpha_20260927.log")
    _source, api = client_for([later, earlier])
    async with api:
        body = (await api.get("/viewer/messages")).json()
    assert [m["fields"][7][1] for m in body["messages"]] == ["EARLY", "LATE"] \
        or [m["summary"].split("11=")[1] for m in body["messages"]] == \
           ["EARLY", "LATE"]


def test_the_cursor_is_the_highest_index_not_the_last_row(tmp_path):
    """Time order means the newest thing read is not always last in the list.

    The helper stamps a line from its MsgSeqNum, so seq 5 happened after
    seq 2 -- and reading the seq-2 file second puts a high index first.
    """
    path = write_log(tmp_path, [line("D", 5, "IN", fields={
        11: "LATER", 55: "IBM", 54: "1", 38: "5", 40: "1", 21: "1",
        60: "20260927-09:00:05.000"})])
    source = MessageSource([path])
    older = tmp_path / "beta_20260927.log"
    older.write_text(line("D", 2, "IN", fields={
        11: "OLDER", 55: "IBM", 54: "1", 38: "5", 40: "1", 21: "1",
        60: "20260927-09:00:02.000"}) + "\n", encoding="utf-8")
    source.patterns.append(str(older))
    source.refresh()

    # It sorts first, because it happened first, but it was read second.
    assert source.messages[0].get(11) == "OLDER"
    assert max(message.index for message in source.messages) == \
        source.messages[0].index

    # So a cursor taken from the last row would go backwards and replay.
    picked = source.select()
    assert max(message.index for message in picked) != picked[-1].index


# --- Cook 8: the shared stylesheet (spec 8.2) ------------------------------

async def test_the_stylesheet_is_served(tmp_path):
    from fastapi import FastAPI

    from orderecho_LogViewer import build_assets_router

    app = FastAPI()
    app.include_router(build_assets_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://v") as api:
        response = await api.get("/assets/orderecho.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert ":root {" in response.text


async def test_serve_mode_serves_the_stylesheet_too(tmp_path):
    """`serve` mounts the viewer at the root; the CSS still has to be there."""
    import threading

    path = write_log(tmp_path)
    port = free_port()
    out = io.StringIO()
    thread = threading.Thread(
        target=lambda: cli_main(["serve", path, "--port", str(port)], out=out),
        daemon=True)
    thread.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                 timeout=5.0) as api:
        for _ in range(100):
            try:
                page = await api.get("/")
                break
            except httpx.ConnectError:
                await __import__("asyncio").sleep(0.1)
        else:
            pytest.fail("serve never came up")
        assert '<link rel="stylesheet" href="/assets/orderecho.css">' in page.text
        css = await api.get("/assets/orderecho.css")
        assert css.status_code == 200
        assert ":root {" in css.text


# --- Cook 8: the guide (spec 8.4) -----------------------------------------

def guide_client():
    from fastapi import FastAPI

    from orderecho_LogViewer import build_assets_router, build_guide_router

    app = FastAPI()
    app.include_router(build_assets_router())
    app.include_router(build_guide_router())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://g")


async def test_the_guide_serves_its_index():
    async with guide_client() as api:
        for path in ("/guide", "/guide/", "/guide/index.html"):
            response = await api.get(path)
            assert response.status_code == 200, path
            assert "OrderEchoFixEmulator" in response.text
            assert "<aside class=\"sidebar\">" in response.text


async def test_the_guide_serves_every_page():
    import orderecho_BuildDocs as builddocs

    async with guide_client() as api:
        for _slug, filename in builddocs.PAGES:
            response = await api.get(f"/guide/{filename}")
            assert response.status_code == 200, filename
            assert "<title>" in response.text


async def test_the_guide_serves_its_stylesheet_both_ways():
    async with guide_client() as api:
        for path in ("/guide/assets/orderecho.css", "/assets/orderecho.css"):
            response = await api.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("text/css")


async def test_an_unknown_guide_page_is_a_helpful_404():
    async with guide_client() as api:
        response = await api.get("/guide/nonsense.html")
    assert response.status_code == 404
    assert "/guide" in response.text


async def test_the_guide_will_not_serve_anything_outside_the_site():
    """A path that survives the client unchanged still stays inside the site."""
    async with guide_client() as api:
        for path in ("/guide/..%2Forderecho_Main.py",
                     "/guide/%2e%2e/orderecho_Main.py",
                     "/guide/..%2f..%2fetc%2fpasswd",
                     "/guide/../orderecho_Main.py"):
            response = await api.get(path)
            assert response.status_code == 404, path
            assert "ORDERECHO_VERSION" not in response.text


async def test_the_engine_serves_the_guide_and_redirects_the_root(engine):
    _transport, http = engine

    page = await http.get("/guide")
    assert page.status_code == 200
    assert "OrderEchoFixEmulator" in page.text

    root = await http.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/guide"

    followed = await http.get("/", follow_redirects=True)
    assert followed.status_code == 200
    assert "<aside class=\"sidebar\">" in followed.text

    css = await http.get("/assets/orderecho.css")
    assert css.status_code == 200


def test_the_banner_shows_the_guide_url():
    from types import SimpleNamespace

    from orderecho_Config import load_config
    from orderecho_Main import banner

    config = load_config("config/orderecho_multi.yaml")
    transport = SimpleNamespace(evidence=SimpleNamespace(path="e.jsonl"))
    api = SimpleNamespace(host="127.0.0.1", bound_port=8090)
    text = banner(config, transport, api)
    assert "guide          : http://127.0.0.1:8090/guide" in text
    assert "log viewer     : http://127.0.0.1:8090/viewer" in text
