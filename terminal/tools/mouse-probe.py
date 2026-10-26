#!/usr/bin/env python3
"""Show what a mouse-tracking app actually receives from this terminal.

Run it inside the web terminal, then scroll. It turns on the same tracking
modes claude and tmux use and prints every byte that arrives, so you can see
whether a gesture arrived as a wheel report or as cursor keys.

Ctrl-C to quit.
"""
import os
import sys
import termios
import tty

ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?1049h"
DISABLE = "\x1b[?1049l\x1b[?1006l\x1b[?1002l\x1b[?1000l"

def describe(chunk: bytes) -> str:
    if chunk.startswith(b"\x1b[<"):
        try:
            body = chunk[3:-1].decode()
            button, x, y = body.split(";")
            names = {64: "WHEEL UP", 65: "WHEEL DOWN", 0: "left", 2: "right"}
            return f"SGR mouse: {names.get(int(button), 'button ' + button)} at {x},{y}"
        except ValueError:
            return "SGR mouse (unparsed)"
    if chunk.startswith(b"\x1b[M"):
        return "X10 mouse report"
    return {b"\x1b[A": "cursor UP", b"\x1b[B": "cursor DOWN",
            b"\x1bOA": "cursor UP (app mode)", b"\x1bOB": "cursor DOWN (app mode)"}.get(
                chunk, "")

fd = sys.stdin.fileno()
saved = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    sys.stdout.write(ENABLE + "\x1b[2J\x1b[H")
    sys.stdout.write("mouse probe — scroll, tap, drag. ctrl-c quits.\r\n\r\n")
    sys.stdout.flush()
    while True:
        chunk = os.read(fd, 1024)
        if chunk == b"\x03":
            break
        note = describe(chunk)
        sys.stdout.write(f"{chunk!r:<40} {note}\r\n")
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    sys.stdout.write(DISABLE)
    sys.stdout.flush()
