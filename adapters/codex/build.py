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
    server = PLUGIN / "server"
    shutil.copy2(ROOT / "adapters/direct/server/mcp.py", server / "direct.py")
    for module in ("owner.py", "dbond.py", "wake_registration.py", "codex_registration.py"):
        shutil.copy2(ROOT / "adapters/cli" / module, server / module)
    (PLUGIN / "server/launch.sh").chmod(0o755)
    print(PLUGIN)


if __name__ == "__main__":
    main()
