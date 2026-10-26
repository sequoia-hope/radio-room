#!/usr/bin/env python3
"""reserve — a reservation queue for this machine's CPU.

    reserve/server.py [--port N] [--bind local] [--keep-free 4]

Heavy jobs (builds, test suites, model runs) ask for a number of cores
before they start, through the `reserve` client beside this file, and wait
their turn when the machine is booked. The book is strict first come, first
served: the head of the queue starts as soon as its cores are free, and
nothing behind it jumps ahead. `keep_free` cores are never booked, so the
desktop, the voice operator and the agents themselves always have room.

Every holder and waiter checks in every few seconds; one that goes quiet for
LAPSE seconds (its client was killed) loses its place, so a crashed job never
holds cores. The book is saved to disk as it changes, so a restart keeps every
booking's id and place; a client the saved book doesn't know (it was lost)
re-books on its next check-in.

The page at / shows the book, what each project is actually using, booked or
not, and the jobs that finished lately. It can move a waiter to the front,
shrink or cancel it, stop a running job, pause granting, and change how many
cores are kept free. Radio Room's page and window do the same through
/api/state and the POSTs below. /client serves the
client script, so a container can fetch it from here.

Stdlib only.
"""

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "core"))
import config as radio_config  # noqa: E402
import listen  # noqa: E402
import registry  # noqa: E402

LAPSE = 20          # seconds without a check-in before a booking is dropped
SAMPLE = 5          # seconds between usage samples


# ── the book ─────────────────────────────────────────────────────────────────

HISTORY = 40        # finished jobs kept for the page


class Book:
    """The queue. Saved to `path` whenever it changes (not on check-ins), so
    a restart keeps everyone's id and place in line: a client that checks in
    after the restart finds its booking still there."""

    def __init__(self, total: int, keep_free: int, path: Path | None = None):
        self.total = total
        self.keep_free = keep_free
        self.paused = False
        self.path = path
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}   # insertion order is queue order
        self.history: list[dict] = []        # newest first
        self.ids = itertools.count(1)
        self.boot = f"{time.time_ns():x}"  # ids from an earlier run never collide

    @property
    def capacity(self) -> int:
        return max(1, self.total - self.keep_free)

    # ── on disk ──

    def load(self, keep_free: int | None) -> None:
        """Take up the book an earlier run saved. Every booking gets a fresh
        LAPSE to check in before it is dropped. `keep_free`, when given on the
        command line, overrides the saved one."""
        try:
            saved = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            saved = {}
        now = time.time()
        with self.lock:
            for e in saved.get("entries", []):
                if e.get("state") in ("waiting", "granted"):
                    self.entries[e["id"]] = {**e, "seen": now}
            self.history = saved.get("history", [])[:HISTORY]
            self.paused = bool(saved.get("paused"))
            if keep_free is not None:
                self.keep_free = keep_free
            elif "keep_free" in saved:
                self.keep_free = int(saved["keep_free"])
            self._schedule()

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": list(self.entries.values()),
                                   "history": self.history, "paused": self.paused,
                                   "keep_free": self.keep_free}))
        tmp.replace(self.path)

    # ── what clients do ──

    def request(self, req: dict) -> dict:
        cores = max(1, min(int(req.get("cores") or 1), self.capacity))
        now = time.time()
        with self.lock:
            eid = f"{self.boot}-{next(self.ids)}"
            e = {"id": eid, "project": req.get("project") or "unknown",
                 "cores": cores, "asked": int(req.get("cores") or 1),
                 "label": (req.get("label") or "")[:120],
                 "command": (req.get("command") or "")[:300],
                 "cwd": req.get("cwd") or "", "where": req.get("where") or "",
                 "session": req.get("session") or "",
                 "unit": req.get("unit") or eid,   # its systemd scope is reserve-<unit>
                 "state": "waiting", "requested": float(req.get("requested") or now),
                 "granted": None, "seen": now}
            if req.get("resume"):   # a job that was running before a restart
                e.update(state="granted", granted=now)
            self.entries[eid] = e
            if req.get("requested") and not req.get("resume"):
                self._requeue(eid)
            self._schedule()
            return self._view(e)

    def checkin(self, eid: str) -> dict | None:
        with self.lock:
            e = self.entries.get(eid)
            if e is None:
                return None
            e["seen"] = time.time()
            return self._view(e)

    def release(self, eid: str, code: int | None = None) -> None:
        with self.lock:
            e = self.entries.pop(eid, None)
            if e and e["state"] == "granted":
                self._finish(e, "stopped" if e.get("stop") else "done", code)
            self._schedule()

    # ── what the page (and the operator) do ──

    def front(self, eid: str) -> bool:
        with self.lock:
            e = self.entries.get(eid)
            if e is None or e["state"] != "waiting":
                return False
            rest = {k: v for k, v in self.entries.items() if k != eid}
            granted = {k: v for k, v in rest.items() if v["state"] == "granted"}
            waiting = {k: v for k, v in rest.items() if v["state"] != "granted"}
            self.entries = {**granted, eid: e, **waiting}
            self._schedule()
            return True

    def cancel(self, eid: str) -> bool:
        """A waiter only; a running job is stopped (stop), not cancelled."""
        with self.lock:
            e = self.entries.get(eid)
            if e is None or e["state"] != "waiting":
                return False
            e["state"] = "cancelled"
            self._finish(e, "cancelled", None)
            self._schedule()
            return True

    def stop(self, eid: str) -> dict | None:
        """Ask a running job to stop. Its client sees `stop` at its next
        check-in and terminates the command; on the host the caller also stops
        its scope, which works for clients too old to know about `stop`."""
        with self.lock:
            e = self.entries.get(eid)
            if e is None or e["state"] != "granted":
                return None
            e["stop"] = True
            self._save()
            return dict(e)

    def resize(self, eid: str, cores: int) -> bool:
        """Change what a waiter asks for — shrink a big job so it fits sooner."""
        with self.lock:
            e = self.entries.get(eid)
            if e is None or e["state"] != "waiting":
                return False
            e["cores"] = max(1, min(int(cores), self.capacity))
            self._schedule()
            return True

    def settings(self, paused: bool | None = None, keep_free: int | None = None) -> None:
        """Pause granting (running jobs carry on), or change how many cores
        are never booked. A smaller capacity never takes cores back from a
        running job; it only holds the next grant until enough are free."""
        with self.lock:
            if paused is not None:
                self.paused = bool(paused)
            if keep_free is not None:
                self.keep_free = max(0, min(int(keep_free), self.total - 1))
            self._schedule()

    # ── housekeeping ──

    def reap(self) -> None:
        cutoff = time.time() - LAPSE
        with self.lock:
            gone = [k for k, v in self.entries.items() if v["seen"] < cutoff]
            for eid in gone:
                e = self.entries.pop(eid)
                if e["state"] != "cancelled":
                    self._finish(e, "lapsed", None)
            if gone:
                self._schedule()

    def _finish(self, e: dict, how: str, code: int | None) -> None:
        self.history.insert(0, {k: e.get(k) for k in (
            "id", "project", "label", "command", "cores", "requested", "granted",
            "where", "session")} | {"ended": time.time(), "how": how, "code": code})
        del self.history[HISTORY:]

    def _requeue(self, eid: str) -> None:
        """Put a re-queued waiter back where its first request put it: before
        the first waiter that asked later."""
        e = self.entries.pop(eid)
        out, placed = {}, False
        for k, v in self.entries.items():
            if not placed and v["state"] == "waiting" and v["requested"] > e["requested"]:
                out[eid] = e
                placed = True
            out[k] = v
        if not placed:
            out[eid] = e
        self.entries = out

    def _booked(self) -> int:
        return sum(v["cores"] for v in self.entries.values() if v["state"] == "granted")

    def _schedule(self) -> None:
        """Grant from the head of the line, then save. Strict FIFO: nobody
        passes the head, even when a smaller job behind it would fit."""
        free = self.capacity - self._booked()
        for e in self.entries.values():
            if self.paused:
                break
            if e["state"] != "waiting":
                continue
            if e["cores"] > free:
                break
            e.update(state="granted", granted=time.time())
            free -= e["cores"]
        self._save()

    def _view(self, e: dict) -> dict:
        out = dict(e)
        if e["state"] == "waiting":
            waiting = [v for v in self.entries.values() if v["state"] == "waiting"]
            out["position"] = waiting.index(e) + 1
            out["ahead"] = [{"project": v["project"], "cores": v["cores"], "label": v["label"]}
                            for v in waiting[:waiting.index(e)]]
            out["running"] = [{"project": v["project"], "cores": v["cores"], "label": v["label"]}
                              for v in self.entries.values() if v["state"] == "granted"]
            out["paused"] = self.paused
        return out

    def snapshot(self) -> dict:
        with self.lock:
            return {"capacity": self.capacity, "booked": self._booked(),
                    "total": self.total, "keep_free": self.keep_free,
                    "paused": self.paused, "history": list(self.history),
                    "entries": [self._view(e) for e in self.entries.values()
                                if e["state"] != "cancelled"]}


def stop_scope(e: dict) -> bool:
    """Stop a host job's systemd scope: SIGTERM, then SIGKILL if it lingers.
    False when there is none (a container job, or a host without systemd) —
    its client stops it at its next check-in instead."""
    if e.get("where") != "host":
        return False
    done = subprocess.run(["systemctl", "--user", "stop", "--no-block",
                           f"reserve-{e.get('unit') or e['id']}.scope"],
                          capture_output=True, text=True, timeout=30)
    return done.returncode == 0


# ── who is using the CPU, booked or not ─────────────────────────────────────

class Usage:
    """Per-project CPU over the last SAMPLE seconds: a process belongs to a
    container by its cgroup, otherwise to the registered project its cwd is
    under. What the page shows beside the book, so an unbooked hog is
    visible."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest: dict = {"projects": [], "total": 0.0}
        self.hz = os.sysconf("SC_CLK_TCK")

    def run(self) -> None:
        prev = self._ticks()
        while True:
            time.sleep(SAMPLE)
            try:
                cur = self._ticks()
                self._sample(prev, cur)
                prev = cur
            except Exception as e:  # noqa: BLE001 — keep sampling
                print(f"usage sample failed: {e}", file=sys.stderr)

    @staticmethod
    def _ticks() -> dict[int, int]:
        out = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                s = Path(f"/proc/{d}/stat").read_text()
                f = s[s.rindex(")") + 2:].split()
                out[int(d)] = int(f[11]) + int(f[12])
            except (OSError, ValueError):
                pass
        return out

    def _sample(self, prev: dict, cur: dict) -> None:
        cfg = registry.load()
        names = containers()
        by: dict[str, float] = {}
        tops: dict[str, dict[str, float]] = {}
        for pid, t in cur.items():
            used = (t - prev.get(pid, t)) / self.hz / SAMPLE
            if used <= 0.01:
                continue
            owner = self._owner(pid, cfg, names)
            by[owner] = by.get(owner, 0) + used
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                comm = "?"
            t2 = tops.setdefault(owner, {})
            t2[comm] = t2.get(comm, 0) + used
        rows = [{"project": k, "cores": round(v, 1),
                 "top": [[c, round(x, 1)] for c, x in
                         sorted(tops[k].items(), key=lambda kv: -kv[1])[:3]]}
                for k, v in sorted(by.items(), key=lambda kv: -kv[1]) if v >= 0.1]
        with self.lock:
            self.latest = {"projects": rows, "total": round(sum(by.values()), 1)}

    @staticmethod
    def _owner(pid: int, cfg: dict, names: dict) -> str:
        try:
            cg = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError:
            return "other"
        m = re.search(r"docker-([0-9a-f]{64})", cg)
        if m:
            return names.get(m.group(1), {}).get("project") or "container"
        # An agent's tools often run from a scratch directory in /tmp, so walk
        # up to the first ancestor whose cwd is in a project (the agent).
        for _ in range(20):
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                return "system"   # another user's process: root, ollama, …
            found = registry.projects_for_dir(cfg, cwd)
            if found:
                return found[0]["name"]
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                pid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError):
                break
            if pid <= 1:
                break
        return "other"

    def get(self) -> dict:
        with self.lock:
            return dict(self.latest)


_containers: dict = {"at": 0.0, "by_id": {}}


def containers() -> dict:
    """Running containers by id: name, IPs and mounts, so a request from a
    container's address and a process in its cgroup can both be traced to a
    project through the host directory it mounts. Cached a minute."""
    if time.time() - _containers["at"] < 60:
        return _containers["by_id"]
    by_id = {}
    try:
        ids = subprocess.run(["docker", "ps", "-q", "--no-trunc"], capture_output=True,
                             text=True, timeout=10).stdout.split()
        if ids:
            info = json.loads(subprocess.run(["docker", "inspect", *ids], capture_output=True,
                                             text=True, timeout=10).stdout)
            cfg = registry.load()
            for c in info:
                mounts = [(m["Destination"], m["Source"]) for m in c.get("Mounts", [])]
                ips = [n.get("IPAddress") for n in
                       (c.get("NetworkSettings", {}).get("Networks") or {}).values()]
                project = container_project(cfg, c, mounts)
                by_id[c["Id"]] = {"name": c["Name"].lstrip("/"), "ips": [i for i in ips if i],
                                  "mounts": mounts, "project": project}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    _containers.update(at=time.time(), by_id=by_id)
    return by_id


def container_project(cfg: dict, c: dict, mounts: list) -> str | None:
    """The project a container belongs to. Not simply the first project dir
    it mounts: one may also mount this repo's voice/ for its hooks."""
    name = c["Name"].lstrip("/")
    labels = (c.get("Config") or {}).get("Labels") or {}
    compose_dir = labels.get("com.docker.compose.project.working_dir")
    if compose_dir:
        found = registry.projects_for_dir(cfg, compose_dir)
        if found:
            return found[0]["name"]
    for p in cfg["projects"]:   # e.g. web_start = "docker start kokoro"
        if re.search(rf"\b{re.escape(name)}\b", f"{p.get('web_start', '')} {p.get('web_stop', '')}"):
            return p["name"]
    for _dst, src in mounts:
        found = registry.projects_for_dir(cfg, src)
        if found:
            return found[0]["name"]
    return None


def project_for(cwd: str, addr: str, session: str = "") -> tuple[str | None, str]:
    """(project, where) for a request: the cwd it was made from, translated
    through the container's mounts when it came from a container. On the host
    a cwd outside every project falls back to the same place in the repo's
    main checkout (an unregistered worktree), then to the tmux session the
    client ran in."""
    cfg = registry.load()
    for c in containers().values():
        if addr in c["ips"]:
            for dst, src in sorted(c["mounts"], key=lambda m: -len(m[0])):
                if cwd == dst or cwd.startswith(dst.rstrip("/") + "/"):
                    found = registry.projects_for_dir(cfg, src + cwd[len(dst):])
                    if found:
                        return found[0]["name"], c["name"]
            return c["project"], c["name"]
    found = registry.projects_for_dir(cfg, cwd) if cwd else []
    if not found and cwd:
        main = main_checkout(cwd)
        found = registry.projects_for_dir(cfg, main) if main else []
    if not found and session:
        found = [p for p in cfg["projects"] for a in registry.AGENTS
                 if (registry.agent_target(p, a) or {}).get("kind") == "tmux"
                 and registry.agent_target(p, a)["name"] == session][:1]
    return (found[0]["name"] if found else None), "host"


def main_checkout(cwd: str) -> str | None:
    """Where `cwd` would be in its repository's main checkout, when it is in
    a linked worktree: ~/x-worktrees/feature/games/y → ~/x/games/y."""
    try:
        done = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute",
                               "--git-common-dir", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = done.stdout.split()
    if done.returncode != 0 or len(lines) != 2 or not lines[0].endswith("/.git"):
        return None
    main, top = lines[0][:-len("/.git")], lines[1]
    if main == top:
        return None
    return main + cwd[len(top):] if cwd.startswith(top) else main


def pressure() -> dict:
    out = {"load": os.getloadavg()[0], "cores": os.cpu_count()}
    try:
        m = re.search(r"some avg10=([\d.]+)", Path("/proc/pressure/cpu").read_text())
        out["pressure"] = float(m.group(1)) if m else None
    except OSError:
        out["pressure"] = None
    return out


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    book: Book
    usage: Usage

    def log_message(self, *_):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._file(HERE / "static" / "index.html", "text/html; charset=utf-8")
        if self.path == "/client":
            return self._file(HERE / "reserve", "text/x-python; charset=utf-8")
        if self.path == "/api/state":
            return self._json({**self.book.snapshot(), "usage": self.usage.get(),
                               **pressure(), "now": time.time()})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        parts = self.path.strip("/").split("/")
        if parts == ["api", "request"]:
            if not body.get("project"):
                body["project"], body["where"] = project_for(body.get("cwd", ""),
                                                             self.client_address[0],
                                                             body.get("session") or "")
            elif not body.get("where"):
                body["where"] = project_for("", self.client_address[0])[1]
            return self._json(self.book.request(body))
        if parts == ["api", "settings"]:
            keep = body.get("keep_free")
            self.book.settings(paused=body.get("paused"),
                               keep_free=int(keep) if keep is not None else None)
            return self._json(self.book.snapshot())
        if len(parts) == 3 and parts[0] == "api":
            verb, eid = parts[1], parts[2]
            if verb == "checkin":
                e = self.book.checkin(eid)
                return self._json(e) if e else self._json({"error": "unknown"}, 404)
            if verb == "release":
                code = body.get("code")
                self.book.release(eid, int(code) if isinstance(code, int) else None)
                return self._json({"ok": True})
            if verb == "stop":
                e = self.book.stop(eid)
                if e is None:
                    return self._json({"ok": False})
                return self._json({"ok": True, "scope": stop_scope(e)})
            if verb == "cores":
                return self._json({"ok": self.book.resize(eid, int(body.get("cores") or 0))})
            if verb == "front":
                return self._json({"ok": self.book.front(eid)})
            if verb == "cancel":
                return self._json({"ok": self.book.cancel(eid)})
        self._json({"error": "not found"}, 404)


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


def publish_client(port: int) -> None:
    """Put the client where every agent can run it as ~/.claude/bin/reserve,
    with this run's port beside it. ~/.claude is the directory a host and
    its Claude Code containers usually share (mounted), and a container has no
    `proj` to ask. Rewritten at every start, so it follows the registry."""
    shared = Path.home() / ".claude"
    (shared / "bin").mkdir(parents=True, exist_ok=True)
    tmp = shared / "bin" / ".reserve.tmp"
    tmp.write_bytes((HERE / "reserve").read_bytes())
    tmp.chmod(0o755)
    tmp.replace(shared / "bin" / "reserve")
    (shared / "reserve.json").write_text(json.dumps({"port": port}) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=None,
                    help="defaults to $PORT, then to `proj port`")
    ap.add_argument("--bind", default=None,
                    help="local, tailscale, any, or an address (default: [reserve] bind "
                         "in the config, else local). Agents in containers reach the "
                         "server through the docker gateway, which takes 'any'.")
    ap.add_argument("--keep-free", type=int, default=None,
                    help="cores never booked, for the desktop and the agents "
                         "(default: what the page last set, else 4)")
    ap.add_argument("--book", type=Path, default=Path.home() / ".local/state/reserve/book.json",
                    help="where the queue is saved between runs")
    args = ap.parse_args()
    port = resolve_port(args.port)
    if port is None:
        print("No port: pass --port, set $PORT, or register this directory "
              "with `proj add`.", file=sys.stderr)
        return 1

    bind = args.bind or radio_config.section("reserve", {"bind": "local"})["bind"]
    try:
        addrs = listen.addresses(bind)
    except listen.BindError as e:
        print(f"reserve: {e}", file=sys.stderr)
        return 1

    publish_client(port)
    Handler.book = Book(os.cpu_count() or 1, 4, args.book)
    Handler.book.load(args.keep_free)
    Handler.usage = Usage()

    def reaper():
        while True:
            time.sleep(2)
            Handler.book.reap()

    threading.Thread(target=reaper, daemon=True, name="reaper").start()
    threading.Thread(target=Handler.usage.run, daemon=True, name="usage").start()
    servers = [ThreadingHTTPServer((addr, port), Handler) for addr in addrs]
    for srv in servers:
        srv.daemon_threads = True
    print(f"reserve: {Handler.book.capacity} bookable cores, port {port} on "
          f"{', '.join(addrs)}", flush=True)
    if listen.exposed(addrs):
        print("reserve: its queue (cancel, stop, pause) is open to everyone on that network",
              file=sys.stderr, flush=True)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
