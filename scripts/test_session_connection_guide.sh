#!/bin/bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d "$HOME/Library/Application Support/D-Bond-Session-Guide-Fixture.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

swift build --package-path "$root" --product dbond-probe >/dev/null
products=$(swift build --package-path "$root" --show-bin-path)
swiftc -parse-as-library -I "$products" \
  "$root/Sources/DBond/Brand.swift" \
  "$root/Sources/DBond/RuleMarkdown.swift" \
  "$root/Sources/DBond/RuleEditor.swift" \
  "$root/Sources/DBond/EventLogWindow.swift" \
  "$root/Sources/DBond/SessionConnectionGuide.swift" \
  "$root/Sources/DBond/ConnectionsWindow.swift" \
  "$root/scripts/fixtures/session_connection_guide/main.swift" \
  "$products/DBondCore.o" -o "$scratch/session-guide-fixture"
DBOND_FIXTURE_ICON="$root/Assets/d-bond-app-icon.png" "$scratch/session-guide-fixture" "$scratch"
