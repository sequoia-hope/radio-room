#!/usr/bin/env bash
# serve.sh — run the reservation server on its registered port.
#
# The number lives in the registry, never in this
# file: $PORT when the hub started us, otherwise the server asks `proj port`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 server.py "$@"
