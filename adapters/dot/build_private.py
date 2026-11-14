#!/usr/bin/env python3
"""Build a private Dot plugin ZIP from an operator-owned app binding.

The output is a local artifact. It must not be committed or published.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SKILL = HERE / "skills/d-bond/SKILL.md"


def identity(binding: object) -> dict:
    if not isinstance(binding, dict) or set(binding) != {"apps"}:
        raise ValueError("binding must contain only an apps object")
    apps = binding["apps"]
    if not isinstance(apps, dict) or len(apps) != 1:
        raise ValueError("binding must identify exactly one connected app")
    (key, value), = apps.items()
    if not isinstance(value, dict) or set(value) != {"id"}:
        raise ValueError("app binding may contain only its id")
    match = re.fullmatch(r"dev-([0-9a-f]{16,32})", key)
    if not match or value["id"] != "asdk_app_" + match.group(1):
        raise ValueError("app identity fields do not match")
    return binding


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=pathlib.Path,
                        help="operator-owned local .app.json with one app identity")
    parser.add_argument("--output", required=True, type=pathlib.Path,
                        help="private ZIP outside this source checkout")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.suffix.lower() != ".zip" or output == args.binding.resolve():
        parser.error("output must be a distinct .zip file")
    checkouts = {ROOT, ROOT.with_name("D-Bond"), ROOT.with_name("D-Bond-Public")}
    if any(output == checkout or checkout in output.parents for checkout in checkouts):
        parser.error("write the private ZIP outside both source checkouts")
    if output.exists():
        parser.error("output already exists; choose a new path")
    binding = identity(json.loads(args.binding.read_text()))
    skill = SKILL.read_text()
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "d-bond", "version": "0.1.0",
        "description": "Connect Dot to the D-Bond host on your Mac.",
        "extensions": {"com.openai": {"apps": "./.app.json", "interface": {
            "displayName": "D-Bond for Dot", "shortDescription": "Message paired services",
            "longDescription": "Exchange messages with paired services using your connected D-Bond MCP app.",
            "developerName": "Local operator", "category": "Other", "capabilities": [],
            "defaultPrompt": "Help me communicate with a paired D-Bond service."}}},
    }
    codex = {"name": "d-bond", "version": "0.1.0", "description": manifest["description"],
             "apps": "./.app.json", "interface": manifest["extensions"]["com.openai"]["interface"],
             "skills": "./skills"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in {
            "plugin.json": json.dumps(manifest, indent=2) + "\n",
            ".codex-plugin/plugin.json": json.dumps(codex, indent=2) + "\n",
            ".app.json": json.dumps(binding, indent=2) + "\n",
            "skills/d-bond/SKILL.md": skill,
        }.items():
            archive.writestr(name, data)
    print(output)


if __name__ == "__main__":
    main()
