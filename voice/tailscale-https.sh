#!/usr/bin/env bash
# tailscale-https.sh — front the voice server with real HTTPS via tailscale serve.
#
#   ./tailscale-https.sh        # set up: operator + serve proxy -> its http port
#   ./tailscale-https.sh off    # tear down the serve proxy
#
# One-time effect: makes your user the tailscale "operator" so serve config
# can be managed without sudo afterwards. Re-runs are harmless.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The plain-HTTP listener: the voice project's web_extra named "http", from
# the registry. No fallback — nothing here should guess a port.
# Worked out before the sudo re-exec below, which carries it over (root's
# home has no config of ours).
if [ -z "${PORT:-}" ]; then
PORT="$(python3 -c '
import sys; sys.path.insert(0, sys.argv[1])
import registry
p = registry.find(registry.load(), registry.radio_config.CFG["voice_project"]) or {}
print(next((x["port"] for x in p.get("web_extra", []) if x.get("name") == "http"), ""))
' "$DIR/../core")"
fi
[ -n "$PORT" ] || { echo "no http port registered for the voice project" >&2; exit 1; }

# Re-exec under sudo if needed, remembering who the real user is.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo REAL_USER="$USER" PORT="$PORT" "$0" "$@"
fi
REAL_USER="${REAL_USER:-${SUDO_USER:-root}}"

if [ "${1:-}" = "off" ]; then
  tailscale serve --https=443 off
  echo "tailscale serve disabled."
  exit 0
fi

tailscale set --operator="$REAL_USER"
echo "operator set to '$REAL_USER' (future 'tailscale serve' tweaks won't need sudo)"

tailscale serve --bg "http://127.0.0.1:${PORT}"
echo
tailscale serve status
echo

DNSNAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo "the voice server is now at: https://${DNSNAME}"
echo "(real certificate, reachable from any device on your tailnet)"
