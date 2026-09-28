"""The FIX 4.2 rendering must not move (spec 6).

These fixtures were captured from the Cook 4 code before any of the Cook 5
refactor was written.  They are frozen: a difference means the refactor changed
4.2 output, which it must not.
"""

import os

import pytest

from golden_scenario import SCENARIOS
from orderecho_FixVersion import FIX_4_2, FIX_4_4

GOLDEN_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "golden")
GOLDEN_DIRS = {
    FIX_4_2: os.path.join(GOLDEN_ROOT, "fix42"),
    FIX_4_4: os.path.join(GOLDEN_ROOT, "fix44"),
}
VERSIONS = sorted(GOLDEN_DIRS)

#: Kept for the checks that only make sense for 4.2.
GOLDEN_DIR = GOLDEN_DIRS[FIX_4_2]


def golden_path(name: str, version: str = FIX_4_2) -> str:
    return os.path.join(GOLDEN_DIRS[version], f"{name}.txt")


@pytest.mark.parametrize("version", VERSIONS)
def test_every_scenario_has_a_fixture(version):
    on_disk = {name[:-4] for name in os.listdir(GOLDEN_DIRS[version])
               if name.endswith(".txt")}
    assert on_disk == set(SCENARIOS)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_rendering_is_unchanged(name, version):
    with open(golden_path(name, version), encoding="utf-8") as handle:
        expected = handle.read()
    actual = SCENARIOS[name](version)

    if actual != expected:                      # a readable first difference
        expected_lines = expected.splitlines()
        actual_lines = actual.splitlines()
        for index, (want, got) in enumerate(zip(expected_lines, actual_lines)):
            assert got == want, (
                f"{version} {name}.txt line {index + 1} changed:\n"
                f"  golden: {want}\n"
                f"  now   : {got}"
            )
        assert len(actual_lines) == len(expected_lines), (
            f"{version} {name}.txt changed length: golden "
            f"{len(expected_lines)} lines, now {len(actual_lines)}"
        )
    assert actual == expected


@pytest.mark.parametrize("version", VERSIONS)
def test_fixtures_are_not_empty(version):
    for name in SCENARIOS:
        with open(golden_path(name, version), encoding="utf-8") as handle:
            text = handle.read()
        assert text.strip(), f"{name}.txt is empty"
        # Every scenario must record real traffic; session_rejects produces
        # only session-level Rejects, which are REJECT lines.
        assert "SEND " in text or "REJECT " in text, \
            f"{name}.txt records no action"


def test_golden_42_output_always_carries_exec_trans_type():
    """4.2 has tag 20 on every ExecutionReport; 4.4 must not."""
    for name in SCENARIOS:
        with open(golden_path(name, FIX_4_2), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("SEND 8 "):
                    assert "|20=0|" in line, f"{name}.txt: {line.strip()}"


def test_golden_44_output_never_carries_exec_trans_type():
    for name in SCENARIOS:
        with open(golden_path(name, FIX_4_4), encoding="utf-8") as handle:
            for line in handle:
                assert "|20=" not in line, f"{name}.txt: {line.strip()}"


def test_the_two_versions_are_not_accidentally_identical():
    """If these matched, the fixtures would be proving nothing."""
    differing = [name for name in SCENARIOS
                 if open(golden_path(name, FIX_4_2), encoding="utf-8").read()
                 != open(golden_path(name, FIX_4_4), encoding="utf-8").read()]
    assert len(differing) == len(SCENARIOS)
