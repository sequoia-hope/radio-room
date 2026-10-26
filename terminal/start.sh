#!/usr/bin/env bash
# start.sh — what `proj up terminal` runs. The hub exports PORT from the
# registry; server.py also asks `proj port` itself when run by hand.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./server.py "$@"
