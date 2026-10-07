"""The isolation guard itself (spec 3.5, test 9.12)."""

import os
import subprocess
import sys
import textwrap

from isolation import (
    PROTECTED_DIRS,
    REPO_ROOT,
    STORAGE_KEYS,
    config_paths,
    describe_changes,
    find_repo_writers,
    guard_failure_message,
    isolated_config,
    leaks_into_repo,
    snapshot_protected_dirs,
)
from orderecho_Config import Config


def test_isolated_config_keeps_every_path_under_tmp_path(tmp_path):
    config = isolated_config(tmp_path)
    paths = config_paths(config)
    assert paths, "no storage paths were collected"
    for path in paths:
        assert path.startswith(str(tmp_path)), path
    assert leaks_into_repo(config) == []


def test_every_storage_key_on_the_config_is_covered():
    """A new storage key with a default must be added to STORAGE_KEYS."""
    covered = {(section, attribute) for section, attribute in STORAGE_KEYS}
    found = set()
    for section in ("storage", "logging"):
        section_type = Config.__dataclass_fields__[section].type
        annotations = getattr(section_type, "__annotations__", None)
        if annotations is None:            # the field type is a string
            from orderecho_Config import LoggingConfig, StorageConfig
            annotations = {"storage": StorageConfig,
                           "logging": LoggingConfig}[section].__annotations__
        for attribute in annotations:
            if attribute.endswith("_dir"):
                found.add((section, attribute))
    missing = found - covered
    assert not missing, (
        f"storage keys not covered by tests/isolation.py: {sorted(missing)}"
    )


def test_leaks_into_repo_spots_a_bad_config(tmp_path):
    config = isolated_config(tmp_path)
    config.storage.evidence_dir = os.path.join(REPO_ROOT, "data", "evidence")
    offenders = leaks_into_repo(config)
    assert len(offenders) == 1
    assert offenders[0].endswith(os.path.join("data", "evidence"))


def test_leaks_into_repo_spots_the_protected_dirs_themselves(tmp_path):
    config = isolated_config(tmp_path)
    config.logging.log_dir = os.path.join(REPO_ROOT, "logs")
    assert leaks_into_repo(config)


def test_describe_changes_reports_creates_modifies_and_deletes():
    before = {"/a": (1, 1), "/b": (1, 1)}
    after = {"/b": (2, 9), "/c": (1, 1)}
    changes = describe_changes(before, after)
    assert any(line.startswith("created") and line.endswith("c")
               for line in changes)
    assert any(line.startswith("modified") for line in changes)
    assert any(line.startswith("deleted") for line in changes)


def test_snapshot_covers_both_protected_directories():
    # Only that it looks at them; they may legitimately be empty.
    assert len(PROTECTED_DIRS) == 2
    assert all(os.path.basename(path) in ("data", "logs")
               for path in PROTECTED_DIRS)
    snapshot_protected_dirs()          # must not raise when they are absent


def _guard_copy(tmp_path):
    """A throwaway repo holding the real guard: conftest.py and
    tests/isolation.py copied verbatim, the real code on PYTHONPATH.

    The guard runs exactly as it does in the repo, but its data/ is under
    tmp_path, so a probe never writes to (or deletes from) the real repo --
    which in a synced folder could come back on a later run.
    """
    import shutil

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "data").mkdir()
    shutil.copy(os.path.join(REPO_ROOT, "conftest.py"), repo / "conftest.py")
    shutil.copy(os.path.join(REPO_ROOT, "tests", "isolation.py"),
                repo / "tests" / "isolation.py")
    env = dict(os.environ, PYTHONPATH=REPO_ROOT, PYTHONDONTWRITEBYTECODE="1")
    return repo, env


def test_the_guard_fails_a_run_that_writes_to_the_repo(tmp_path):
    """Run pytest in a subprocess on a deliberately leaky test.

    The probe lives inside tests/ of a copy of the repo's guard, so the
    repo's own conftest -- and so the guard -- actually applies to it, which
    is the thing being tested.
    """
    repo, env = _guard_copy(tmp_path)
    probe_dir = repo / "data" / "guard_probe"
    (repo / "tests" / "test_guard_probe_TEMP.py").write_text(textwrap.dedent(f'''
        import os

        def test_writes_into_the_repo():
            target = {str(probe_dir)!r}
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "leak.txt"), "w") as handle:
                handle.write("this should fail the run\\n")
    '''))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_guard_probe_TEMP.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=180,
    )
    output = result.stdout + result.stderr
    # The test itself passes; the session guard is what fails the run.
    assert result.returncode != 0, output
    assert "wrote to the repo's own runtime directories" in output, output
    assert "guard_probe" in output, output
    assert "isolated_config" in output, output
    # And the real repo was never touched.
    assert not os.path.exists(os.path.join(REPO_ROOT, "data", "guard_probe"))


# ------------------------------------- a running emulator, not a test

HOLDER = textwrap.dedent("""
    import sys, time
    handle = open(sys.argv[1], "a")
    handle.write("held\\n"); handle.flush()
    print("ready", flush=True)
    time.sleep(60)
""")


def _start_holder(path, cwd=None, script=None):
    process = subprocess.Popen(
        [sys.executable, script or "-c", *([] if script else [HOLDER]), path],
        cwd=cwd, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"
    return process


def _stop(process):
    process.kill()
    process.wait(timeout=10)


def test_a_process_holding_a_file_open_is_found(tmp_path):
    holder = _start_holder(str(tmp_path / "fix.log"))
    try:
        writers = dict(find_repo_writers(dirs=[str(tmp_path)],
                                         repo_root=str(tmp_path / "nowhere")))
        assert holder.pid in writers
        assert os.getpid() not in writers
    finally:
        _stop(holder)
    assert holder.pid not in dict(find_repo_writers(
        dirs=[str(tmp_path)], repo_root=str(tmp_path / "nowhere")))


def test_an_engine_started_in_the_repo_is_found_by_its_cwd(tmp_path):
    """Between writes an engine may hold nothing open under data/: an
    orderecho_Main.py running from the repo is a writer anyway."""
    fake_main = tmp_path / "orderecho_Main.py"
    fake_main.write_text(HOLDER)
    holder = _start_holder(str(tmp_path / "elsewhere.log"), cwd=str(tmp_path),
                           script=str(fake_main))
    try:
        writers = dict(find_repo_writers(dirs=[], repo_root=str(tmp_path)))
        assert holder.pid in writers
        assert "orderecho_Main.py" in writers[holder.pid]
        assert holder.pid not in dict(find_repo_writers(
            dirs=[], repo_root=str(tmp_path / "other")))
    finally:
        _stop(holder)


def test_a_shell_that_only_mentions_the_engine_is_not_an_emulator(tmp_path):
    """A shell whose command line merely contains 'orderecho_Main.py' (as a
    launcher script's does) and sits in the repo is not reported."""
    shell = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 # python orderecho_Main.py"],
        cwd=str(tmp_path))
    try:
        assert shell.pid not in dict(find_repo_writers(
            dirs=[], repo_root=str(tmp_path)))
    finally:
        _stop(shell)


def test_a_process_that_is_not_an_emulator_is_named_neutrally():
    text = guard_failure_message(["modified logs/x.log"], [(77, "tail -f x")])
    assert ("a running process, pid 77, has files open in repo data/logs; "
            "stop it before running tests") in text
    assert "emulator" not in text


def test_the_message_names_the_running_emulator():
    changes = ["modified logs/fix/agent42_20261004.log"]
    text = guard_failure_message(
        changes, [(14395, "/usr/bin/python3 orderecho_Main.py --config "
                          "config/orderecho_multi.yaml")])
    assert ("a running emulator, pid 14395, is writing to repo data/logs; "
            "stop it before running tests") in text
    assert "logs/fix/agent42_20261004.log" in text
    assert "isolated_config" not in text
    generic = guard_failure_message(changes, [])
    assert "wrote to the repo's own runtime directories" in generic
    assert "isolated_config" in generic


def test_the_guard_names_a_running_writer_instead_of_blaming_tests(tmp_path):
    """End to end: while a pytest run is going, an emulator writes to the
    repo's data/ and keeps the file open.

    Runs in a throwaway copy of the repo's guard (conftest.py and
    tests/isolation.py under tmp_path), so nothing touches the real data/ --
    which matters in a synced folder, where a deleted file can come back.
    """
    repo, env = _guard_copy(tmp_path)
    probe_dir = repo / "data" / "live"
    pid_file = tmp_path / "writer.pid"
    (repo / "tests" / "test_probe.py").write_text(textwrap.dedent(f'''
        import os, subprocess, sys

        def test_meanwhile_an_emulator_is_writing():
            # Not this test's write: a separate, long-lived engine's.
            os.makedirs({str(probe_dir)!r}, exist_ok=True)
            main = os.path.join({str(probe_dir)!r}, "orderecho_Main.py")
            with open(main, "w") as handle:
                handle.write({HOLDER!r})
            writer = subprocess.Popen(
                [sys.executable, main,
                 os.path.join({str(probe_dir)!r}, "fix.log")],
                stdout=subprocess.PIPE, text=True, start_new_session=True)
            assert writer.stdout.readline().strip() == "ready"
            with open({str(pid_file)!r}, "w") as handle:
                handle.write(str(writer.pid))
    '''))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_probe.py", "-q",
             "-p", "no:cacheprovider"],
            cwd=str(repo), env=env, capture_output=True, text=True,
            timeout=180,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        writer_pid = int(pid_file.read_text())
        assert (f"a running emulator, pid {writer_pid}, is writing to repo "
                "data/logs; stop it before running tests") in output, output
        assert "data/live/fix.log" in output, output
        assert "isolated_config" not in output, output
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ValueError, OSError):
                pass
