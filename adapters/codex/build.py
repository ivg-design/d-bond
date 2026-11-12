#!/usr/bin/env python3
"""Stage the self-contained local Codex plugin without credentials."""
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PLUGIN = HERE / "plugins/d-bond-local-control"


def main():
    shutil.copy2(ROOT / "adapters/cli/owner.py", PLUGIN / "server/owner.py")
    (PLUGIN / "server/launch.sh").chmod(0o755)
    print(PLUGIN)


if __name__ == "__main__":
    main()
