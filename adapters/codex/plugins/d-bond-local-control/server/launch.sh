#!/bin/sh
set -eu
PYTHONDONTWRITEBYTECODE=1 exec /usr/bin/python3 "$(dirname "$0")/mcp.py"
