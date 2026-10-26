#!/usr/bin/env python3
"""terminal — a browser terminal into this machine's login shell.

    ./server.py                serve on the Tailscale address, on the registered port
    ./server.py --port N       different port
    ./server.py --bind ADDR    different address ('any' for 0.0.0.0)
    ./server.py --no-tls       plain HTTP only (and so no microphone)

Opening the page drops you into the same login shell an SSH login gets —
and so into whatever launcher your ~/.bashrc starts for one (see README). The PTY lives in this process and outlives
every browser connection, so a closed browser or a dead phone screen leaves
the session running; reconnecting replays recent output and forces a repaint.

There can be several of those at once — one per tab, each named after the
project it is showing (see tabs.py). Which tab is in front is server state
like everything else here, so a phone and a laptop are two views of one thing
rather than two sessions.

It serves HTTPS with the machine's own tailnet certificate, from `tailscale
cert`, because a browser will not hand a plain-HTTP page the microphone — and
the microphone is how voice mode works from a phone (see mic.py). Plain HTTP
still answers on the same port, and sends page loads over to HTTPS.

This binds to the Tailscale interface by default and on purpose. What it
serves is an unauthenticated shell, and the tailnet is the only boundary
around it — do not move it to 0.0.0.0 on an untrusted network.

Endpoints:
    GET  /              the terminal
    GET  /ws            WebSocket: binary frames are PTY bytes down and mic
                        audio up, text is JSON
    GET  /api/health    JSON — the tabs, and whether their PTYs are up
    POST /api/image     an image, saved where the agents can read it
                        (core/pastes.py); JSON {"path": ...} for the page to paste

    /?attach=<project>[&agent=claude|agy] opens a tab on that project's running
    agent session (or switches to one already showing it); see attach.py.
"""

import argparse
import base64
import binascii
import fcntl
import json
import mimetypes
import os
import queue
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import attach as attaching
import wsproto
from mic import MicError, VirtualMic
from session import PtySession
from tabs import TabSet

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
import pastes  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
CERTS = HERE / "certs"

TAILSCALE_IF = "tailscale0"
# tailscale renews when asked and the certificate is shorter-lived than a
# server can be, so ask again now and then and pick up whatever comes back.
CERT_REFRESH = 24 * 3600

# Big enough that a burst of terminal output never trips it, small enough that
# a browser that has stopped reading gets cut loose instead of eating memory.
# A dropped client reconnects and replays, so this costs nothing but a blink.
SEND_QUEUE_DEPTH = 4096

PING_INTERVAL = 25.0
POLL_INTERVAL = 1.0  # how fast a writer thread notices its connection died

tabs: TabSet = None
mic = VirtualMic()


# ── where to listen ──────────────────────────────────────────────────────────

def interface_ip(name: str) -> str | None:
    """IPv4 address of an interface, or None if it has none."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            sock.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack("256s", name.encode()[:15]),
        )
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def resolve_port(flag: int | None) -> int | None:
    """--port, then $PORT (the hub exports it), then the registry. No default:
    a server that cannot find its port says so rather than guessing one."""
    if flag is not None:
        return flag
    if os.environ.get("PORT"):
        return int(os.environ["PORT"])
    try:
        done = subprocess.run([str(HERE.parent / "core" / "proj"), "port"], cwd=HERE, capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.strip()
    return int(out) if done.returncode == 0 and out.isdigit() else None


# ── TLS ──────────────────────────────────────────────────────────────────────

def tailnet_name() -> str | None:
    """This machine's MagicDNS name — the one name its certificate is for."""
    try:
        done = subprocess.run(["tailscale", "status", "--self", "--json"],
                              capture_output=True, text=True, timeout=10)
        name = json.loads(done.stdout)["Self"]["DNSName"].rstrip(".")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return None
    return name or None


def fetch_cert(name: str) -> tuple[Path, Path]:
    """Ask tailscale for the certificate; it hands back a cached one while that
    is still good. Raises on failure."""
    CERTS.mkdir(mode=0o700, exist_ok=True)
    cert, key = CERTS / f"{name}.crt", CERTS / f"{name}.key"
    subprocess.run(
        ["tailscale", "cert", "--cert-file", str(cert), "--key-file", str(key), name],
        check=True, capture_output=True, timeout=120,
    )
    return cert, key


def tls_context(name: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(*fetch_cert(name))

    def refresh():
        while True:
            time.sleep(CERT_REFRESH)
            try:
                # Only new handshakes see it; live connections keep theirs.
                ctx.load_cert_chain(*fetch_cert(name))
            except (OSError, subprocess.SubprocessError, ssl.SSLError) as exc:
                print(f"certificate refresh failed: {exc}", flush=True)

    threading.Thread(target=refresh, daemon=True, name="cert-refresh").start()
    return ctx


class Server(ThreadingHTTPServer):
    """HTTPS and HTTP on one port, told apart by the first byte.

    A TLS connection opens with a handshake record (0x16), and nothing that
    speaks HTTP does. So old http:// links and bookmarks keep working — they
    get redirected — without a second port to register.
    """

    daemon_threads = True
    tls: ssl.SSLContext | None = None
    https_origin: str | None = None  # where a plain-HTTP page load is sent

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        # Bounded, so a client that connects and says nothing cannot hold a
        # thread forever, and lifted again once the handshake is done.
        request.settimeout(15)
        try:
            first = request.recv(1, socket.MSG_PEEK)
            if first == b"\x16":
                request = self.tls.wrap_socket(request, server_side=True)
            request.settimeout(None)
        except (OSError, ssl.SSLError):
            return
        try:
            self.RequestHandlerClass(request, client_address, self)
        finally:
            if isinstance(request, ssl.SSLSocket):
                request.close()


# ── the shell behind the terminal ────────────────────────────────────────────

def child_env() -> dict:
    """Environment for the PTY, matching what sshd would hand a login shell.

    Inherited rather than rebuilt so things like XDG_RUNTIME_DIR and the D-Bus
    address survive, but with the multiplexer variables stripped: this server
    is normally started inside a tmux session by `proj up`, and a leaked TMUX
    would make the launcher's `tmux attach` refuse to nest.
    """
    env = dict(os.environ)
    for leaked in ("TMUX", "TMUX_PANE", "STY", "WINDOWID"):
        env.pop(leaked, None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.setdefault("LANG", "C.UTF-8")
    # A login script sourced from ~/.bashrc is what starts the launcher on
    # login, gated on SSH; this is the second key that opens it.
    env["SSH_CLIENT_WEB"] = "1"
    return env


LOGIN_SHELL = ["/bin/bash", "-li"]


def attach_command(project: str, agent: str) -> str:
    return " ".join(shlex.quote(a) for a in
                    (sys.executable, str(Path(__file__).resolve().parent / "attach.py"),
                     project, agent))


# ── HTTP + WebSocket ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ssh_client"

    def log_message(self, fmt, *args):
        pass  # the interesting events are logged by hand below

    def note(self, msg: str) -> None:
        print(f"[{self.address_string()}] {msg}", flush=True)

    # -- routing --

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.wants_https(path):
            self.send_response(301)
            self.send_header("Location", self.server.https_origin + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/ws":
            if wsproto.is_upgrade(self.headers):
                self.serve_ws()
            else:
                self.send_error(400, "expected a WebSocket upgrade")
            return
        if path == "/api/health":
            # Names are only re-read while a browser is watching, so ask for a
            # fresh reading here rather than reporting whatever the last one
            # said — this is the endpoint you reach for when something looks
            # wrong, and a stale answer is worse than none.
            tabs.refresh()
            self.send_json(
                {
                    "active": tabs.active,
                    "tabs": [
                        {
                            "id": tab.id,
                            "name": tab.name,
                            "alive": tab.session.alive,
                            "pid": tab.session.pid,
                            "cols": tab.session.cols,
                            "rows": tab.session.rows,
                        }
                        for tab in tabs.all()
                    ],
                    "mic": mic.status(),
                }
            )
            return
        self.serve_static(path)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/image":
            self.send_error(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > pastes.LIMIT:
            # Hang up rather than read it: the point of the cap is not to.
            self.close_connection = True
            self.send_json({"error": f"image too large ({length} bytes)"}, 413)
            return
        try:
            path = pastes.save(self.rfile.read(length))
        except (ValueError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return
        self.note(f"image {path.name}")
        self.send_json({"path": str(path)})

    def wants_https(self, path: str) -> bool:
        """A page load over plain HTTP, when HTTPS is on offer. The socket and
        the health check stay put: scripts curl one, and a page that is
        already loaded has no use for a redirect on the other."""
        return (self.server.tls is not None
                and not isinstance(self.connection, ssl.SSLSocket)
                and path not in ("/ws", "/api/health"))

    def send_json(self, payload: dict, code: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not target.is_file() or STATIC not in target.parents:
            self.send_error(404, "not found")
            return

        body = target.read_bytes()
        ctype, _ = mimetypes.guess_type(target.name)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # Vendored libraries are version-pinned and never change in place; the
        # page and its script do, and stale ones are maddening to debug.
        if target.parent.name == "vendor":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- the terminal connection --

    def serve_ws(self):
        accept = wsproto.accept_key(self.headers["Sec-WebSocket-Key"])
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        self.wfile.flush()
        self.close_connection = True

        self.connection.settimeout(None)
        # self.rfile may already hold frames the browser sent alongside the
        # handshake, so the WebSocket has to keep reading through it.
        ws = wsproto.WebSocket(self.connection, reader=self.rfile)
        Connection(ws, self.note).run()


class Connection:
    """One browser, attached to one tab's PTY.

    Every byte the browser receives goes through one queue and one writer
    thread. Sending from the PTY pump directly would race the replayed
    history against live output and interleave the two.

    Only the tab in front is subscribed. The others keep running and keep
    filling their own history, and switching to one replays it — the same
    path, and the same code, as reconnecting after a dropped phone.
    """

    def __init__(self, ws: wsproto.WebSocket, note):
        self.ws = ws
        self.note = note
        self.out = queue.Queue(maxsize=SEND_QUEUE_DEPTH)
        self.pushed_back = None  # one item the coalescer took but could not use
        self.cols, self.rows = 80, 24
        self.tab = None
        self.dead = threading.Event()

    # -- output --

    def enqueue(self, item) -> None:
        try:
            self.out.put_nowait(item)
        except queue.Full:
            # This browser has stopped draining. Cut it loose rather than grow
            # without bound; it will reconnect and replay from history.
            self.dead.set()
            self.ws.close(1011)

    def sink(self, data) -> None:
        """Called by the PTY pump of whichever tab we are on. `None` = it exited."""
        if data is None:
            code = self.tab.session.exit_code if self.tab else None
            self.enqueue(("t", json.dumps({"t": "exit", "code": code})))
        else:
            self.enqueue(("b", data))

    def send_text(self, payload: dict) -> None:
        self.enqueue(("t", json.dumps(payload)))

    def next_item(self, timeout):
        """Next queued item, honouring anything the coalescer handed back."""
        if self.pushed_back is not None:
            item, self.pushed_back = self.pushed_back, None
            return item
        return self.out.get(timeout=timeout)

    def writer(self) -> None:
        idle = 0.0
        while not self.dead.is_set():
            try:
                item = self.next_item(POLL_INTERVAL)
            except queue.Empty:
                idle += POLL_INTERVAL
                if idle >= PING_INTERVAL:
                    idle = 0.0
                    try:
                        self.ws.ping()  # keeps idle connections from being reaped
                    except wsproto.WebSocketError:
                        break
                continue
            idle = 0.0
            if item is None:
                break

            kind, payload = item
            try:
                if kind == "t":
                    self.ws.send_text(payload)
                else:
                    # Coalesce a burst into one frame — a busy build scrolling
                    # past is thousands of tiny reads, and one frame per read
                    # is what makes a web terminal feel sluggish.
                    chunks = [payload]
                    total = len(payload)
                    while total < 256 * 1024:
                        try:
                            nxt = self.out.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is None or nxt[0] != "b":
                            self.pushed_back = nxt
                            break
                        chunks.append(nxt[1])
                        total += len(nxt[1])
                    self.ws.send_bytes(b"".join(chunks))
            except wsproto.WebSocketError:
                break
        self.dead.set()

    # -- attach --

    def attach(self, tab, restart: bool = False) -> None:
        """(Re)bind this browser to a tab's PTY and rebuild its screen.

        Switching tabs and reconnecting after a dead phone screen are the same
        thing from here: let go of whatever we were watching, make the new one
        repaint, and replay.
        """
        if tab is None:
            return
        if self.tab is not None:
            self.tab.session.unsubscribe(self.sink)
        self.tab = tab
        session = tab.session

        if restart or not session.alive:
            session.restart()
        # Sizes the PTY and forces a redraw first, so the snapshot subscribe()
        # hands back is a current screen and not a stack of stale patches.
        session.repaint(self.cols, self.rows)

        history = session.subscribe(self.sink)
        self.send_text({"t": "attach", "tab": tab.id, "alive": session.alive,
                        "cols": session.cols, "rows": session.rows})
        for i in range(0, len(history), 128 * 1024):
            self.enqueue(("b", history[i : i + 128 * 1024]))
        self.send_text({"t": "synced"})
        # A restart or a fresh tab lands somewhere new; say what it is now
        # rather than leaving the strip a poll behind.
        tabs.refresh(announce=True)

    # -- main loop --

    def run(self) -> None:
        self.note("attached")
        writer = threading.Thread(target=self.writer, daemon=True, name="ws-write")
        writer.start()
        tabs.listen(self.send_text)
        try:
            while not self.dead.is_set():
                message = self.ws.recv()
                if message is None:
                    break
                opcode, payload = message
                if opcode == wsproto.OP_BINARY:
                    # The only thing a browser sends as binary is microphone
                    # audio, and only while it holds the mic.
                    mic.feed(self, payload)
                    continue
                if opcode != wsproto.OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                self.handle(msg)
        except (wsproto.WebSocketError, OSError):
            pass
        finally:
            # A phone that drops off the network mid-sentence lets go of the
            # mic too, or the default source would never come back.
            mic.release(self)
            tabs.unlisten(self.send_text)
            if self.tab is not None:
                self.tab.session.unsubscribe(self.sink)
            self.dead.set()
            try:
                self.out.put_nowait(None)  # wake the writer; it also polls `dead`
            except queue.Full:
                pass
            self.ws.close()
            self.note("detached")

    def size_from(self, msg: dict) -> None:
        """Every message that can change which PTY we are on carries a size."""
        if "cols" in msg and "rows" in msg:
            self.cols, self.rows = int(msg["cols"]), int(msg["rows"])

    def handle(self, msg: dict) -> None:
        kind = msg.get("t")
        session = self.tab.session if self.tab else None

        if kind == "i":
            if session:
                session.write(str(msg.get("d", "")).encode("utf-8"))
        elif kind == "ib":
            # Raw bytes from xterm's binary channel (mouse reporting), which
            # are not text and must not be re-encoded as if they were.
            try:
                if session:
                    session.write(base64.b64decode(msg.get("d", ""), validate=True))
            except (ValueError, binascii.Error):
                pass
        elif kind == "resize":
            self.size_from(msg)
            if session:
                session.resize(self.cols, self.rows)
        elif kind == "hello":
            self.size_from(msg)
            # No tab list first: attach() ends by announcing one, and sending
            # it ahead of the attach would have this browser ask to switch to
            # the tab it is already being given.
            self.attach(tabs.active_tab)
        elif kind == "select":
            # Which tab is in front is shared, so this moves every browser.
            self.size_from(msg)
            tab = tabs.select(msg.get("tab"))
            if tab is not None and tab is not self.tab:
                self.attach(tab)
        elif kind == "newtab":
            self.size_from(msg)
            tab = tabs.open()
            if tab is None:
                self.note(f"refusing a {len(tabs.all()) + 1}th tab")
                # Nothing changed, but say so anyway: the browser is holding a
                # claim on a tab it asked for, and needs to hear that it is not
                # coming rather than pin it on the next thing to arrive.
                tabs.refresh(announce=True)
            else:
                self.note(f"opened tab {tab.id}")
                self.attach(tab)
        elif kind == "open":
            # A link asking for one project's agent: the tab already on it if
            # there is one, otherwise a new tab attached to it.
            self.size_from(msg)
            try:
                _, p, agent, target = attaching.resolve(str(msg.get("project") or ""),
                                                        msg.get("agent") or None)
            except attaching.AttachError as exc:
                self.send_text({"t": "notice", "text": str(exc)})
                tabs.refresh(announce=True)   # settles the browser's claim
                return
            name = attaching.tab_name(target)
            tab = tabs.named(name) if name else None
            if tab is not None:
                tabs.select(tab.id)
            else:
                tab = tabs.open(attach_command(p["name"], agent))
            if tab is None:
                self.send_text({"t": "notice", "text": "too many tabs open"})
                tabs.refresh(announce=True)
            else:
                self.note(f"tab {tab.id} on {p['name']} ({agent})")
                self.attach(tab)
        elif kind == "closetab":
            target = msg.get("tab")
            if tabs.close(target):
                self.note(f"closed tab {target}")
            # Everyone follows the broadcast onto whatever is in front now.
        elif kind == "mic":
            # Handled before the next message is read, which is what lets the
            # browser send this and then the space that starts recording: the
            # default source has already been swapped by the time Claude opens it.
            if msg.get("on"):
                try:
                    mic.hold(self, int(msg.get("rate") or 48000))
                except (MicError, ValueError) as exc:
                    self.note(f"mic unavailable: {exc}")
                    self.send_text({"t": "mic", "ok": False, "error": str(exc)})
                    return
                self.send_text({"t": "mic", "ok": True})
            else:
                mic.release(self)
        elif kind == "restart":
            self.note("restarting the session")
            self.attach(self.tab, restart=True)


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    global tabs

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=None,
                    help="defaults to $PORT, then to `proj port`")
    ap.add_argument(
        "--bind",
        default=None,
        help=f"address to listen on; defaults to the {TAILSCALE_IF} address. "
             "'any' binds 0.0.0.0 — only do that on a trusted network.",
    )
    ap.add_argument("--no-tls", action="store_true",
                    help="plain HTTP only; browsers then refuse the microphone")
    args = ap.parse_args()

    port = resolve_port(args.port)
    if port is None:
        print("No port: pass --port, set $PORT, or register this directory "
              "with `proj add`.", file=sys.stderr)
        return 1

    if args.bind is None:
        addr = interface_ip(TAILSCALE_IF)
        if addr is None:
            print(
                f"No {TAILSCALE_IF} address — is Tailscale up?\n"
                "Pass --bind ADDR (or --bind any) to listen somewhere else, "
                "knowing this serves an unauthenticated shell.",
                file=sys.stderr,
            )
            return 1
    elif args.bind == "any":
        addr = "0.0.0.0"
    else:
        addr = args.bind

    # An interactive login shell, which is exactly what sshd starts. It reads
    # ~/.profile and ~/.bashrc, and ~/.bashrc's tail runs the launcher — so the
    # browser and an SSH login are the same code path, and quitting the
    # launcher drops to the same shell either way. Every tab gets one of these,
    # which is why it is a factory and not a session.
    #
    # A tab opened on one project runs its attach first, from a login shell
    # that is not interactive — so ~/.bashrc returns before it would start the
    # launcher — and becomes the usual interactive one when you detach.
    def new_session(command: str | None = None) -> PtySession:
        if command is None:
            return PtySession(argv=LOGIN_SHELL, env=child_env(), cwd=str(Path.home()))
        return PtySession(
            argv=["/bin/bash", "-lc", f"{command}; exec /bin/bash -li"],
            env=child_env(),
            cwd=str(Path.home()),
            restart_argv=LOGIN_SHELL,
        )

    tabs = TabSet(new_session)

    # `proj down` and a closed tmux pane end this with a signal, not ^C, and
    # either way the finally below has to run: it is what gives the machine
    # its own microphone back.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))

    httpd = Server((addr, port), Handler)
    url = f"http://{addr}:{port}"
    if not args.no_tls:
        name = tailnet_name()
        try:
            if name is None:
                raise OSError("no tailnet name from `tailscale status`")
            httpd.tls = tls_context(name)
            httpd.https_origin = url = f"https://{name}:{port}"
        except (OSError, subprocess.SubprocessError, ssl.SSLError) as exc:
            # The terminal works without it; only the microphone does not.
            print(f"No TLS ({exc}) — serving plain HTTP, and voice input will "
                  "not work.", file=sys.stderr, flush=True)
    print(f"radio-room terminal on {url}  "
          f"(pty pid {tabs.active_tab.session.pid})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        mic.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
