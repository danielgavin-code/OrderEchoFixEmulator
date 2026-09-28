"""Makes the repo root importable by the test suite (pytest prepends the
directory holding the root conftest.py to sys.path), and guards the repo's own
runtime directories against leaky tests.

Spec 3.5: a test that writes to the repo's data/ or logs/ fails the run. Tests
must build their config through tests/isolation.isolated_config, which puts
every storage path under tmp_path.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tests"))

from isolation import describe_changes, snapshot_protected_dirs  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def repo_runtime_dirs_are_untouched():
    """Fail the run if anything wrote to the repo's own data/ or logs/."""
    before = snapshot_protected_dirs()
    yield
    changes = describe_changes(before, snapshot_protected_dirs())
    if changes:
        listed = "\n  ".join(changes)
        pytest.fail(
            "tests wrote to the repo's own runtime directories:\n  "
            f"{listed}\n"
            "Build the config with tests/isolation.isolated_config so every "
            "storage path lives under tmp_path (spec 3.5).",
            pytrace=False,
        )
