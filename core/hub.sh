#!/usr/bin/env bash
# hub.sh — run the project hub dashboard.
#   ./hub.sh       # foreground
#   ./hub.sh -d    # detached, in tmux session "hub"
#   ./hub.sh -k    # stop the detached server
#   ./hub.sh -r    # restart it
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="hub"

# `=` anchors the session name: a bare tmux target is a prefix match, which
# would find a session merely starting with "hub". Pane targets (capture-pane)
# need the trailing colon as well, or tmux answers "can't find pane".
start() {
  if tmux has-session -t "=$SESSION" 2>/dev/null; then
    echo "already running (tmux session '$SESSION')"
    return
  fi
  tmux new-session -d -s "$SESSION" -c "$DIR" "./hub.py; exec bash"
  sleep 0.6
  tmux capture-pane -p -t "=$SESSION:" | grep -m1 "project hub" \
    || { echo "hub failed to start; pane says:"; tmux capture-pane -p -t "=$SESSION:" | tail -5; exit 1; }
}

case "${1:-}" in
  -d) start ;;
  -k) tmux kill-session -t "=$SESSION" 2>/dev/null && echo "stopped" || echo "not running" ;;
  -r) tmux kill-session -t "=$SESSION" 2>/dev/null || true; sleep 0.3; start ;;
  *)  exec "$DIR/hub.py" ;;
esac
