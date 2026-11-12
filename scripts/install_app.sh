#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="D-Bond"
SOURCE_APP="$ROOT/dist/$APP_NAME.app"
DEST_DIR="${1:-$HOME/Applications}"
DEST_APP="$DEST_DIR/$APP_NAME.app"
EXPECTED_BUNDLE_ID="${DBOND_BUNDLE_ID:-design.ivg.dbond}"

die() { printf 'install_app: %s\n' "$*" >&2; exit 1; }

[[ "$DEST_DIR" = /* ]] || die "destination must be an absolute directory path"
[[ "$DEST_DIR" != "/" ]] || die "refusing root as an installation directory"

# Build before touching a currently installed copy.
bash "$ROOT/scripts/bundle.sh"
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
fi

printf 'Installed D-Bond at %s\n' "$DEST_APP"
printf 'Open the app, then select Connections → Add Connection in its menu bar menu. Use Start at Login to keep it available.\n'
