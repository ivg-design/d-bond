#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
launcher="$here/python-launch.sh"
[ -f "$launcher" ] || launcher="$here/../../../../runtime/python_launch.sh"
exec /bin/sh "$launcher" "$here/mcp.py"
