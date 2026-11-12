#!/bin/sh
# Claude Desktop can launch with a PATH where python3 is macOS's Python 3.9.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for candidate in \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  "$HOME/Library/Application Support/DotBridge/venv/bin/python" \
  "$HOME/.local/bin/python3" \
  /usr/bin/python3
do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(not ((3, 9) <= sys.version_info[:2] < (4, 0)))' >/dev/null 2>&1; then
    exec "$candidate" "$script_dir/mcp.py"
  fi
done
echo 'D-Bond needs Python 3.9 or newer; no supported local interpreter was found.' >&2
exit 1
