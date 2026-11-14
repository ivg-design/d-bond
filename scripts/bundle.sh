#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="D-Bond"
mkdir -p "$ROOT/dist"
staging_dir="$(mktemp -d "$ROOT/dist/.bundle.XXXXXX")"
icon_dir="$(mktemp -d)"
trap 'rm -rf "$staging_dir" "$icon_dir"' EXIT
APP_DIR="$staging_dir/$APP_NAME.app"
BIN_DIR="$APP_DIR/Contents/MacOS"
RES_DIR="$APP_DIR/Contents/Resources"
BUNDLE_ID="${DBOND_BUNDLE_ID:-design.ivg.dbond}"
[[ "$BUNDLE_ID" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || { echo "Invalid bundle identifier" >&2; exit 1; }

cd "$ROOT"
swift build -c release --product DBond -Xswiftc -DDBOND_APP_BUNDLE
mkdir -p "$BIN_DIR" "$RES_DIR"
product_dir="$(swift build -c release --show-bin-path)"
cp "$product_dir/DBond" "$BIN_DIR/DBond"
/usr/bin/strip -S "$BIN_DIR/DBond"
install -m 644 "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$RES_DIR/"
cp "$ROOT/scripts/native_link.py" "$RES_DIR/"
python3 "$ROOT/scripts/package_dot_runtime.py" --output "$RES_DIR/dot-runtime"
mkdir -p "$RES_DIR/dot-host" "$RES_DIR/dot-plugin"
for name in connector provision core.py host.py requirements.lock upstream.json UPSTREAM_NOTICE.md; do
  cp -p "$ROOT/adapters/dot-host/$name" "$RES_DIR/dot-host/"
done
ditto "$ROOT/adapters/dot-host/vendor" "$RES_DIR/dot-host/vendor"
ditto "$ROOT/adapters/dot" "$RES_DIR/dot-plugin"
mkdir -p "$RES_DIR/dot-ingress"
install -m 755 "$ROOT/adapters/dot-ingress/ingress" "$RES_DIR/dot-ingress/"
install -m 644 "$ROOT/adapters/dot-ingress/ingress.py" "$RES_DIR/dot-ingress/"
install -m 644 "$ROOT/adapters/cli/dbond.py" "$RES_DIR/dbond.py"
cat > "$RES_DIR/dbond" <<'SH'
#!/bin/sh
set -eu
entry=$0
while [ -L "$entry" ]; do
  target=$(/usr/bin/readlink "$entry")
  case "$target" in
    /*) entry=$target ;;
    *) entry=$(dirname -- "$entry")/$target ;;
  esac
done
HERE=$(CDPATH= cd -- "$(dirname -- "$entry")" && pwd -P)
exec /bin/sh "$HERE/agent-installers/python-launch.sh" "$HERE/dbond.py" "$@"
SH
chmod 755 "$RES_DIR/dbond"
for module in owner.py wake_registration.py codex_registration.py claude_registration.py; do
  install -m 644 "$ROOT/adapters/cli/$module" "$RES_DIR/$module"
done

# Package connector installers without any local pairing or host credentials.
python3 "$ROOT/adapters/claude/build.py" >/dev/null
python3 "$ROOT/adapters/codex/build.py" >/dev/null
AGENT_DIR="$RES_DIR/agent-installers"
mkdir -p "$AGENT_DIR/claude-code-marketplace/.claude-plugin" \
  "$AGENT_DIR/claude-code-marketplace/plugin/.claude-plugin" \
  "$AGENT_DIR/claude-code-marketplace/plugin/server" \
  "$AGENT_DIR/codex-marketplace/.agents/plugins" \
  "$AGENT_DIR/codex-marketplace/plugins/d-bond-local-control" \
  "$AGENT_DIR/direct-stdio/adapters/direct/server" \
  "$AGENT_DIR/direct-stdio/adapters/cli"
install -m 644 "$ROOT/adapters/runtime/python_launch.sh" "$AGENT_DIR/python-launch.sh"
mkdir -p "$AGENT_DIR/examples/vollna" "$AGENT_DIR/adapters/cli"
install -m 644 "$ROOT/examples/vollna/forward_job.py" "$ROOT/examples/vollna/README.md" \
  "$ROOT/examples/vollna/n8n-job-intake.json" "$AGENT_DIR/examples/vollna/"
cat > "$AGENT_DIR/examples/vollna/forward-job" <<'SH'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$HERE/../../python-launch.sh" "$HERE/forward_job.py" "$@"
SH
chmod 755 "$AGENT_DIR/examples/vollna/forward-job"
mkdir -p "$AGENT_DIR/examples/backplane"
install -m 644 "$ROOT/examples/backplane/relay.py" "$ROOT/examples/backplane/README.md" "$AGENT_DIR/examples/backplane/"
for module in dbond.py owner.py wake_registration.py codex_registration.py claude_registration.py; do
  install -m 644 "$ROOT/adapters/cli/$module" "$AGENT_DIR/adapters/cli/$module"
done
install -m 644 "$ROOT/dist/d-bond-claude.mcpb" "$AGENT_DIR/d-bond-claude.mcpb"
/usr/bin/unzip -p "$AGENT_DIR/d-bond-claude.mcpb" manifest.json > "$AGENT_DIR/claude-desktop-manifest.json"
cat > "$AGENT_DIR/claude-code-marketplace/.claude-plugin/marketplace.json" <<'JSON'
{"name":"d-bond-local","owner":{"name":"IVG"},"plugins":[{"name":"d-bond","source":"./plugin"}]}
JSON
install -m 644 "$ROOT/adapters/claude/plugin/.claude-plugin/plugin.json" "$AGENT_DIR/claude-code-marketplace/plugin/.claude-plugin/plugin.json"
mkdir -p "$AGENT_DIR/claude-code-marketplace/plugin/hooks"
install -m 644 "$ROOT/adapters/claude/plugin/hooks/hooks.json" "$AGENT_DIR/claude-code-marketplace/plugin/hooks/"
install -m 755 "$ROOT/adapters/claude/plugin/hooks/session_start.py" "$AGENT_DIR/claude-code-marketplace/plugin/hooks/"
install -m 755 "$ROOT/adapters/claude/plugin/hooks/launch.sh" "$AGENT_DIR/claude-code-marketplace/plugin/hooks/"
install -m 644 "$ROOT/adapters/claude/plugin/.mcp.json" "$AGENT_DIR/claude-code-marketplace/plugin/.mcp.json"
for name in dbond.py mcp.py owner.py wake_registration.py codex_registration.py claude_registration.py; do
  install -m 644 "$ROOT/adapters/claude/plugin/server/$name" "$AGENT_DIR/claude-code-marketplace/plugin/server/$name"
done
install -m 755 "$ROOT/adapters/claude/plugin/server/launch.sh" "$AGENT_DIR/claude-code-marketplace/plugin/server/launch.sh"
install -m 644 "$ROOT/adapters/runtime/python_launch.sh" "$AGENT_DIR/claude-code-marketplace/plugin/server/python-launch.sh"
install -m 644 "$ROOT/adapters/codex/.agents/plugins/marketplace.json" "$AGENT_DIR/codex-marketplace/.agents/plugins/marketplace.json"
ditto "$ROOT/adapters/codex/plugins/d-bond-local-control" "$AGENT_DIR/codex-marketplace/plugins/d-bond-local-control"
install -m 644 "$ROOT/adapters/direct/server/mcp.py" "$AGENT_DIR/direct-stdio/adapters/direct/server/mcp.py"
mkdir -p "$AGENT_DIR/codex-session" "$AGENT_DIR/claude-session"
for module in dbond.py owner.py wake_registration.py codex_registration.py claude_registration.py; do
  install -m 644 "$ROOT/adapters/cli/$module" "$AGENT_DIR/direct-stdio/adapters/cli/$module"
  install -m 644 "$ROOT/adapters/cli/$module" "$AGENT_DIR/codex-session/$module"
  install -m 644 "$ROOT/adapters/cli/$module" "$AGENT_DIR/claude-session/$module"
done
for module in "$ROOT"/adapters/codex-session/*.py; do
  [[ "$(basename "$module")" == test_* ]] && continue
  install -m 644 "$module" "$AGENT_DIR/codex-session/"
done
for module in listener.py inbox.py; do
  install -m 644 "$ROOT/adapters/claude-session/$module" "$AGENT_DIR/claude-session/$module"
done
cat > "$AGENT_DIR/direct-stdio/launch.sh" <<'SH'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$HERE/python-launch.sh" "$HERE/adapters/direct/server/mcp.py"
SH
chmod 755 "$AGENT_DIR/direct-stdio/launch.sh"
install -m 644 "$ROOT/adapters/runtime/python_launch.sh" "$AGENT_DIR/direct-stdio/python-launch.sh"
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
  <key>CFBundleShortVersionString</key><string>0.4.1</string>
  <key>CFBundleVersion</key><string>15</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$APP_DIR/Contents/Info.plist"

# Generated Python caches are not application assets.
find "$RES_DIR" -type d -name __pycache__ -prune -exec rm -rf '{}' +
# The installer puts this symlink on PATH. Check the real packaged entry point.
mkdir "$staging_dir/cli-smoke"
ln -s "../$APP_NAME.app/Contents/Resources/dbond" "$staging_dir/cli-smoke/dbond"
"$staging_dir/cli-smoke/dbond" --help >/dev/null
python3 "$ROOT/scripts/release_sign.py" sign --app "$APP_DIR"
python3 "$ROOT/scripts/package_dot_runtime.py" --output "$RES_DIR/dot-runtime" --smoke-only
if [[ -d "$ROOT/dist/$APP_NAME.app" ]]; then
  mv "$ROOT/dist/$APP_NAME.app" "$staging_dir/previous.app"
fi
if ! mv "$APP_DIR" "$ROOT/dist/$APP_NAME.app"; then
  if [[ -d "$staging_dir/previous.app" ]]; then mv "$staging_dir/previous.app" "$ROOT/dist/$APP_NAME.app"; fi
  exit 1
fi
echo "Created signed app (${DBOND_SIGNING_MODE:-adhoc}): $ROOT/dist/$APP_NAME.app"
