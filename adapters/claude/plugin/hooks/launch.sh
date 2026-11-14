#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$here/../server/python-launch.sh" "$here/session_start.py"
