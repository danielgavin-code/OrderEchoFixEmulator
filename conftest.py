"""Makes the repo root importable by the test suite (pytest prepends the
directory holding the root conftest.py to sys.path), and guards the repo's own
runtime directories against leaky tests.

Spec 3.5: a test that writes to the repo's data/ or logs/ fails the run. Tests
must build their config through tests/isolation.isolated_config, which puts
every storage path under tmp_path.  A running emulator writing to the repo
is reported as such, with its pid.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tests"))

from isolation import (  # noqa: E402
    describe_changes,
    find_repo_writers,
    guard_failure_message,
    snapshot_protected_dirs,
)


@pytest.fixture(scope="session", autouse=True)
def repo_runtime_dirs_are_untouched():
    """Fail the run if anything wrote to the repo's own data/ or logs/.

    When the writer is a running emulator rather than a test, say so.
    """
    before = snapshot_protected_dirs()
    yield
    changes = describe_changes(before, snapshot_protected_dirs())
    if changes:
        pytest.fail(guard_failure_message(changes, find_repo_writers()),
                    pytrace=False)
