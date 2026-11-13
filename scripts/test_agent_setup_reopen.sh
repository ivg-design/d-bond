#!/bin/bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

swift build --package-path "$root" --product dbond-probe >/dev/null
products=$(swift build --package-path "$root" --show-bin-path)
swiftc -parse-as-library -I "$products" \
  "$root/Sources/DBond/Brand.swift" \
  "$root/Sources/DBond/AgentInstallState.swift" \
  "$root/Sources/DBond/AgentSetup.swift" \
  "$root/scripts/fixtures/agent_setup/main.swift" \
  "$products/DBondCore.o" -o "$scratch/agent-setup-fixture"
"$scratch/agent-setup-fixture" "$scratch/ledger"
