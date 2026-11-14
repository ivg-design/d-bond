#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
TEMP=$(mktemp -d /tmp/dbond-python-launch.XXXXXX)
trap 'rm -rf "$TEMP"' EXIT

APP="$TEMP/Applications/D-Bond.app"
RUNTIME="$APP/Contents/Resources/dot-runtime/python/bin"
mkdir -p "$RUNTIME" "$TEMP/exported/server" "$APP/Contents/Resources/agent-installers"
cat > "$RUNTIME/python3.13" <<'SH'
#!/bin/sh
if [ "$4" = '-c' ]; then exit 0; fi
printf '%s\n' "$@" > "$DBOND_LAUNCH_RECEIPT"
SH
chmod 755 "$RUNTIME/python3.13"
cp "$ROOT/adapters/runtime/python_launch.sh" "$TEMP/exported/server/python-launch.sh"
cp "$ROOT/adapters/runtime/python_launch.sh" "$APP/Contents/Resources/agent-installers/python-launch.sh"
printf 'fixture\n' > "$TEMP/exported/server/mcp.py"

# A copied plugin has no source checkout or Python on PATH. It resolves the
# explicitly installed app and launches its bundled runtime with isolation flags.
DBOND_APP_BUNDLE="$APP" DBOND_LAUNCH_RECEIPT="$TEMP/receipt" PATH=/usr/bin:/bin \
  /bin/sh "$TEMP/exported/server/python-launch.sh" "$TEMP/exported/server/mcp.py" hello
expected=$(printf '%s\n' -E -s -B "$TEMP/exported/server/mcp.py" hello)
[ "$(cat "$TEMP/receipt")" = "$expected" ]

# An in-app entry point resolves the same runtime from its resource ancestry,
# without any environment setting for the app location.
DBOND_LAUNCH_RECEIPT="$TEMP/receipt" PATH=/usr/bin:/bin \
  /bin/sh "$APP/Contents/Resources/agent-installers/python-launch.sh" "$TEMP/exported/server/mcp.py" local
expected=$(printf '%s\n' -E -s -B "$TEMP/exported/server/mcp.py" local)
[ "$(cat "$TEMP/receipt")" = "$expected" ]
echo 'PASS: packaged and exported launchers use the app runtime without PATH Python'
