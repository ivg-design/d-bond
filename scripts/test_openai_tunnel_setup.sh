#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
swift build --package-path "$root" --product dbond-probe >/dev/null
products="$(swift build --package-path "$root" --show-bin-path)"
swiftc -parse-as-library -I "$products" "$root/Sources/DBond/Brand.swift" \
  "$root/Sources/DBond/OpenAITunnelSetupWindow.swift" \
  "$root/scripts/fixtures/openai_tunnel_setup/main.swift" \
  "$products/DBondCore.o" -o "$scratch/private-tunnel-ui"
DBOND_FIXTURE_ICON="$root/Assets/d-bond-app-icon.png" "$scratch/private-tunnel-ui" "$scratch"
