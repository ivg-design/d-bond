#!/usr/bin/env python3
"""Bundle the Claude adapter and current shared D-Bond CLI without credentials."""
import json
import pathlib
import shutil
import subprocess
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUTPUT = ROOT / "dist" / "d-bond-claude.mcpb"

def main():
    manifest = json.loads((HERE / "manifest.json").read_text())
    assert manifest["server"]["entry_point"] == "server/d-bond-launcher"
    OUTPUT.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        launcher = pathlib.Path(folder) / "d-bond-launcher"
        subprocess.run(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-arch", "arm64", "-arch", "x86_64",
                        "-mmacosx-version-min=11.0", str(HERE / "server/launcher.c"), "-o", str(launcher)], check=True)
        with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, src in [("manifest.json", HERE / "manifest.json"),
                              ("LICENSE", ROOT / "LICENSE"),
                              ("server/d-bond-launcher", launcher),
                              ("server/launch.sh", HERE / "server/launch.sh"),
                              ("server/mcp.py", HERE / "server/mcp.py"),
                              ("server/dbond.py", ROOT / "adapters/cli/dbond.py"),
                              *[(f"server/{name}", ROOT / "adapters/cli" / name)
                                for name in ("owner.py", "codex_registration.py", "claude_registration.py", "wake_registration.py")]]:
                bundle.write(src, name)
    plugin_server = HERE / "plugin" / "server"
    plugin_server.mkdir(exist_ok=True)
    shutil.copy2(HERE / "server/mcp.py", plugin_server / "mcp.py")
    shutil.copy2(HERE / "server/launch.sh", plugin_server / "launch.sh")
    shutil.copy2(ROOT / "adapters/cli/dbond.py", plugin_server / "dbond.py")
    for name in ("owner.py", "codex_registration.py", "claude_registration.py", "wake_registration.py"):
        shutil.copy2(ROOT / "adapters/cli" / name, plugin_server / name)
    hook = HERE / "plugin/hooks/session_start.py"
    hook.parent.mkdir(exist_ok=True)
    shutil.copy2(HERE / "session-hooks/session_start.py", hook)
    print(OUTPUT)

if __name__ == "__main__":
    main()
