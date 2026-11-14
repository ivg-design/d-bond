#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
ref="${1:-HEAD}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/source" "$stage/archive" "$stage/check"

# Package only files in the chosen Git revision.
git -C "$root" archive "$ref" | tar -x -C "$stage/source"
DBOND_BUNDLE_ID=design.ivg.dbond bash "$stage/source/scripts/bundle.sh"
app="$stage/source/dist/D-Bond.app"
arch="$(lipo -archs "$app/Contents/MacOS/DBond")"
case "$arch" in arm64|x86_64) ;; *) echo "Unexpected release architecture: $arch" >&2; exit 1 ;; esac
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist")"
out="$root/dist/releases/v$version"
mkdir -p "$out"

ditto --norsrc --noextattr --noqtn "$app" "$stage/archive/D-Bond.app"
cp "$stage/source/LICENSE" "$stage/source/THIRD_PARTY_NOTICES.md" "$stage/archive/"
zip_name="D-Bond-macOS-$arch.zip"
ditto -c -k --norsrc --noextattr --noqtn "$stage/archive" "$stage/$zip_name"
ditto -x -k "$stage/$zip_name" "$stage/check"
codesign --verify --deep --strict "$stage/check/D-Bond.app"
cp "$stage/source/dist/d-bond-claude.mcpb" "$stage/"
unzip -tq "$stage/d-bond-claude.mcpb"
(
  cd "$stage"
  shasum -a 256 "$zip_name" d-bond-claude.mcpb > SHA256SUMS.txt
  shasum -a 256 -c SHA256SUMS.txt
)
cp "$stage/$zip_name" "$stage/d-bond-claude.mcpb" "$stage/SHA256SUMS.txt" "$out/"
echo "Release assets for $version: $out"
