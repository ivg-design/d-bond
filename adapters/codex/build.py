#!/usr/bin/env python3
"""Stage the self-contained local Codex plugin without credentials."""
from pathlib import Path
import shutil
import json

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PLUGIN = HERE / "plugins/d-bond-local-control"


def main():
    versions = [json.loads((PLUGIN / file).read_text())["version"]
                for file in ("plugin.json", ".codex-plugin/plugin.json")]
    if versions[0] != versions[1]:
        raise ValueError("Codex plugin manifests disagree about the version")
    shutil.copy2(ROOT / "adapters/cli/owner.py", PLUGIN / "server/owner.py")
    (PLUGIN / "server/launch.sh").chmod(0o755)
    print(PLUGIN)


if __name__ == "__main__":
    main()
