#!/bin/sh
# Run a packaged D-Bond Python entry point without relying on the host's PATH.
set -eu

if [ "$#" -lt 1 ]; then
  echo 'D-Bond Python entry point is missing.' >&2
  exit 64
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usable() {
  [ -x "$1" ] && "$1" -E -s -B -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

# A helper still inside the installed app can find its bundled runtime even if the
# user installed the app somewhere other than Applications.
probe=$here
count=0
while [ "$count" -lt 9 ]; do
  candidate="$probe/dot-runtime/python/bin/python3.13"
  if usable "$candidate"; then exec "$candidate" -E -s -B "$@"; fi
  parent=$(dirname -- "$probe")
  [ "$parent" = "$probe" ] && break
  probe=$parent
  count=$((count + 1))
done

# An exported plugin can be moved into a host cache. Locate a separately
# installed D-Bond app; never depend on the source checkout or build path.
for app in "${DBOND_APP_BUNDLE:-}" "$HOME/Applications/D-Bond.app" /Applications/D-Bond.app; do
  [ -n "$app" ] || continue
  candidate="$app/Contents/Resources/dot-runtime/python/bin/python3.13"
  if usable "$candidate"; then exec "$candidate" -E -s -B "$@"; fi
done

# Standalone plugins continue working on Macs with a suitable host Python.
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3" /usr/bin/python3; do
  if usable "$candidate"; then exec "$candidate" -E -s -B "$@"; fi
done
echo 'D-Bond needs its installed app runtime or Python 3.10 or newer.' >&2
exit 1
