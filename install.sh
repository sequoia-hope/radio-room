#!/usr/bin/env bash
# install.sh — put Radio Room on this machine: `proj` and `radio-room` on
# PATH (~/.local/bin), the desktop window in the app grid, and a config file
# to edit if there isn't one. Everything is a symlink back into this checkout,
# so a `git pull` is an upgrade. Safe to re-run; nothing existing is replaced.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
CONF="${RADIO_ROOM_CONFIG:-$HOME/.config/radio-room/config.toml}"

link() {   # link TARGET NAME
  if [ -e "$2" ] || [ -L "$2" ]; then
    echo "kept     $2"
  else
    mkdir -p "$(dirname "$2")"
    ln -s "$1" "$2"
    echo "linked   $2"
  fi
}

link "$REPO/core/proj" "$BIN/proj"
link "$REPO/desktop/radio_room.py" "$BIN/radio-room"
# Agents outlive a login; this keeps their image paste (xclip) on the live display.
command -v xclip >/dev/null && link "$REPO/core/bin/xclip" "$BIN/xclip"
link "$REPO/desktop/net.radioroom.Desktop.desktop" "$APPS/net.radioroom.Desktop.desktop"
link "$REPO/desktop/icons/net.radioroom.Desktop.svg" "$ICONS/net.radioroom.Desktop.svg"

if [ -e "$CONF" ]; then
  echo "kept     $CONF"
else
  mkdir -p "$(dirname "$CONF")"
  cp "$REPO/config.example.toml" "$CONF"
  echo "wrote    $CONF (the defaults; edit to taste)"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on PATH; add it in ~/.profile" ;;
esac

missing=()
command -v tmux >/dev/null || missing+=(tmux)
command -v xclip >/dev/null || command -v wl-paste >/dev/null || missing+=("xclip (pasting images into agents)")
python3 -c 'import gi; gi.require_version("Vte", "2.91"); gi.require_version("WebKit2", "4.1")' \
  2>/dev/null || missing+=("gir1.2-vte-2.91 gir1.2-webkit2-4.1 python3-gi")
if [ ${#missing[@]} -gt 0 ]; then
  echo "still needed: ${missing[*]}"
fi
