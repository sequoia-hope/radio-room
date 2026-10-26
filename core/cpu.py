"""The CPU reservation queue (reserve/server.py), as the front ends see it.

The voice server's page, the desktop window and the operator's tools all read
and steer the queue through here. The server is found in the registry, as the
`reserve` project's web port: no port is written down anywhere else.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import registry

PROJECT = "reserve"
VERBS = ("front", "cancel", "stop", "cores")


class Unavailable(Exception):
    """The queue server isn't registered or isn't answering."""


def base() -> str:
    p = registry.find(registry.load(), PROJECT)
    if not p or not p.get("web_port"):
        raise Unavailable(f"no '{PROJECT}' project with a web port in the registry")
    return f"http://{p.get('web_host') or '127.0.0.1'}:{p['web_port']}"


def _call(path: str, body: dict | None = None, timeout: float = 5) -> dict:
    url = base() + path
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unavailable(f"the queue server at {url} isn't answering: {e}") from e


def state(timeout: float = 5) -> dict:
    """The book: entries (running and waiting, in queue order), capacity,
    booked, keep_free, paused, history, usage, load and pressure."""
    return _call("/api/state", timeout=timeout)


def act(verb: str, eid: str, body: dict | None = None) -> dict:
    if verb not in VERBS:
        raise ValueError(f"no such queue action: {verb}")
    return _call(f"/api/{verb}/{eid}", body or {}, timeout=35)


def settings(paused: bool | None = None, keep_free: int | None = None) -> dict:
    body = {k: v for k, v in (("paused", paused), ("keep_free", keep_free)) if v is not None}
    return _call("/api/settings", body)


def by_project(s: dict) -> dict[str, dict]:
    """What each project has in the queue: cores running, cores and jobs
    waiting, and its best place in line."""
    out: dict[str, dict] = {}
    for e in s.get("entries", []):
        p = out.setdefault(e["project"], {"running": 0, "waiting": 0, "jobs": 0, "position": None})
        if e["state"] == "granted":
            p["running"] += e["cores"]
        elif e["state"] == "waiting":
            p["waiting"] += e["cores"]
            p["jobs"] += 1
            if p["position"] is None or e.get("position", 99) < p["position"]:
                p["position"] = e.get("position")
    return out


def describe(s: dict) -> str:
    """The queue in a few plain lines, for the operator to read aloud."""
    run = [e for e in s["entries"] if e["state"] == "granted"]
    wait = [e for e in s["entries"] if e["state"] == "waiting"]
    head = (f"{s['booked']} of {s['capacity']} bookable cores booked, {s['keep_free']} kept free"
            + (". Granting is PAUSED: nothing new starts." if s.get("paused") else "."))
    lines = [head]
    for e in run:
        lines.append(f"running: {e['project']} {e['cores']} cores, {e['label'] or e['command'][:60]}"
                     f" [id {e['id']}]")
    for e in wait:
        lines.append(f"waiting #{e['position']}: {e['project']} {e['cores']} cores, "
                     f"{e['label'] or e['command'][:60]} [id {e['id']}]")
    if not run and not wait:
        lines.append("Nothing booked and nobody waiting.")
    return "\n".join(lines)
