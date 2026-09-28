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


def test_the_guard_fails_a_run_that_writes_to_the_repo():
    """Run pytest in a subprocess on a deliberately leaky test.

    The probe lives inside tests/ so that the repo's own conftest -- and so
    the guard -- actually applies to it, which is the thing being tested.
    """
    tests_dir = os.path.join(REPO_ROOT, "tests")
    leaky = os.path.join(tests_dir, "test_guard_probe_TEMP.py")
    probe_dir = os.path.join(REPO_ROOT, "data", "guard_probe")
    with open(leaky, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(f'''
            import os

            def test_writes_into_the_repo():
                target = {probe_dir!r}
                os.makedirs(target, exist_ok=True)
                with open(os.path.join(target, "leak.txt"), "w") as handle:
                    handle.write("this should fail the run\\n")
        '''))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", leaky, "-q",
             "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        output = result.stdout + result.stderr
        # The test itself passes; the session guard is what fails the run.
        assert result.returncode != 0, output
        assert "wrote to the repo's own runtime directories" in output, output
        assert "guard_probe" in output, output
        assert "isolated_config" in output, output
    finally:
        if os.path.exists(leaky):
            os.remove(leaky)
        leak = os.path.join(probe_dir, "leak.txt")
        if os.path.exists(leak):
            os.remove(leak)
        if os.path.isdir(probe_dir):
            os.rmdir(probe_dir)
