#!/usr/bin/env bash
# serve.sh — run Radio Room's voice server (its ports come from the registry).
#   ./serve.sh       # foreground
#   ./serve.sh -d    # detached, in tmux session "vp-server"
#   ./serve.sh -k    # stop the detached server
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="vp-server"

case "${1:-}" in
  -d)
    # `=` anchors the session name: a bare tmux target is a *prefix* match, so
    # an unanchored name finds any session merely starting with it. Pane targets
    # (capture-pane) need the trailing colon too, or tmux says "can't find pane".
    if tmux has-session -t "=$SESSION" 2>/dev/null; then
      echo "already running (tmux session '$SESSION')"
    else
      tmux new-session -d -s "$SESSION" -c "$DIR" "python3 server.py"
      echo "started in tmux session '$SESSION'"
    fi
    sleep 0.5
    tmux capture-pane -p -t "=$SESSION:" | grep -m1 "serving" || true
    ;;
  -k)
    tmux kill-session -t "=$SESSION" 2>/dev/null && echo "stopped" || echo "not running"
    ;;
  *)
    exec python3 "$DIR/server.py"
    ;;
esac
