#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
swift build --package-path "$root" --product dbond-probe >/dev/null
products="$(swift build --package-path "$root" --show-bin-path)"
swiftc -parse-as-library -I "$products" \
  "$root/Sources/DBond/Brand.swift" \
  "$root/Sources/DBond/Login.swift" \
  "$root/Sources/DBond/SettingsWindow.swift" \
  "$root/scripts/fixtures/dot_connector/main.swift" \
  "$products/DBondCore.o" -o "$scratch/dot-connector-settings"
app="$scratch/Downloaded D-Bond Fixture.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
mv "$scratch/dot-connector-settings" "$app/Contents/MacOS/Fixture"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleExecutable</key><string>Fixture</string><key>CFBundleIdentifier</key><string>design.ivg.dbond.fixture.dot-settings</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>
PLIST
DOT_CONNECTOR_QA_DIR="${DOT_CONNECTOR_QA_DIR:-$scratch}" "$app/Contents/MacOS/Fixture"
