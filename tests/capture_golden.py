"""Write the golden FIX 4.2 fixtures.

Run ONCE, from the pre-refactor code:

    .venv/bin/python tests/capture_golden.py

After the Cook 5 refactor starts these files are frozen: if one differs, the
code is wrong, not the fixture.  The script refuses to overwrite unless asked,
so it cannot be run by accident.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from golden_scenario import run_all  # noqa: E402
from orderecho_FixVersion import FIX_4_2, FIX_4_4  # noqa: E402

GOLDEN_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "golden")

#: version -> fixture directory
GOLDEN_DIRS = {
    FIX_4_2: os.path.join(GOLDEN_ROOT, "fix42"),
    FIX_4_4: os.path.join(GOLDEN_ROOT, "fix44"),
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    wanted = [arg for arg in argv if arg in GOLDEN_DIRS] or list(GOLDEN_DIRS)

    written = 0
    for version in wanted:
        directory = GOLDEN_DIRS[version]
        os.makedirs(directory, exist_ok=True)
        existing = [name for name in os.listdir(directory)
                    if name.endswith(".txt")]
        if existing and not force:
            print(f"error: {len(existing)} fixture(s) already in {directory}.",
                  file=sys.stderr)
            print("The golden files are frozen once the refactor starts; pass "
                  "--force only if you are deliberately re-baselining.",
                  file=sys.stderr)
            return 1

        for name, text in run_all(version).items():
            path = os.path.join(directory, f"{name}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"wrote {os.path.relpath(path)} "
                  f"({len(text.splitlines())} lines)")
            written += 1
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
