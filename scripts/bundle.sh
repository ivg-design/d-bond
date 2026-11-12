#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="D-Bond"
APP_DIR="$ROOT/dist/$APP_NAME.app"
BIN_DIR="$APP_DIR/Contents/MacOS"
RES_DIR="$APP_DIR/Contents/Resources"
BUNDLE_ID="${DBOND_BUNDLE_ID:-design.ivg.dbond}"
[[ "$BUNDLE_ID" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || { echo "Invalid bundle identifier" >&2; exit 1; }

cd "$ROOT"
swift build -c release --product DBond
mkdir -p "$BIN_DIR" "$RES_DIR"
product_dir="$(swift build -c release --show-bin-path)"
cp "$product_dir/DBond" "$BIN_DIR/DBond"
cp "$ROOT/scripts/native_link.py" "$RES_DIR/"
install -m 755 "$ROOT/adapters/cli/dbond.py" "$RES_DIR/dbond"
install -m 644 "$ROOT/adapters/cli/owner.py" "$RES_DIR/owner.py"
icon_dir="$(mktemp -d)"
trap 'rm -rf "$icon_dir"' EXIT
swift "$ROOT/scripts/make_app_icon.swift" "$ROOT/Assets/d-bond-app-icon.png" "$icon_dir/D-Bond.iconset"
iconutil -c icns "$icon_dir/D-Bond.iconset" -o "$RES_DIR/D-Bond.icns"
install -m 644 "$ROOT/Assets/d-bond-menu-icon.svg" "$RES_DIR/d-bond-menu-icon.svg"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>DBond</string>
  <key>CFBundleIdentifier</key><string>design.ivg.dbond</string>
  <key>CFBundleIconFile</key><string>D-Bond.icns</string>
  <key>CFBundleName</key><string>D-Bond</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$APP_DIR/Contents/Info.plist"

codesign --force --deep --sign - "$APP_DIR"
echo "Created ad hoc signed app: $APP_DIR"
