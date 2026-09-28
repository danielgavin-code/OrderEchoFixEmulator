"""OrderEchoFixEmulator entry point.

    python orderecho_Main.py [--config PATH] [--reset-seqnums]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from orderecho_Clock import SystemClock
from orderecho_Config import ConfigError, load_config
from orderecho_ControlApi import ControlApiServer
from orderecho_MessageStore import MessageStore
from orderecho_Pricing import warm_up
from orderecho_SeqStore import FileSeqStore
from orderecho_Transport import Transport
from orderecho_Version import ORDERECHO_BUILD, ORDERECHO_VERSION
DEFAULT_CONFIG = os.path.join("config", "orderecho.yaml")


def build_parser() -> argparse.ArgumentParser:
    """The command line, on its own, so the docs can be checked against it."""
    parser = argparse.ArgumentParser(
        prog="orderecho_Main.py",
        description="OrderEchoFixEmulator - local FIX acceptor",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"path to the YAML config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--reset-seqnums", action="store_true",
                        help="reset stored sequence numbers to 1/1 before starting")
    return parser


def parse_args(argv=None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def banner(config, transport: Transport, control_api=None) -> str:
    plural = "" if len(config.sessions) == 1 else "s"
    lines = [
        f"OrderEchoFixEmulator {ORDERECHO_VERSION} ({ORDERECHO_BUILD}) "
        f"- {len(config.sessions)} FIX session{plural}",
        f"  config         : {config.path}",
        f"  evidence file  : {transport.evidence.path}",
        f"  fix log        : {os.path.join(config.logging.log_dir, 'fix')}",
        f"  engine log     : {os.path.join(config.logging.log_dir, 'engine')}",
        "",
        f"  {'session':<16} {'version':<8} {'route':<26} {'port':<6} "
        f"{'rules':<6} band",
        f"  {'-' * 16} {'-' * 8} {'-' * 26} {'-' * 6} {'-' * 6} ----",
    ]
    for spec in config.sessions:
        route = f"{spec.sender_comp_id} -> {spec.target_comp_id}"
        rules = len(spec.rules) if spec.rules is not None else 0
        band = (f"{spec.price_band.pct:g}%" if spec.price_band
                and spec.price_band.enabled else "off")
        lines.append(
            f"  {spec.id:<16} {spec.fix_version:<8} {route:<26} "
            f"{spec.port:<6} {rules:<6} {band}"
        )
    lines.append("")
    if control_api is not None:
        base = f"http://{control_api.host}:{control_api.bound_port}"
        lines.append(f"  control api    : {base}  (docs at /docs)")
        lines.append(f"  guide          : {base}/guide")
        lines.append(f"  log viewer     : {base}/viewer")
    else:
        lines.append("  control api    : disabled")
    lines.append("  Ctrl+C to shut down.")
    return "\n".join(lines)


async def run(config) -> int:
    clock = SystemClock()
    transport = Transport(config, clock=clock)
    try:
        await transport.start()
    except OSError as exc:
        print(f"error: cannot listen on {config.session.host}:"
              f"{config.session.port}: {exc}", file=sys.stderr)
        transport.close_logs()
        return 1

    control_api = None
    if config.control_api.enabled:
        try:
            control_api = ControlApiServer(config, transport)
            await control_api.start()
        except Exception as exc:
            print(f"error: could not start the control API on "
                  f"{config.control_api.host}:{config.control_api.port}: {exc}",
                  file=sys.stderr)
            transport.engine_log.exception("Control API failed to start")
            await transport.stop()
            transport.close_logs()
            return 1

    # Prime the live price cache off the critical path; startup never waits.
    if config.pricing.mode == "live":
        warm_up(transport.price_source, config.pricing.warm_symbols,
                engine_log=transport.engine_log)

    print(banner(config, transport, control_api), flush=True)
    listed = "; ".join(
        f"{spec.id}={spec.fix_version}@{spec.host_port}"
        for spec in config.sessions
    )
    startup = (
        f"version={ORDERECHO_VERSION} build={ORDERECHO_BUILD} "
        f"config={config.path} sessions=[{listed}] "
        f"evidence={transport.evidence.path}"
    )
    transport.engine_log.info(f"Startup: {startup}")
    transport.evidence.event("startup", startup)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers_installed = []
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
            handlers_installed.append(sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    serving = asyncio.create_task(transport.serve_forever())
    try:
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {serving, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        waiter.cancel()
        if serving in done:
            serving.result()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for sig in handlers_installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
        print("\nShutting down...", flush=True)
        serving.cancel()
        if control_api is not None:
            try:
                await control_api.stop()
            except Exception:
                transport.engine_log.exception("Error stopping the control API")
        try:
            await transport.shutdown_gracefully("OrderEcho shutting down")
        except Exception:
            transport.engine_log.exception("Error during shutdown")
        transport.engine_log.info("Shutdown complete")
        transport.close_logs()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.reset_seqnums:
        # Every session, not just the first: each has its own files.
        for spec in config.sessions:
            store = FileSeqStore(
                config.storage.seqnum_dir,
                spec.sender_comp_id, spec.target_comp_id, name=spec.id,
            )
            store.reset()
            print(f"Sequence numbers reset to 1/1 ({store.path})", flush=True)
            # Seq 1 is about to mean a different message, so the stored
            # outbound messages can no longer answer a ResendRequest.
            messages = MessageStore(
                config.storage.msgstore_dir,
                spec.sender_comp_id, spec.target_comp_id, name=spec.id,
            )
            archived = messages.archive()
            messages.close()
            if archived:
                print(f"Outbound message store archived to {archived}",
                      flush=True)

    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        # asyncio.run re-raises the KeyboardInterrupt after cancelling run();
        # the finally block above has already logged out and stopped.
        return 0


if __name__ == "__main__":
    sys.exit(main())
