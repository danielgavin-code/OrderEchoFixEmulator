"""One way to build a fully isolated test config, and the guard that proves it.

Every storage path the engine can write to is listed in STORAGE_KEYS.  A test
that builds its config through `isolated_config` cannot touch the repo's own
`data/` or `logs/`; the autouse guard in conftest.py fails the run if anything
does anyway.

When a future cook adds a storage key with a default, add it here: the
`test_every_storage_key_is_isolated` test fails until it is.
"""

from __future__ import annotations

import os
from decimal import Decimal

from orderecho_Config import (
    Config,
    ControlApiConfig,
    EngineConfig,
    LoggingConfig,
    OrdersConfig,
    PriceBandConfig,
    PricingConfig,
    SessionConfig,
    SessionSpec,
    StorageConfig,
)
from orderecho_Rules import load_rules

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Directories the engine writes to that must never be the repo's own.
PROTECTED_DIRS = (
    os.path.join(REPO_ROOT, "data"),
    os.path.join(REPO_ROOT, "logs"),
)

#: (config section, attribute) for every path the engine can write to.
STORAGE_KEYS = (
    ("storage", "seqnum_dir"),
    ("storage", "evidence_dir"),
    ("storage", "msgstore_dir"),
    ("storage", "orders_dir"),
    ("logging", "log_dir"),
)

DEFAULT_RULES = [
    {"name": "nasdaq-test-reject", "match": {"symbol": "ZVZZT"},
     "behavior": "reject", "reject_code": 1, "text": "Unknown symbol"},
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "a-to-d-full", "match": {"first_letter": "A-D"},
     "behavior": "full_fill"},
    {"name": "e-to-g-partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave"},
    {"name": "default", "match": {"any": True}, "behavior": "ack_only"},
]


def isolated_config(tmp_path, *, fix_version="FIX.4.2", fix_port=0,
                    api_port=0, rules=None, delay_ms=50, band=None,
                    pricing=None, orders=None, control_api_enabled=False,
                    resend_mode="replay", engine_level="DEBUG",
                    console=False) -> Config:
    """A Config whose every writable path lives under *tmp_path*."""
    root = str(tmp_path)
    return Config(
        session=SessionConfig(
            fix_version=fix_version, sender_comp_id="ORDERECHO",
            target_comp_id="AGENT", host="127.0.0.1", port=fix_port,
            heartbeat_grace_pct=20, logout_timeout_sec=10,
            resend_mode=resend_mode,
        ),
        storage=StorageConfig(
            seqnum_dir=os.path.join(root, "data", "seqnums"),
            evidence_dir=os.path.join(root, "data", "evidence"),
            msgstore_dir=os.path.join(root, "data", "msgstore"),
            orders_dir=os.path.join(root, "data", "orders"),
        ),
        logging=LoggingConfig(
            log_dir=os.path.join(root, "logs"), fix_delimiter="|",
            engine_level=engine_level, console=console,
        ),
        orders=orders or OrdersConfig(default_delay_ms=delay_ms),
        pricing=pricing or PricingConfig(
            mode="static", static={"AAPL": Decimal("227.50")},
            static_default=Decimal("100.00"), warm_symbols=[],
        ),
        control_api=ControlApiConfig(enabled=control_api_enabled,
                                     host="127.0.0.1", port=api_port),
        price_band=band if band is not None else PriceBandConfig(enabled=False),
        rules=load_rules(rules or DEFAULT_RULES, delay_ms),
        path="<test>",
    )


def config_paths(config) -> list:
    """Every writable path in a config, for the isolation guard."""
    paths = []
    for section, attribute in STORAGE_KEYS:
        value = getattr(getattr(config, section), attribute, None)
        if value:
            paths.append(os.path.abspath(value))
    return paths


def leaks_into_repo(config) -> list:
    """Paths in *config* that point inside the repo's own data/ or logs/."""
    offenders = []
    for path in config_paths(config):
        for protected in PROTECTED_DIRS:
            if path == protected or path.startswith(protected + os.sep):
                offenders.append(path)
    return offenders


def snapshot_protected_dirs() -> dict:
    """Map every file under the repo's data/ and logs/ to its mtime+size."""
    seen = {}
    for protected in PROTECTED_DIRS:
        for root, _dirs, files in os.walk(protected):
            for name in files:
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                seen[path] = (stat.st_mtime_ns, stat.st_size)
    return seen


def describe_changes(before: dict, after: dict) -> list:
    """What changed between two snapshots, as readable lines."""
    changes = []
    for path in sorted(set(after) - set(before)):
        changes.append(f"created {os.path.relpath(path, REPO_ROOT)}")
    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            changes.append(f"modified {os.path.relpath(path, REPO_ROOT)}")
    for path in sorted(set(before) - set(after)):
        changes.append(f"deleted {os.path.relpath(path, REPO_ROOT)}")
    return changes


def _is_emulator(command: str) -> bool:
    """A Python process running orderecho_Main.py -- not merely a shell
    whose command line mentions it."""
    tokens = command.split()
    if not tokens or "python" not in os.path.basename(tokens[0]).lower():
        return False
    return any(os.path.basename(token) == "orderecho_Main.py"
               for token in tokens[1:])


def find_repo_writers(dirs=PROTECTED_DIRS, repo_root=REPO_ROOT,
                      exclude_pids=None) -> list:
    """Other processes writing to *dirs*: sorted (pid, command) pairs.

    Two ways to be found: holding a file open under one of *dirs* (a running
    engine keeps its FIX log and evidence file open), or being an emulator
    (orderecho_Main.py) whose working directory is *repo_root* (started from
    the repo with the default, relative storage paths).  Best effort:
    without `lsof` and `ps` this finds nothing, and the caller falls back to
    the generic message.
    """
    import subprocess

    exclude = {os.getpid()} | set(exclude_pids or ())

    def run(args):
        try:
            return subprocess.run(args, capture_output=True, text=True,
                                  timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    commands = {}
    for line in run(["ps", "-ax", "-o", "pid=,command="]).splitlines():
        pid_text, _sep, command = line.strip().partition(" ")
        if pid_text.isdigit():
            commands[int(pid_text)] = command.strip()

    pids = set()
    existing = [d for d in dirs if os.path.isdir(d)]
    if existing:
        args = ["lsof", "-n", "-P", "-F", "p"]
        for directory in existing:
            args += ["+D", directory]
        pids |= {int(line[1:]) for line in run(args).splitlines()
                 if line.startswith("p") and line[1:].isdigit()}

    root = os.path.realpath(repo_root)
    for pid, command in commands.items():
        if not _is_emulator(command):
            continue
        cwd = None
        if os.path.isdir(f"/proc/{pid}"):
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = None
        else:
            for out in run(["lsof", "-n", "-P", "-a", "-p", str(pid),
                            "-d", "cwd", "-F", "n"]).splitlines():
                if out.startswith("n"):
                    cwd = out[1:]
        if cwd and os.path.realpath(cwd) == root:
            pids.add(pid)

    return sorted((pid, commands.get(pid, "?")) for pid in pids - exclude)


def guard_failure_message(changes: list, writers: list) -> str:
    """What the repo guard says when data/ or logs/ changed during a run."""
    listed = "\n  ".join(changes)
    if writers:
        lines = []
        for pid, command in writers:
            if _is_emulator(command):
                lines.append(f"a running emulator, pid {pid}, is writing to "
                             f"repo data/logs; stop it before running tests")
            else:
                lines.append(f"a running process, pid {pid}, has files open "
                             f"in repo data/logs; stop it before running "
                             f"tests")
            lines.append(f"    ({command})")
        return ("\n".join(lines)
                + "\nThese changes are most likely its doing, not a test's:"
                + f"\n  {listed}")
    return ("tests wrote to the repo's own runtime directories:\n  "
            f"{listed}\n"
            "Build the config with tests/isolation.isolated_config so every "
            "storage path lives under tmp_path (spec 3.5).")


def isolated_multi_config(tmp_path, sessions, *, api_port=0,
                          default_session=None, control_api_enabled=False,
                          pricing=None, host="127.0.0.1", delay_ms=50):
    """A multi-session Config with every storage path under *tmp_path*.

    `sessions` is a list of dicts: id, fix_version, sender, target, port, and
    optionally rules / band / orders.
    """
    base = isolated_config(tmp_path, api_port=api_port,
                           control_api_enabled=control_api_enabled,
                           pricing=pricing, delay_ms=delay_ms)
    specs = []
    for entry in sessions:
        spec_rules = entry.get("rules", DEFAULT_RULES)
        specs.append(SessionSpec(
            id=entry["id"],
            session=SessionConfig(
                fix_version=entry.get("fix_version", "FIX.4.2"),
                sender_comp_id=entry.get("sender", "ORDERECHO"),
                target_comp_id=entry.get("target", "AGENT"),
                host=host,
                port=entry.get("port", 0),
                heartbeat_grace_pct=20,
                logout_timeout_sec=entry.get("logout_timeout_sec", 10),
                resend_mode=entry.get("resend_mode", "replay"),
            ),
            orders=entry.get("orders") or OrdersConfig(
                default_delay_ms=entry.get("delay_ms", delay_ms)
            ),
            rules=load_rules(spec_rules,
                             entry.get("delay_ms", delay_ms))
            if spec_rules is not None else None,
            price_band=entry.get("band") or PriceBandConfig(enabled=False),
        ))
    base.sessions = specs
    base.engine = EngineConfig(host=host, fix_port=specs[0].port,
                               default_session=default_session)
    # The unscoped views point at whichever session the API would act on.
    chosen = base.default_session_spec or specs[0]
    base.session = chosen.session
    base.orders = chosen.orders
    base.rules = chosen.rules
    base.price_band = chosen.price_band
    return base
