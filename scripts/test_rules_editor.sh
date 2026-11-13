#!/bin/bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

swift build --package-path "$root" --product dbond-probe >/dev/null
products=$(swift build --package-path "$root" --show-bin-path)
swiftc -parse-as-library -I "$products" \
  "$root/Sources/DBond/Brand.swift" \
  "$root/Sources/DBond/RuleMarkdown.swift" \
  "$root/Sources/DBond/RuleEditor.swift" \
  "$root/scripts/fixtures/rules_editor/main.swift" \
  "$products/DBondCore.o" -o "$scratch/rules-editor-fixture"

RULES_EDITOR_QA_DIR="${RULES_EDITOR_QA_DIR:-$scratch}" "$scratch/rules-editor-fixture"
python3 "$root/scripts/test_owner_sop.py"
