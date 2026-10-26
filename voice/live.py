"""Live terminals in the page: one WebSocket, one tmux client.

    GET /ws/attach?project=P&agent=A&cols=C&rows=R[&token=T]

The Projects tab expands a project into a terminal on one of its agents'
sessions. Each is a PTY running `tmux attach` on that session — or `docker
exec -it <container> tmux attach` for one inside a container — streamed over
a WebSocket, so it is exactly another tmux client, like the desktop window's
VTE tabs. Closing the socket detaches that client; the agent keeps running.

Nothing is buffered here, unlike terminal/'s sessions: tmux repaints a client
the moment it attaches, so a reconnect is just another attach.

On the wire, as terminal/'s page has it: binary frames down are PTY bytes;
text frames up are JSON — {"t": "i", "d": text}, {"t": "ib", "d": base64}
for bytes, {"t": "resize", "cols": C, "rows": R}. Text frames down say how it
ended: {"t": "exit"} when the client exits (detached, or the session ended),
{"t": "error", "message": ...} when it never started.
"""

from __future__ import annotations

import base64
import fcntl
import importlib.util
import json
import os
import pty
import shutil
import signal
import struct
import termios
import threading
import time
import warnings
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wsproto", Path(__file__).resolve().parent.parent / "terminal" / "wsproto.py")
wsproto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsproto)

READ_CHUNK = 65536
PING_EVERY = 20.0


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def attach_argv(target: dict) -> list[str]:
    """The tmux client for a target, in status.py's shape. `=` anchors the
    session name: a bare target is a prefix match."""
    if target["type"] == "docker":
        return [shutil.which("docker") or "docker", "exec", "-it", target["container"],
                "tmux", "attach-session", "-t", f"={target['session']}"]
    return [shutil.which("tmux") or "tmux", "attach-session", "-t", f"={target['session']}"]


def client_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMUX", "TMUX_PANE", "STY", "WINDOWID")}
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    env.setdefault("LANG", "C.UTF-8")
    return env


def handshake(handler) -> "wsproto.WebSocket":
    """Answer the upgrade on a BaseHTTPRequestHandler and hand back the socket."""
    accept = wsproto.accept_key(handler.headers["Sec-WebSocket-Key"])
    handler.wfile.write((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    handler.wfile.flush()
    handler.close_connection = True
    handler.connection.settimeout(None)
    # The browser may already have sent frames behind the handshake, sitting
    # in the handler's buffered reader; the socket has to keep reading there.
    return wsproto.WebSocket(handler.connection, reader=handler.rfile)


def refuse(ws, message: str) -> None:
    try:
        ws.send_text(json.dumps({"t": "error", "message": message}))
    finally:
        ws.close()


def run(ws, target: dict, cols: int, rows: int) -> None:
    """Attach, then pump both ways until either side goes."""
    cols, rows = max(20, min(cols, 500)), max(5, min(rows, 300))
    argv, env = attach_argv(target), client_env()
    master, slave = pty.openpty()
    set_winsize(slave, cols, rows)
    with warnings.catch_warnings():
        # Forking a threaded process: the child does nothing but fd setup and
        # an exec, the same shape as pty.fork() (and terminal/session.py).
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for fd in (0, 1, 2):
                os.dup2(slave, fd)
            if slave > 2:
                os.close(slave)
            os.execve(argv[0], argv, env)
        except BaseException:
            os._exit(127)
    os.close(slave)
    done = threading.Event()

    def pump():
        try:
            while True:
                try:
                    data = os.read(master, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                ws.send_bytes(data)
        except wsproto.WebSocketError:
            pass
        finally:
            done.set()
            try:
                ws.send_text(json.dumps({"t": "exit"}))
            except wsproto.WebSocketError:
                pass
            ws.close()

    def keepalive():
        while not done.wait(PING_EVERY):
            try:
                ws.ping()
            except wsproto.WebSocketError:
                return

    threading.Thread(target=pump, daemon=True, name="live-pty").start()
    threading.Thread(target=keepalive, daemon=True, name="live-ping").start()
    try:
        while not done.is_set():
            msg = ws.recv()
            if msg is None:
                break
            opcode, payload = msg
            if opcode != wsproto.OP_TEXT:
                continue
            try:
                m = json.loads(payload)
            except ValueError:
                continue
            kind = m.get("t")
            if kind == "i":
                os.write(master, str(m.get("d", "")).encode())
            elif kind == "ib":
                os.write(master, base64.b64decode(m.get("d", "")))
            elif kind == "resize":
                set_winsize(master, max(20, min(int(m.get("cols") or cols), 500)),
                            max(5, min(int(m.get("rows") or rows), 300)))
    except (wsproto.WebSocketError, OSError, ValueError):
        pass
    finally:
        done.set()
        ws.close()
        _end(pid)
        try:
            os.close(master)
        except OSError:
            pass


def _end(pid: int) -> None:
    """Let the tmux client go: SIGHUP is a hung-up terminal, which detaches
    it and leaves the session be. SIGKILL only if it lingers."""
    for sig, wait in ((signal.SIGHUP, 1.0), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    return
            except ChildProcessError:
                return
            time.sleep(0.05)
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
