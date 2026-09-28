"""Local control API — the emulator's backstage intercom.

A FastAPI app served by uvicorn *inside the engine's own event loop*, so every
handler can touch the engine directly without locks: there is only ever one
thread running the session.

Two rules shape everything here:

* It never bypasses the pure core. Order actions go through ``OrderBook`` and
  every outbound message goes through ``Session``, so sequence numbers,
  evidence and logs stay correct whether a fill came from a timer, an inbound
  message or a curl command.
* Deliberate mischief is always recorded with ``injected: true``.

It is emulator-only: a real venue has nothing like it, and the agent under test
must never need it for core behavior.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from orderecho_Config import ConfigError, is_loopback
from orderecho_FixDict import default_dictionary
from orderecho_LogViewer import (
    build_assets_router,
    build_guide_router,
    build_router,
    engine_source,
)
from orderecho_Timeline import build_chain
from orderecho_OrderBook import OrderActionError
from orderecho_Pricing import resolve_quote
from orderecho_Session import State
from orderecho_Transport import InjectionError
from orderecho_Version import ORDERECHO_BUILD, ORDERECHO_VERSION

# Error code -> HTTP status.
ERROR_STATUS = {
    "not_found": 404,
    "session_not_active": 409,
    "order_closed": 409,
    "invalid_request": 400,
    "conflict": 409,
    "price_pending": 409,
    "ambiguous_session": 409,
}


class ApiError(Exception):
    """An error with a machine-readable code, rendered per §4."""

    def __init__(self, code: str, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status or ERROR_STATUS.get(code, 400)


# ------------------------------------------------------------------- bodies


class FillBody(BaseModel):
    qty: int
    price: str | None = None


class FillRestBody(BaseModel):
    price: str | None = None


class CancelBody(BaseModel):
    text: str | None = None


class LogoutBody(BaseModel):
    text: str | None = None


class InjectNextBody(BaseModel):
    msg_type: str | None = None
    set_fields: dict[str, str] | None = Field(default=None, alias="set")
    remove: list[str] | None = None
    corrupt_checksum: bool = False
    count: int = 1

    model_config = {"populate_by_name": True}


class SeqGapBody(BaseModel):
    skip: int


class DuplicateLastBody(BaseModel):
    poss_dup: bool = False


# -------------------------------------------------------------------- app


def build_app(transport) -> FastAPI:
    """Build the control API bound to a running Transport."""

    app = FastAPI(
        title="OrderEchoFixEmulator control API",
        version=ORDERECHO_VERSION,
        description=(
            f"Local, unauthenticated control API for the FIX emulator "
            f"({ORDERECHO_BUILD}). "
            "Human documentation is planned for /guide; the generated "
            "reference is at /docs."
        ),
        docs_url="/docs",
    )
    app.state.transport = transport

    # ------------------------------------------------------------ plumbing

    def engine_log():
        return transport.engine_log

    @app.middleware("http")
    async def log_api_calls(request: Request, call_next):
        request.state.api_params = ""
        response = await call_next(request)
        params = getattr(request.state, "api_params", "") or ""
        try:
            engine_log().info(
                f"{'API':<5} {request.method} {request.url.path}{params} "
                f"-> {response.status_code}"
            )
        except Exception:  # logging must never break a response
            pass
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request,
                                      exc: RequestValidationError):
        # Keep one error shape across the whole API (§4) rather than letting
        # FastAPI's 422 envelope leak out.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', ())[1:])}: "
            f"{err.get('msg', 'invalid')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request",
                     "detail": problems or "invalid request body"},
        )

    def note(request: Request, **params) -> None:
        """Record parameters for the engine-log line."""
        text = "".join(f" {key}={value}" for key, value in params.items()
                       if value is not None)
        request.state.api_params = text

    def resolve(session_id: str):
        """A session by id, or 404."""
        runtime = transport.runtime(session_id)
        if runtime is None:
            raise ApiError(
                "not_found",
                f"No such session: {session_id} "
                f"(have {', '.join(transport.session_ids)})",
            )
        return runtime

    def default_runtime():
        """The session the unscoped routes act on.

        With one session that is obvious; with several it takes an explicit
        engine.default_session, and without one we would rather say so than
        guess which session the caller meant.
        """
        runtime = transport.default_runtime
        if runtime is None:
            raise ApiError(
                "ambiguous_session",
                "This engine runs several sessions and no "
                "engine.default_session is configured; use "
                "/sessions/{id}/... instead. Sessions: "
                f"{', '.join(transport.session_ids)}",
            )
        return runtime

    def require_active(runtime):
        if not runtime.session_active:
            state = (runtime.session.state.value if runtime.session
                     else State.DISCONNECTED.value)
            raise ApiError("session_not_active",
                           f"Session {runtime.id} is {state}, not ACTIVE")
        return runtime.session

    def order_book(runtime):
        if runtime.order_book is None:
            raise ApiError("conflict",
                           f"No order book: session {runtime.id} has no rules "
                           f"configured")
        return runtime.order_book

    def owning_runtime(order_id: str):
        """OrderIDs are unique engine-wide, so the order names its session."""
        runtime = transport.runtime_for_order(order_id)
        if runtime is None:
            raise ApiError("not_found", f"No such order: {order_id}")
        return runtime

    def run_order_action(request: Request, order_id: str, call,
                         qty=None, price=None, check_price: bool = False):
        """Shared shape for the four order endpoints.

        The order itself says which session owns it.  Facts about the order
        come first: an unknown order is a 404 and a closed one a 409 whether
        or not a session is up.  Only once the request is known to be sane do
        we insist on that session being live.
        """
        runtime = owning_runtime(order_id)
        book = order_book(runtime)
        try:
            book.precheck(order_id, qty=qty, price=price,
                          check_price=check_price)
        except OrderActionError as exc:
            raise ApiError(exc.code, exc.detail) from exc

        require_active(runtime)

        try:
            actions = call(book)
        except OrderActionError as exc:
            raise ApiError(exc.code, exc.detail) from exc
        sent = runtime.run_app_actions(actions)
        order = book.get_order(order_id)
        return {"session": runtime.id,
                "order": order.snapshot() if order else None, "sent": sent}

    # -------------------------------------------------------- §5 orders

    @app.post("/orders/{order_id}/fill")
    async def fill_order(order_id: str, body: FillBody, request: Request):
        """Fill part of a working order, at a price you choose."""
        note(request, qty=body.qty, price=body.price)
        return run_order_action(
            request, order_id,
            lambda book: book.manual_fill(order_id, body.qty, body.price),
            qty=body.qty, price=body.price, check_price=True,
        )

    @app.post("/orders/{order_id}/fill-rest")
    async def fill_rest(order_id: str, request: Request,
                        body: FillRestBody | None = None):
        """Fill everything the order has left."""
        body = body or FillRestBody()
        note(request, price=body.price)
        return run_order_action(
            request, order_id,
            lambda book: book.manual_fill_rest(order_id, body.price),
            price=body.price, check_price=True,
        )

    @app.post("/orders/{order_id}/cancel")
    async def cancel_order(order_id: str, request: Request,
                           body: CancelBody | None = None):
        """Cancel a working order, as the counterparty would."""
        body = body or CancelBody()
        note(request, text=body.text)
        return run_order_action(
            request, order_id,
            lambda book: book.manual_cancel(order_id, body.text),
        )

    @app.post("/orders/{order_id}/hold")
    async def hold_order(order_id: str, request: Request):
        """Stop the scheduled reports for an order and leave it working."""
        note(request)
        return run_order_action(request, order_id,
                                lambda book: book.hold(order_id))

    # ------------------------------------------------------- session

    def _session_status(runtime) -> dict:
        session = runtime.session
        book = runtime.order_book
        open_orders = (len([o for o in book.orders() if not o.closed])
                       if book is not None else 0)
        return {
            "id": runtime.id,
            "state": session.state.value if session else State.DISCONNECTED.value,
            "fix_version": runtime.spec.fix_version,
            "sender_comp_id": runtime.spec.sender_comp_id,
            "target_comp_id": runtime.spec.target_comp_id,
            "port": runtime.spec.port,
            "peer": f"{runtime.peer[0]}:{runtime.peer[1]}"
                    if runtime.peer else None,
            "next_out": session.next_out if session
            else runtime.seq_store.load()[0],
            "next_in": session.expected_in if session
            else runtime.seq_store.load()[1],
            "heart_bt_int": session.heart_bt_int if session else None,
            "last_sent": _iso(session.last_sent) if session else None,
            "last_received": _iso(session.last_received) if session else None,
            "pending_test_req_id": session.pending_test_req_id if session
            else None,
            "open_orders": open_orders,
            "run_id": transport.run_id,
            "evidence_path": transport.evidence.path,
            "fix_log_path": runtime.fix_log.path,
            "engine_log_path": transport.engine_log.path,
        }

    def _do_test_request(runtime, request: Request) -> dict:
        note(request)
        session = require_active(runtime)
        sent = runtime.send_test_request()
        return {"test_req_id": session.pending_test_req_id, "sent": sent}

    def _do_logout(runtime, request: Request, body) -> dict:
        body = body or LogoutBody()
        text = body.text or "Logout via control API"
        note(request, text=text)
        session = require_active(runtime)
        sent = runtime.run_session_actions(session.initiate_logout(text))
        return {"state": session.state.value, "sent": sent}

    def _do_disconnect(runtime, request: Request) -> dict:
        note(request)
        if runtime.session is None or not runtime._busy:
            raise ApiError("session_not_active", "No connection to drop")
        runtime.force_disconnect("Disconnected via control API")
        return {"disconnected": True}

    def _do_reset_seqnums(runtime, request: Request) -> dict:
        note(request)
        state = (runtime.session.state if runtime.session
                 else State.DISCONNECTED)
        if state is not State.DISCONNECTED or runtime._busy:
            raise ApiError(
                "conflict",
                f"Sequence numbers can only be reset while DISCONNECTED; "
                f"session {runtime.id} is {state.value}",
            )
        runtime.seq_store.reset()
        runtime.log_info("Sequence numbers reset to 1/1 via control API")
        runtime.record_event("seqnums reset", "via control API")
        archived = runtime.archive_message_store("reset via control API")
        next_out, next_in = runtime.seq_store.load()
        return {"next_out": next_out, "next_in": next_in,
                "archived_store": archived}

    def _do_inject_next(runtime, body, request: Request) -> dict:
        note(request, msg_type=body.msg_type, count=body.count,
             corrupt_checksum=body.corrupt_checksum or None)
        require_active(runtime)
        try:
            mutation = runtime.injector.queue(
                msg_type=body.msg_type,
                set_fields=body.set_fields,
                remove_tags=body.remove,
                corrupt_checksum=body.corrupt_checksum,
                count=body.count,
            )
        except InjectionError as exc:
            raise ApiError("invalid_request", str(exc)) from exc
        runtime.record_event("injection queued", mutation.describe(),
                             injected=True)
        return {"queued": mutation.as_dict()}

    def _do_inject_seq_gap(runtime, body, request: Request) -> dict:
        note(request, skip=body.skip)
        require_active(runtime)
        try:
            next_out = runtime.inject_seq_gap(body.skip)
        except InjectionError as exc:
            raise ApiError("invalid_request", str(exc)) from exc
        return {"skipped": body.skip, "next_out": next_out}

    def _do_duplicate_last(runtime, body, request: Request) -> dict:
        body = body or DuplicateLastBody()
        note(request, poss_dup=body.poss_dup)
        require_active(runtime)
        try:
            sent = runtime.duplicate_last(body.poss_dup)
        except InjectionError as exc:
            raise ApiError("conflict", str(exc)) from exc
        return {"sent": sent}

    def _rules_body(runtime) -> dict:
        band = runtime.spec.price_band
        band_json = {
            "enabled": bool(band.enabled) if band else False,
            "pct": band.pct if band else None,
            "mode": band.mode if band else None,
            "enforce_on_fallback": bool(band.enforce_on_fallback) if band
            else False,
            "lookup_timeout_ms": band.lookup_timeout_ms if band else None,
            "on_no_reference": getattr(band, "on_no_reference", None),
        }
        rules = runtime.spec.rules
        listed = ([_rule_json(rule, rules.default_delay_ms) for rule in rules]
                  if rules is not None else [])
        return {"session": runtime.id, "rules": listed,
                "price_band": band_json}

    # -- session-scoped routes ------------------------------------------

    @app.get("/sessions")
    async def list_sessions():
        """Every session this engine runs, and what each is doing."""
        return {"sessions": [_session_status(runtime)
                             for runtime in transport.runtimes.values()]}

    @app.get("/sessions/{session_id}/status")
    async def session_status(session_id: str):
        """One session: its identity, state, sequence numbers and counts."""
        return _session_status(resolve(session_id))

    @app.get("/sessions/{session_id}/orders")
    async def session_orders(session_id: str,
                             status: str = Query("all",
                                                 pattern="^(open|closed|all)$")):
        """Orders belonging to one session."""
        runtime = resolve(session_id)
        return {"session": runtime.id,
                "orders": _orders_of(runtime, status)}

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(
        session_id: str,
        limit: int = Query(50, ge=1, le=1000),
        direction: str = Query("all", pattern="^(in|out|all)$"),
    ):
        """Recent messages on one session, newest last."""
        runtime = resolve(session_id)
        return {"session": runtime.id,
                "messages": runtime.recent_messages(limit, direction)}

    @app.get("/sessions/{session_id}/rules")
    async def session_rules(session_id: str):
        """The behavior rules one session answers with."""
        return _rules_body(resolve(session_id))

    @app.post("/sessions/{session_id}/test-request")
    async def session_test_request(session_id: str, request: Request):
        """Send a TestRequest and expect the Heartbeat that answers it."""
        return _do_test_request(resolve(session_id), request)

    @app.post("/sessions/{session_id}/logout")
    async def session_logout_scoped(session_id: str, request: Request,
                                    body: LogoutBody | None = None):
        """Send a Logout and wait for the reply."""
        return _do_logout(resolve(session_id), request, body)

    @app.post("/sessions/{session_id}/disconnect")
    async def session_disconnect_scoped(session_id: str, request: Request):
        """Drop the socket without a Logout, so the client sees a dead link."""
        return _do_disconnect(resolve(session_id), request)

    @app.post("/sessions/{session_id}/reset-seqnums")
    async def session_reset_scoped(session_id: str, request: Request):
        """Reset stored sequence numbers to 1/1 for one session."""
        return _do_reset_seqnums(resolve(session_id), request)

    @app.post("/sessions/{session_id}/inject/next")
    async def session_inject_next(session_id: str, body: InjectNextBody,
                                  request: Request):
        """Alter the next outbound message of a type: set, remove or corrupt fields."""
        return _do_inject_next(resolve(session_id), body, request)

    @app.post("/sessions/{session_id}/inject/seq-gap")
    async def session_inject_seq_gap(session_id: str, body: SeqGapBody,
                                     request: Request):
        """Skip outbound sequence numbers, so the client must ask for a resend."""
        return _do_inject_seq_gap(resolve(session_id), body, request)

    @app.post("/sessions/{session_id}/inject/duplicate-last")
    async def session_inject_duplicate(session_id: str, request: Request,
                                       body: DuplicateLastBody | None = None):
        """Send the last outbound message again, marked PossDup."""
        return _do_duplicate_last(resolve(session_id), body, request)

    @app.get("/sessions/{session_id}/inject")
    async def session_list_injections(session_id: str, request: Request):
        """The injections queued on one session."""
        runtime = resolve(session_id)
        note(request)
        return {"session": runtime.id,
                "pending": runtime.injector.as_list()}

    @app.delete("/sessions/{session_id}/inject")
    async def session_clear_injections(session_id: str, request: Request):
        """Forget every injection queued on one session."""
        runtime = resolve(session_id)
        note(request)
        return {"session": runtime.id, "cleared": runtime.injector.clear()}

    # -- legacy routes: the default session -----------------------------

    @app.post("/session/test-request")
    async def legacy_test_request(request: Request):
        """TestRequest on the default session. Prefer the session-scoped route."""
        return _do_test_request(default_runtime(), request)

    @app.post("/session/logout")
    async def legacy_logout(request: Request, body: LogoutBody | None = None):
        """Logout on the default session. Prefer the session-scoped route."""
        return _do_logout(default_runtime(), request, body)

    @app.post("/session/disconnect")
    async def legacy_disconnect(request: Request):
        """Disconnect the default session. Prefer the session-scoped route."""
        return _do_disconnect(default_runtime(), request)

    @app.post("/session/reset-seqnums")
    async def legacy_reset_seqnums(request: Request):
        """Reset the default session's sequence numbers. Prefer the scoped route."""
        return _do_reset_seqnums(default_runtime(), request)

    @app.post("/inject/next")
    async def legacy_inject_next(body: InjectNextBody, request: Request):
        """Inject into the default session. Prefer the session-scoped route."""
        return _do_inject_next(default_runtime(), body, request)

    @app.post("/inject/seq-gap")
    async def legacy_inject_seq_gap(body: SeqGapBody, request: Request):
        """Sequence gap on the default session. Prefer the session-scoped route."""
        return _do_inject_seq_gap(default_runtime(), body, request)

    @app.post("/inject/duplicate-last")
    async def legacy_duplicate_last(request: Request,
                                    body: DuplicateLastBody | None = None):
        """Duplicate on the default session. Prefer the session-scoped route."""
        return _do_duplicate_last(default_runtime(), body, request)

    @app.get("/inject")
    async def legacy_list_injections(request: Request):
        """Injections on the default session. Prefer the session-scoped route."""
        note(request)
        return {"pending": default_runtime().injector.as_list()}

    @app.delete("/inject")
    async def legacy_clear_injections(request: Request):
        """Clear injections on the default session. Prefer the scoped route."""
        note(request)
        return {"cleared": default_runtime().injector.clear()}

    # ---------------------------------------------------------- read

    def _orders_of(runtime, status: str) -> list:
        book = runtime.order_book
        orders = book.orders() if book is not None else []
        if status == "open":
            orders = [order for order in orders if not order.closed]
        elif status == "closed":
            orders = [order for order in orders if order.closed]
        return [order.snapshot() for order in orders]

    @app.get("/health")
    async def health():
        """Version, build and uptime. Answers even when no client is connected."""
        return {"ok": True, "version": ORDERECHO_VERSION,
                "build": ORDERECHO_BUILD,
                "sessions": len(transport.runtimes)}

    @app.get("/status")
    async def status():
        """The default session's status, for one-session setups."""
        return _session_status(default_runtime())

    @app.get("/orders")
    async def list_orders(status: str = Query("all",
                                              pattern="^(open|closed|all)$")):
        """Orders across every session, newest first."""
        listed = []
        for runtime in transport.runtimes.values():
            for snapshot in _orders_of(runtime, status):
                snapshot = dict(snapshot)
                snapshot["session"] = runtime.id
                listed.append(snapshot)
        return {"orders": listed}

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str):
        """One order: its quantities, state and the reports sent for it."""
        runtime = owning_runtime(order_id)
        order = runtime.order_book.get_order(order_id)
        reports = [
            row for row in runtime.messages
            if row["kind"] == "out" and row["msg_type"] == "8"
            and f"|37={order_id}|" in row["raw"]
        ]
        return {
            "session": runtime.id,
            "order": order.snapshot(),
            "cl_ord_id_chain": list(order.cl_ord_id_chain),
            "execution_reports": reports,
        }

    @app.get("/messages")
    async def list_messages(
        limit: int = Query(50, ge=1, le=1000),
        direction: str = Query("all", pattern="^(in|out|all)$"),
    ):
        """Recent messages across every session."""
        return {"messages": default_runtime().recent_messages(limit, direction)}

    @app.get("/price/{symbol}")
    async def get_price(symbol: str):
        """The reference price we would use for a symbol, and where it came from."""
        quote = await resolve_quote(
            transport.price_source, symbol,
            transport.config.pricing.timeout_sec,
        )
        return {"symbol": symbol, "price": str(quote.price),
                "source": quote.source}

    @app.get("/rules")
    async def list_rules():
        """The default session's behavior rules."""
        return _rules_body(default_runtime())

    # ------------------------------------------------- the log viewer
    #
    # Mounted over the engine's own FIX logs, so http://host:port/viewer
    # works whenever the engine does.

    viewer_source = engine_source(transport.config)
    app.include_router(build_assets_router())
    app.include_router(build_guide_router())
    app.include_router(build_router(viewer_source, prefix="/viewer"))

    @app.get("/", include_in_schema=False)
    async def root():
        """The guide is the front door; everything else is linked from it."""
        return RedirectResponse("/guide", status_code=307)

    @app.get("/orders/{order_id}/timeline")
    async def order_timeline(order_id: str):
        """One order's whole life plus the timeline checks, as JSON.

        The live order book names the session, but it only holds this run's
        orders; an order from an earlier run is still ours, and still in our
        logs, so fall back to what the log says rather than 404.
        """
        runtime = transport.runtime_for_order(order_id)
        viewer_source.refresh()
        chain = build_chain(viewer_source.messages, order_id=order_id)
        if not chain.steps:
            if runtime is not None:
                raise ApiError(
                    "not_found",
                    f"order {order_id} is known to session {runtime.id} but "
                    f"has no messages in the FIX logs yet",
                )
            raise ApiError("not_found", f"No such order: {order_id}")
        body = chain.as_dict(default_dictionary())
        body["session"] = runtime.id if runtime is not None else next(
            (step.message.session for step in chain.steps
             if step.message.session), None)
        return body

    return app


def _iso(when) -> str | None:
    if when is None:
        return None
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rule_json(rule, default_delay_ms: int) -> dict:
    if rule.any_symbol:
        match = {"any": True}
    elif rule.symbol is not None:
        match = {"symbol": rule.symbol}
    else:
        low, high = rule.first_letter
        match = {"first_letter": low if low == high else f"{low}-{high}"}
    return {
        "name": rule.name,
        "match": match,
        "behavior": rule.behavior,
        "fills": [str(spec) for spec in rule.fills],
        "then": rule.then,
        "delay_ms": rule.delay_for(default_delay_ms),
        "reject_code": rule.reject_code,
        "text": rule.text,
        "price_band_pct": rule.price_band_pct,
    }


# ------------------------------------------------------------------ server


class _NoSignalServer(uvicorn.Server):
    """uvicorn without its signal handling.

    The engine owns SIGINT/SIGTERM (orderecho_Main installs loop handlers);
    uvicorn's own `signal.signal` calls would replace them and Ctrl+C would
    stop the API but never the acceptor.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class ControlApiServer:
    """Runs the control API as a task on the engine's event loop."""

    def __init__(self, config, transport) -> None:
        self.config = config
        self.transport = transport
        self.app = build_app(transport)
        api = config.control_api
        if not is_loopback(api.host):
            raise ConfigError(
                f"control API host must be a loopback address, got {api.host!r}"
            )
        self.host = api.host
        self.port = api.port
        uvicorn_config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self.server = _NoSignalServer(uvicorn_config)
        self.task = None

    @property
    def bound_port(self) -> int:
        for server in getattr(self.server, "servers", ()) or ():
            for sock in server.sockets:
                return sock.getsockname()[1]
        return self.port

    async def start(self, timeout: float = 10.0):
        import asyncio

        self.task = asyncio.create_task(self.server.serve())
        waited = 0.0
        while not self.server.started and not self.task.done() and waited < timeout:
            await asyncio.sleep(0.02)
            waited += 0.02
        if self.task.done():          # surface a bind failure rather than hang
            self.task.result()
        self.transport.engine_log.info(
            f"Control API listening on http://{self.host}:{self.bound_port} "
            f"(docs at /docs)"
        )
        return self.task

    async def stop(self) -> None:
        if self.task is None:
            return
        self.server.should_exit = True
        try:
            await self.task
        except Exception:  # pragma: no cover - shutdown races
            pass
        self.task = None
