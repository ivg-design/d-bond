#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="D-Bond"
SOURCE_APP="$ROOT/dist/$APP_NAME.app"
DEST_DIR="${1:-$HOME/Applications}"
DEST_APP="$DEST_DIR/$APP_NAME.app"
if [[ -z "${DBOND_BUNDLE_ID:-}" && -f "$DEST_APP/Contents/Info.plist" ]]; then
  EXPECTED_BUNDLE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DEST_APP/Contents/Info.plist" 2>/dev/null || true)
else
  EXPECTED_BUNDLE_ID="${DBOND_BUNDLE_ID:-design.ivg.dbond}"
fi
[[ "$EXPECTED_BUNDLE_ID" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || { printf 'install_app: invalid bundle ID\n' >&2; exit 1; }
AGENT_LABEL="$EXPECTED_BUNDLE_ID.supervisor"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT="$AGENT_DIR/$AGENT_LABEL.plist"
DOMAIN="gui/$(id -u)"

die() { printf 'install_app: %s\n' "$*" >&2; exit 1; }

[[ "$DEST_DIR" = /* ]] || die "destination must be an absolute directory path"
[[ "$DEST_DIR" != "/" ]] || die "refusing root as an installation directory"
if [[ "$DEST_DIR" == "$HOME/Applications" ]] && pgrep -x DBond >/dev/null; then
  die "D-Bond is running. Quit it before upgrading so launchd can own the next instance."
fi

# Build before touching a currently installed copy.
DBOND_BUNDLE_ID="$EXPECTED_BUNDLE_ID" bash "$ROOT/scripts/bundle.sh"
[[ -d "$SOURCE_APP/Contents" ]] || die "build did not produce $SOURCE_APP"
SOURCE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SOURCE_APP/Contents/Info.plist")
[[ "$SOURCE_ID" == "$EXPECTED_BUNDLE_ID" ]] || die "built app has unexpected bundle ID: $SOURCE_ID"

if [[ -e "$DEST_APP" || -L "$DEST_APP" ]]; then
  [[ -d "$DEST_APP" && ! -L "$DEST_APP" && -f "$DEST_APP/Contents/Info.plist" ]] || \
    die "destination exists and is not a recognized app bundle: $DEST_APP"
  DEST_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DEST_APP/Contents/Info.plist" 2>/dev/null || true)
  [[ "$DEST_ID" == "$EXPECTED_BUNDLE_ID" ]] || \
    die "destination belongs to another or unrecognized app (bundle ID: ${DEST_ID:-missing}); leaving it untouched"
fi

if [[ "$DEST_DIR" == "$HOME/Applications" && -e "$AGENT" ]]; then
  [[ ! -L "$AGENT" && -f "$AGENT" ]] || die "unexpected launch agent at $AGENT"
  AGENT_ID=$(/usr/libexec/PlistBuddy -c 'Print :Label' "$AGENT" 2>/dev/null || true)
  AGENT_PROGRAM=$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$AGENT" 2>/dev/null || true)
  [[ "$AGENT_ID" == "$AGENT_LABEL" && "$AGENT_PROGRAM" == "$DEST_APP/Contents/MacOS/DBond" ]] || \
    die "existing launch agent differs from D-Bond's expected job: $AGENT"
fi

mkdir -p "$DEST_DIR"
STAGE="$DEST_DIR/.$APP_NAME.installing.$$.app"
BACKUP="$DEST_DIR/.$APP_NAME.previous.$$.app"
cleanup() {
  [[ ! -e "$STAGE" ]] || rm -rf "$STAGE"
  if [[ -e "$BACKUP" && ! -e "$DEST_APP" ]]; then mv "$BACKUP" "$DEST_APP"; fi
}
trap cleanup EXIT INT TERM

ditto "$SOURCE_APP" "$STAGE"
STAGE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$STAGE/Contents/Info.plist")
[[ "$STAGE_ID" == "$EXPECTED_BUNDLE_ID" ]] || die "staged app failed bundle ID check"

# Preserve the old recognized bundle until the new copy is fully staged.
if [[ -e "$DEST_APP" ]]; then mv "$DEST_APP" "$BACKUP"; fi
mv "$STAGE" "$DEST_APP"
rm -rf "$BACKUP"
trap - EXIT INT TERM

if [[ "$DEST_DIR" == "$HOME/Applications" ]]; then
  CLI_DIR="$HOME/.local/bin"
  CLI="$CLI_DIR/dbond"
  mkdir -p "$CLI_DIR"
  if [[ ! -e "$CLI" && ! -L "$CLI" ]]; then
    ln -s "$DEST_APP/Contents/Resources/dbond" "$CLI"
  elif [[ -L "$CLI" && "$(readlink "$CLI")" == "$ROOT/adapters/cli/dbond.py" ]]; then
    # Migrate the older checkout-based install; upgrading the app now updates the CLI.
    ln -sfn "$DEST_APP/Contents/Resources/dbond" "$CLI"
  elif [[ ! -L "$CLI" || "$(readlink "$CLI")" != "$DEST_APP/Contents/Resources/dbond" ]]; then
    printf 'Left the existing command at %s unchanged. The bundled CLI is at %s\n' "$CLI" "$DEST_APP/Contents/Resources/dbond"
  fi

  mkdir -p "$AGENT_DIR"
  AGENT_STAGE="$AGENT_DIR/.$AGENT_LABEL.$$.plist"
  python3 - "$AGENT_STAGE" "$AGENT_LABEL" "$DEST_APP/Contents/MacOS/DBond" <<'PY'
import pathlib
import plistlib
import sys

path, label, program = sys.argv[1:]
job = {
    "Label": label,
    "ProgramArguments": [program],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Interactive",
}
with pathlib.Path(path).open("wb") as stream:
    plistlib.dump(job, stream)
PY
  plutil -lint "$AGENT_STAGE" >/dev/null
  chmod 644 "$AGENT_STAGE"
  if launchctl print "$DOMAIN/$AGENT_LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$AGENT_LABEL"
  fi
  mv "$AGENT_STAGE" "$AGENT"
  launchctl enable "$DOMAIN/$AGENT_LABEL"
  launchctl bootstrap "$DOMAIN" "$AGENT"
fi

printf 'Installed D-Bond at %s\n' "$DEST_APP"
if [[ "$DEST_DIR" == "$HOME/Applications" ]]; then
  printf 'D-Bond is supervised by launchd at login and after abnormal exits. Quit D-Bond exits normally and stays stopped until the next login.\n'
  printf 'Remove any older D-Bond Open at Login item in System Settings so there is only one login launch path.\n'
else
  printf 'This nondefault destination was packaged only; no login job was installed.\n'
fi
