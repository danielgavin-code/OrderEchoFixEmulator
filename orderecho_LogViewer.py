"""FIX log viewer -- web.

A FastAPI router plus one self-contained page.  The engine mounts the router
at /viewer over its own logs; `orderecho_LogView.py serve` mounts the same
router over whatever files you point it at, with no engine involved.

Loopback only, always: this reads whatever log files it is given and has no
authentication of any kind.
"""

from __future__ import annotations

import glob as globmodule
import os
import sys

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from orderecho_Config import is_loopback
from orderecho_FixDict import default_dictionary
from orderecho_LogParse import (
    FIX_LOG_NAME,
    LogParser,
    merge_chronologically,
)
from orderecho_Summary import (
    TAG_CL_ORD_ID,
    TAG_ORDER_ID,
    TAG_ORIG_CL_ORD_ID,
    TAG_SYMBOL,
    flags_for,
    is_reject,
    msg_type_name,
    stamp_of,
    summarize_all,
)
from orderecho_Timeline import build_chain

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(HERE, "viewer", "index.html")
CSS_PATH = os.path.join(HERE, "web", "orderecho.css")


class MessageSource:
    """The messages behind the viewer, kept up to date as files grow.

    Re-globs its patterns on every refresh, so a new day's log file appears
    without a restart.
    """

    def __init__(self, patterns, me: str | None = None,
                 max_messages: int = 20000) -> None:
        self.patterns = list(patterns)
        self.me = me
        self.max_messages = max_messages
        self.parser = LogParser(me=me)
        self.dictionary = default_dictionary()
        self.messages: list = []
        self.summaries: dict = {}
        self._offsets: dict = {}
        self._line_no: dict = {}
        self.refresh()

    # ---------------------------------------------------------- reading

    def _paths(self) -> list:
        found: list = []
        for pattern in self.patterns:
            for path in sorted(globmodule.glob(pattern)):
                if path not in found:
                    found.append(path)
            if not globmodule.has_magic(pattern) and os.path.exists(pattern) \
                    and pattern not in found:
                found.append(pattern)
        return found

    def refresh(self) -> int:
        """Read whatever is new.  Returns how many messages that was."""
        before = len(self.messages)
        for path in self._paths():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = self._offsets.get(path, 0)
            if size < offset:               # truncated: start over
                offset = 0
                self._line_no[path] = 0
            if size == offset:
                continue
            match = FIX_LOG_NAME.match(os.path.basename(path))
            session = match.group("session") if match else None
            name = os.path.basename(path)
            if name not in self.parser.stats.files:
                # parse_line does not know about files; record it here so
                # /stats can say how many it is reading.
                self.parser.stats.files.append(name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh:
                        self._line_no[path] = self._line_no.get(path, 0) + 1
                        self.messages.extend(self.parser.parse_line(
                            line, self._line_no[path], name, session))
                    self._offsets[path] = fh.tell()
            except OSError:
                continue
        if len(self.messages) - before:
            # Several files read one after another interleave nothing, so put
            # them back in the order things happened before anyone sees them.
            self.messages = merge_chronologically(self.messages)
            if len(self.messages) > self.max_messages:
                self.messages = self.messages[-self.max_messages:]
            self.summaries = summarize_all(self.messages, self.dictionary)
        return len(self.messages) - before

    # ---------------------------------------------------------- queries

    @property
    def sessions(self) -> list:
        seen = []
        for message in self.messages:
            if message.session and message.session not in seen:
                seen.append(message.session)
        return sorted(seen)

    def select(self, after: int = 0, limit: int = 300, session=None,
               msg_type=None, direction=None, symbol=None, clordid=None,
               order_id=None, injected=False, rejects=False, grep=None) -> list:
        wanted_types = ({part.strip() for part in msg_type.split(",")
                         if part.strip()} if msg_type else None)
        chain_ids = None
        if clordid or order_id:
            chain = build_chain(self.messages, cl_ord_id=clordid,
                                order_id=order_id)
            chain_ids = set(chain.ids) | set(chain.order_ids)

        picked = []
        for message in self.messages:
            if message.index <= after:
                continue
            if session and message.session != session:
                continue
            if wanted_types and message.msg_type not in wanted_types:
                continue
            if direction and message.direction != direction:
                continue
            if symbol and message.get(TAG_SYMBOL) != symbol:
                continue
            if injected and not message.injected:
                continue
            if rejects and not is_reject(message):
                continue
            if grep and grep.lower() not in message.raw.lower():
                continue
            if chain_ids is not None:
                ids = {message.get(TAG_CL_ORD_ID),
                       message.get(TAG_ORIG_CL_ORD_ID),
                       message.get(TAG_ORDER_ID)}
                if not (ids & chain_ids):
                    continue
            picked.append(message)
        return picked[-limit:] if limit and limit > 0 else picked

    def by_index(self, index: int):
        for message in self.messages:
            if message.index == index:
                return message
        return None

    def stats(self) -> dict:
        by_session: dict = {}
        by_type: dict = {}
        by_direction: dict = {}
        injected = rejects = 0
        first = last = None
        for message in self.messages:
            by_session[message.session or "-"] = \
                by_session.get(message.session or "-", 0) + 1
            by_type[message.msg_type or "?"] = \
                by_type.get(message.msg_type or "?", 0) + 1
            by_direction[message.direction] = \
                by_direction.get(message.direction, 0) + 1
            if message.injected:
                injected += 1
            if is_reject(message):
                rejects += 1
            if message.ts:
                first = message.ts if first is None else min(first, message.ts)
                last = message.ts if last is None else max(last, message.ts)
        return {
            "messages": len(self.messages),
            "files": list(self.parser.stats.files),
            "by_session": by_session,
            "by_msg_type": by_type,
            "by_direction": by_direction,
            "injected": injected,
            "rejects": rejects,
            "bad_checksum": self.parser.stats.bad_checksum,
            "bad_length": self.parser.stats.bad_length,
            "unparseable_lines": self.parser.stats.skipped_lines,
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
        }


    def as_json(self, message) -> dict:
        """The wire form of one message, carrying the shared summary.

        The web viewer shows exactly the words the CLI shows, because they
        are the same words: `summary` is what `orderecho_Summary` said.
        """
        summary, style = self.summaries.get(
            message.index, ("", None))
        payload = message.as_dict()
        payload["summary"] = summary
        payload["style"] = style
        payload["msg_type_name"] = msg_type_name(message, self.dictionary)
        payload["stamp"] = stamp_of(message)
        payload["flags"] = flags_for(message)
        return payload


def _decoded(message, dictionary) -> list:
    version = message.begin_string
    rows = []
    for tag, value in message.fields:
        rows.append({
            "tag": tag,
            "name": dictionary.tag_name(tag, version),
            "value": value,
            "enum": dictionary.enum_name(tag, value, version),
        })
    return rows


def build_assets_router() -> APIRouter:
    """`/assets/orderecho.css` -- the one stylesheet the guide and the viewer
    share.  It is mounted on the app rather than on the viewer's router,
    because both pages ask for the same absolute path.
    """
    router = APIRouter()

    @router.get("/assets/orderecho.css", include_in_schema=False)
    async def stylesheet():
        try:
            with open(CSS_PATH, "r", encoding="utf-8") as handle:
                return Response(handle.read(), media_type="text/css")
        except OSError as exc:                      # pragma: no cover
            return Response(f"/* stylesheet missing: {exc} */",
                            media_type="text/css", status_code=500)

    return router


SITE_DIR = os.path.join(HERE, "docs", "site")


def build_guide_router(prefix: str = "/guide",
                       site_dir: str = SITE_DIR) -> APIRouter:
    """Serve the built documentation site.

    The same files that open from disk, served over HTTP so the engine can
    point at them.  Nothing is rendered here: if `docs/site` is stale, the
    fix is to run the build, and a test says so.
    """
    router = APIRouter()

    def read(name: str):
        path = os.path.normpath(os.path.join(site_dir, name))
        if not path.startswith(os.path.abspath(site_dir) + os.sep) \
                and path != os.path.abspath(site_dir):
            return None                       # nothing outside the site
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def page_response(name: str):
        body = read(name)
        if body is None:
            return HTMLResponse(
                "<h1>Not in the guide</h1>"
                "<p>No such page. <a href=\"/guide\">Start here.</a></p>",
                status_code=404)
        return HTMLResponse(body)

    async def index():
        return page_response("index.html")

    async def page(name: str):
        if name == "assets/orderecho.css" or name.endswith(".css"):
            body = read(name)
            if body is None:
                return Response("/* not found */", media_type="text/css",
                                status_code=404)
            return Response(body, media_type="text/css")
        return page_response(name)

    router.add_api_route(prefix, index, methods=["GET"],
                         response_class=HTMLResponse, include_in_schema=False)
    router.add_api_route(prefix + "/", index, methods=["GET"],
                         response_class=HTMLResponse, include_in_schema=False)
    router.add_api_route(prefix + "/{name:path}", page, methods=["GET"],
                         response_class=HTMLResponse, include_in_schema=False)
    return router


def build_router(source: MessageSource, prefix: str = "") -> APIRouter:
    """The viewer's page and its four endpoints."""
    router = APIRouter(prefix=prefix)
    dictionary = default_dictionary()

    async def page():
        try:
            with open(PAGE_PATH, "r", encoding="utf-8") as handle:
                return HTMLResponse(handle.read())
        except OSError as exc:
            return HTMLResponse(
                f"<h1>viewer page missing</h1><p>{exc}</p>", status_code=500)

    router.add_api_route("/", page, methods=["GET"],
                         response_class=HTMLResponse, include_in_schema=False)
    if prefix:
        # So that /viewer serves the page as well as /viewer/. At the root
        # there is no prefix to append to, and FastAPI rejects an empty path.
        router.add_api_route("", page, methods=["GET"],
                             response_class=HTMLResponse,
                             include_in_schema=False)

    @router.get("/messages")
    async def messages(
        after: int = 0,
        limit: int = Query(300, ge=1, le=5000),
        session: str | None = None,
        msg_type: str | None = None,
        direction: str | None = None,
        symbol: str | None = None,
        clordid: str | None = None,
        order_id: str | None = None,
        injected: bool = False,
        rejects: bool = False,
        grep: str | None = None,
        live: bool = True,
    ):
        """Messages in time order, filtered; `after` returns only what is new."""
        if live:
            source.refresh()
        picked = source.select(
            after=after, limit=limit, session=session, msg_type=msg_type,
            direction=direction, symbol=symbol, clordid=clordid,
            order_id=order_id, injected=injected, rejects=rejects, grep=grep,
        )
        # The list is in time order now, so the last one is not necessarily
        # the newest thing read: take the highest index or the cursor slips.
        cursor = max((message.index for message in picked), default=after)
        return {
            "messages": [source.as_json(message) for message in picked],
            "cursor": cursor,
            "total": len(source.messages),
            "sessions": source.sessions,
        }

    @router.get("/message/{index}")
    async def message_detail(index: int):
        """One message with every field named and every enum decoded."""
        message = source.by_index(index)
        if message is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"no message #{index}"},
                status_code=404)
        payload = source.as_json(message)
        payload["decoded"] = _decoded(message, dictionary)
        return payload

    @router.get("/timeline")
    async def timeline(clordid: str | None = None,
                       order_id: str | None = None):
        """One order's chain and its checks, found by ClOrdID or OrderID."""
        if not (clordid or order_id):
            return JSONResponse(
                {"error": "invalid_request",
                 "detail": "give clordid or order_id"}, status_code=400)
        source.refresh()
        chain = build_chain(source.messages, cl_ord_id=clordid,
                            order_id=order_id)
        if not chain.steps:
            return JSONResponse(
                {"error": "not_found",
                 "detail": f"no messages for {clordid or order_id}"},
                status_code=404)
        return chain.as_dict(dictionary)

    @router.get("/stats")
    async def stats():
        """Counts per session, type and direction, plus the flag totals."""
        source.refresh()
        return source.stats()

    return router


def engine_source(config) -> MessageSource:
    """Every session's FIX log, today's and the days before it."""
    pattern = os.path.join(config.logging.log_dir, "fix", "*.log")
    return MessageSource([pattern])


def serve_files(paths, host: str = "127.0.0.1", port: int = 8091,
                me: str | None = None, out=sys.stdout) -> int:
    """Stand the viewer up on its own, with no engine behind it."""
    import uvicorn
    from fastapi import FastAPI

    if not is_loopback(host):
        print(f"error: the viewer serves loopback addresses only, not {host!r}",
              file=sys.stderr)
        return 2

    source = MessageSource(paths, me=me)
    app = FastAPI(title="OrderEcho FIX log viewer", docs_url=None,
                  redoc_url=None)
    app.include_router(build_assets_router())
    app.include_router(build_router(source))

    print(f"FIX log viewer on http://{host}:{port}  "
          f"({source.stats()['messages']} messages from "
          f"{len(source.parser.stats.files)} file(s))", file=out, flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning",
                access_log=False)
    return 0
