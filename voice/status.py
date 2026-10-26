"""Agent status — which agents are running, what state each is in, and what
it last said — for the voice server and the desktop window alike.

Hooks in the agents write status events straight into events.db (see
events.py and hooks/). This side keeps the session map those hooks resolve
against, watches panes for what hooks can't see, gives finished turns their
rough local summaries, indexes token usage, and answers the overview.

Any number of processes may read; only one runs the monitors. `start()` takes
a machine-wide lock beside events.db in a background thread, so whichever of
the voice server and the desktop window starts first does the watching, and
the other takes over the moment it exits.

Settings are the [status] table of Radio Room's config file, and the
"summaries" and "usage" features.
"""

import fcntl
import json
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path

import events
import summarize
import usage

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "core"))
import config as radio_config  # noqa: E402
import registry as REGISTRY  # noqa: E402

DEFAULTS = {
    "status_monitor": True,
    "status_poll": 5,            # seconds between pane checks
    "status_settle": 30,         # an idle pane this long ends a "working" state
                                 # whose Stop never arrived
    # Rough one-line summaries of each finished turn, from a local model, so
    # the overview works with no cloud at all (feature "summaries").
    "summary_ollama_url": "http://127.0.0.1:11434",
    "summary_local_model": "granite4:1b",
    "summary_keep_alive": "30m",
    "summary_num_ctx": 2048,
    "summary_num_gpu": -1,       # layers on the GPU; -1 = Ollama decides, 0 = CPU only
    "summary_local_timeout": 120,
    "summary_window": 21600,     # only turns this recent (s) get summarized
    "summary_retry": 600,        # retry a failed summary after this long (s)
    # A project added this recently (s) that has never run an agent gets a
    # "new" card, until its agent starts or it is marked seen.
    "new_project_window": 259200,
    # Overrides for the default agent's tmux target, per project:
    # [status.targets.<project>] type/session/container.
    "targets": {},
}

CFG = radio_config.section("status", DEFAULTS)
CFG["summary_local"] = radio_config.feature("summaries")
CFG["usage"] = radio_config.feature("usage")
LOCK_PATH = events.DB_PATH.with_name("monitor.lock")

for _meta in REGISTRY.AGENTS.values():
    events.set_pane_patterns(_meta["type"], asking=_meta.get("pane_asking"),
                             busy=_meta.get("pane_busy"), idle=_meta.get("pane_idle"))


# ── Project index / tmux targets ─────────────────────────────────────────────

def parse_target(project_dir: str, command: str):
    """Infer the tmux target from a project's launch script."""
    try:
        script_rel = shlex.split(command)[0]
        script = (Path(project_dir) / script_rel).resolve()
        text = script.read_text()
    except (OSError, ValueError, IndexError):
        return None
    m_container = re.search(r'^CONTAINER="([^"]+)"', text, re.M)
    if m_container:
        m_sess = re.search(r'^DEFAULT_SESSION="([^"]+)"', text, re.M)
        if m_sess:
            return {"type": "docker", "container": m_container.group(1),
                    "session": m_sess.group(1)}
        return None
    m_sess = re.search(r'^SESSION="([^"]+)"', text, re.M)
    if m_sess:
        return {"type": "local", "session": m_sess.group(1)}
    return None


def project_web(entry: dict) -> dict | None:
    """The project's own web app, from the launcher index — what the third
    column shows. The host is passed through as configured and left for the
    page to resolve: "localhost" is right at the desk and useless from the
    phone, and only the browser knows which one it is."""
    port = entry.get("web_port")
    if not port:
        return None
    def one(host, prt, path, name=None):
        return {"name": name, "host": str(host or ""), "port": int(prt),
                "scheme": str(entry.get("web_scheme") or "http"),
                "path": str(path or "/")}
    main = one(entry.get("web_host"), port, "/")
    extra = [one(x.get("host") or entry.get("web_host"), x["port"],
                 x.get("path"), x.get("name"))
             for x in (entry.get("web_extra") or []) if x.get("port")]
    # The address the Projects tab and the agent cards link to: the
    # configured path, or a fixed URL when the project names one.
    link = {**one(entry.get("web_host"), port, entry.get("web_path")),
            "url": entry.get("web_url")}
    return {"desc": str(entry.get("web_desc") or ""), "main": main,
            "extra": extra, "start": str(entry.get("web_start") or ""), "link": link}


# The agents a project can run: the registry's, in its order. The first is
# the default instance, the one every older caller that says nothing about
# agents still means.
AGENTS = {a: {"type": m["type"], "label": m["label"]} for a, m in REGISTRY.AGENTS.items()}
DEFAULT_AGENT = REGISTRY.DEFAULT_AGENT


def registry_target(entry: dict, agent: str) -> dict | None:
    """The registry's target for one agent, in this module's shape."""
    try:
        t = REGISTRY.agent_target(entry, agent)
    except Exception:  # noqa: BLE001
        return None
    if not t:
        return None
    if t.get("kind") == "docker":
        return {"type": "docker", "container": t["name"],
                "session": t.get("session") or "default"}
    return {"type": "local", "session": t["name"]}


def project_instances(entry: dict, override: dict | None) -> list[dict]:
    """Every agent instance the project could have. Whether one is actually
    running is a liveness question, asked separately — a project that has only
    ever run Claude still has an (offline) Antigravity slot."""
    out = []
    for aid, meta in AGENTS.items():
        if aid == DEFAULT_AGENT:
            target = (override or registry_target(entry, aid)
                      or parse_target(entry["dir"], entry.get("command", "")))
        else:
            target = registry_target(entry, aid)
        if target:
            out.append({"id": aid, **meta, "target": target})
    return out


def load_projects() -> list[dict]:
    data = REGISTRY.load()
    overrides = CFG.get("targets") or {}
    projects = []
    for p in data.get("projects", []):
        instances = project_instances(p, overrides.get(p["name"]))
        # `target` stays the Claude Code instance's, for everything written
        # before a project could have more than one agent.
        target = next((i["target"] for i in instances if i["id"] == DEFAULT_AGENT), None)
        projects.append({"name": p["name"], "dir": p["dir"],
                         "command": p.get("command", ""), "target": target,
                         "instances": instances, "web": project_web(p),
                         "added": p.get("added"), "pinned": bool(p.get("pin_agent"))})
    return projects


def get_project(name: str) -> dict:
    for p in load_projects():
        if p["name"] == name:
            return p
    raise KeyError(f"unknown project: {name}")


def agent_id(agent: str | None) -> str:
    """Normalize what a caller named — the launcher id ("agy") or the type
    ("antigravity") — to the id. Nothing named means the default instance."""
    a = (agent or "").strip().lower()
    if not a:
        return DEFAULT_AGENT
    for aid, meta in AGENTS.items():
        if a in (aid, meta["type"]):
            return aid
    raise ValueError(f"unknown agent {agent!r} (expected one of {', '.join(AGENTS)})")


def get_instance(project: dict, agent: str | None = None) -> dict:
    aid = agent_id(agent)
    for inst in project["instances"]:
        if inst["id"] == aid:
            return inst
    raise KeyError(f"no {AGENTS[aid]['label']} target known for {project['name']}")

def target_desc(target: dict) -> str:
    if target["type"] == "docker":
        return (f"tmux session '{target['session']}' inside docker container "
                f"'{target['container']}'")
    return f"local tmux session '{target['session']}'"


# ── tmux plumbing ────────────────────────────────────────────────────────────

def tmux_argv(target: dict, args: list[str], stdin: bool = False) -> list[str]:
    if target["type"] == "docker":
        docker = ["docker", "exec"]
        if stdin:
            docker.append("-i")
        return docker + [target["container"], "tmux", *args]
    return ["tmux", *args]


def tmux_session(name: str) -> str:
    """A session target tmux will match exactly. Without the "=", tmux falls
    back to prefix matching: the project whose session is "bike" resolves to
    "bike-tracker" when no "bike" exists — reporting the wrong session alive,
    capturing the wrong terminal, and typing dispatched prompts into another
    project's agent."""
    return name if name.startswith("=") else "=" + name


def tmux_pane(name: str) -> str:
    """The same session, as a pane target. The trailing colon is what makes
    tmux read the "=" as part of a session name rather than a pane name."""
    return tmux_session(name) + ":"


def session_alive(target: dict | None) -> bool:
    if not target:
        return False
    try:
        p = subprocess.run(
            tmux_argv(target, ["has-session", "-t", tmux_session(target["session"])]),
            capture_output=True, timeout=10)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def local_sessions() -> set[str]:
    """Every local tmux session name in one call. The overview asks about every
    project at once and polls while it's open, so a `has-session` spawn per
    project — 27 of them here — is not the way to answer it."""
    try:
        p = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if p.returncode != 0:
        return set()            # no server running: nothing is alive
    return {line.strip() for line in p.stdout.splitlines() if line.strip()}


def alive_checker():
    """A liveness test for many projects. Local sessions come from one listing;
    docker ones still cost a call each, but there are usually few."""
    live = local_sessions()

    def alive(target: dict | None) -> bool:
        if not target:
            return False
        if target.get("type") == "local":
            return target.get("session") in live
        return session_alive(target)
    return alive


def send_to_project(target: dict, text: str) -> None:
    """Paste text into the target pane (bracketed paste) and press Enter."""
    text = text.rstrip("\n")
    if not text:
        raise ValueError("empty prompt")
    session = target["session"]
    p = subprocess.run(tmux_argv(target, ["load-buffer", "-b", "vp", "-"], stdin=True),
                       input=text.encode(), capture_output=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"tmux load-buffer failed: {p.stderr.decode().strip()}")
    p = subprocess.run(tmux_argv(target, ["paste-buffer", "-p", "-d", "-b", "vp",
                                          "-t", tmux_pane(session)]),
                       capture_output=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"tmux paste-buffer failed: {p.stderr.decode().strip()}")
    time.sleep(0.4)  # let the TUI finish processing the paste before submit
    p = subprocess.run(tmux_argv(target, ["send-keys", "-t", tmux_pane(session), "Enter"]),
                       capture_output=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"tmux send-keys failed: {p.stderr.decode().strip()}")


def capture_pane(target: dict | None, lines: int) -> str | None:
    if not target:
        return None
    argv = tmux_argv(target, ["capture-pane", "-p", "-J",
                              "-t", tmux_pane(target["session"]), "-S", f"-{lines}"])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.rstrip("\n")



# ── Agent status (events.py) ────────────────────────────────────────────────
#
# Hooks in Claude Code and Antigravity write status events straight into
# events.db; this side keeps the session map they resolve against, watches
# panes for what hooks can't see, and answers the overview.

_status_local = threading.local()


def status_db():
    """This thread's connection to the event store (sqlite connections don't
    cross threads)."""
    db = getattr(_status_local, "db", None)
    if db is None:
        db = _status_local.db = events.connect()
    return db


def session_owners(projects: list[dict]):
    """Each tmux session, owned by the first project that claims it. Later
    claimants — comfy-mobile on ComfyUI's session, music-lab on rust-games',
    the conversational_agent services on its own — are aliases of that agent,
    not agents of their own. Returns (rows for the session map, aliases)."""
    rows, owners, aliases = [], {}, defaultdict(list)
    for p in projects:
        for i in p["instances"]:
            key = events.session_key(i["target"])
            if key in owners:
                if owners[key] != p["name"] and p["name"] not in aliases[key]:
                    aliases[key].append(p["name"])
                continue
            owners[key] = p["name"]
            rows.append((key, p["name"], i["id"], i["type"]))
    return rows, aliases


def session_created() -> dict[str, float]:
    """Local tmux sessions and when each was created — a hook event older than
    its session can't vouch for the agent running in it now."""
    try:
        p = subprocess.run(["tmux", "list-sessions", "-F",
                            "#{session_name} #{session_created}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in p.stdout.splitlines() if p.returncode == 0 else []:
        name, _, created = line.rpartition(" ")
        try:
            out[name] = float(created)
        except ValueError:
            pass
    return out


class StatusMonitor:
    """Watches every live agent pane and turns what it sees into events.

    For everyone: a question menu or permission dialog means needs_input (agy
    fires no hook while it holds one), a shell where the agent was means it
    exited, and a "working" state whose pane has sat idle for status_settle
    seconds is over — its Stop went missing.

    For sessions hooks don't cover — inside containers, or started before the
    hooks were installed — the pane's busy/idle edges are the only signal, so
    they become prompt and stop events here."""

    def __init__(self):
        self.seen: dict[tuple[str, str], tuple[str | None, float, int]] = {}
        self.synced: list | None = None

    def run(self) -> None:
        poll = max(1.0, float(CFG["status_poll"]))
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a monitor must never take the server down
                traceback.print_exc()
            time.sleep(poll)

    def tick(self) -> None:
        db = status_db()
        projects = load_projects()
        rows, _ = session_owners(projects)
        if rows != self.synced:
            events.sync_sessions(db, rows)
            self.synced = rows
        created = session_created()
        stored = {(r["project"], r["instance"]): r
                  for r in db.execute("SELECT * FROM instances")}
        insts = {(p["name"], i["id"]): i for p in projects for i in p["instances"]}
        for key, project, aid, atype in rows:
            target = insts[(project, aid)]["target"]
            if target["type"] == "local" and target["session"] not in created:
                self.seen.pop((project, aid), None)
                continue
            text = capture_pane(target, 40)
            if text is None:
                self.seen.pop((project, aid), None)
                continue
            self.observe(db, project, aid, atype, key, text,
                         events.classify_pane(atype, text),
                         stored.get((project, aid)),
                         created.get(target["session"]) if target["type"] == "local" else None)

    def observe(self, db, project, aid, atype, session, text, cls, row, created):
        now = time.time()
        prev, since, count = self.seen.get((project, aid), (None, now, 0))
        if cls == prev:
            count += 1
        else:
            since, count = now, 1
        self.seen[(project, aid)] = (cls, since, count)
        state = row["state"] if row else None
        covered = bool(row and row["last_hook"] and created and row["last_hook"] >= created)

        def emit(event, **kw):
            events.record(db, event=event, source="pane", project=project,
                          instance=aid, agent_type=atype, session=session,
                          pane=text, **kw)

        if cls is None:
            return
        if state == "offline" and cls != "shell":
            emit("revived")
            state = "idle"
        if cls == "asking":
            if state != "needs_input":
                emit("asking")
        elif cls == "shell":
            if state != "offline":
                emit("exited")
        elif cls == "busy":
            # Covered sessions get their prompt from a hook; the pane only
            # corrects a state that is plainly wrong, and only once it has
            # held for two polls, so the last frame of a turn whose Stop just
            # landed can't flip it back to working.
            if state != "working" and (not covered or count >= 2 or prev == "asking"):
                emit("busy")
        elif cls == "idle":
            if prev == "asking" and state == "needs_input":
                emit("menu_closed")
            elif state == "working":
                quiet = now - max(since, row["updated"] if row else 0)
                if (not covered and prev == "busy") or quiet >= float(CFG["status_settle"]):
                    emit("settled", message=events.pane_body(text),
                         detail={"background_tasks": events.pane_background(text)})


def state_reason(cause) -> str | None:
    """What put an instance in its state, in words, when the final message
    doesn't already say: a waiting card with no question of its own."""
    if not cause:
        return None
    if cause["event"] == "asking":
        return "A menu or prompt is open in the terminal"
    if cause["event"] == "notify":
        detail = json.loads(cause["detail"]) if cause["detail"] else {}
        # "Claude is waiting for your input" only restates the state.
        if detail.get("kind") == "idle_prompt":
            return None
        return detail.get("text") or "The agent is waiting for you"
    return None

def instances_payload() -> dict:
    """Every agent worth a card: running ones, dead ones still holding
    something you haven't seen, and projects added lately that have never run
    one — and every pinned project's, whatever it is doing. Pinned first, then
    most recently changed: a card's time is its last change of state (marking
    it seen doesn't count), else when its session started, else when the
    project was added."""
    db = status_db()
    projects = load_projects()
    rows, aliases = session_owners(projects)
    aliases_by_owner: dict[str, list[str]] = defaultdict(list)
    for key, project, _, _ in rows:
        for al in aliases.get(key, []):
            if al not in aliases_by_owner[project]:
                aliases_by_owner[project].append(al)
    usnap = usage.snapshot(db, aliases_by_owner)
    stored = {(r["project"], r["instance"]): r
              for r in db.execute("SELECT * FROM instances")}
    insts = {(p["name"], i["id"]): i for p in projects for i in p["instances"]}
    alive = alive_checker()
    created = session_created()
    added = {p["name"]: p["added"] for p in projects if p.get("added")}
    pinned = {p["name"] for p in projects if p["pinned"]}
    webs = {p["name"]: p["web"]["link"] for p in projects if p["web"]}
    web_up: dict[str, bool] = {}
    now = time.time()
    out = []
    for key, project, aid, atype in rows:
        inst = insts[(project, aid)]
        row = stored.get((project, aid))
        is_alive = alive(inst["target"])
        state = row["state"] if row else "idle"
        pending = state in events.DISMISSABLE
        fresh = (not is_alive and row is None and aid == DEFAULT_AGENT
                 and now - added.get(project, 0) < float(CFG["new_project_window"])
                 and not any(r[0] == project for r in stored))
        pin = project in pinned
        if fresh:
            state = "new"
        elif not is_alive and not pending:
            # A pinned project keeps its card with no session; one per project.
            if not (pin and aid == DEFAULT_AGENT):
                continue
            state = "offline"
        changed = db.execute(
            "SELECT MAX(ts) FROM events WHERE project = ? AND instance = ? "
            "AND state IS NOT NULL AND event != 'dismiss'", (project, aid)).fetchone()[0]
        recent = changed or (created.get(inst["target"].get("session"))
                             if inst["target"]["type"] == "local" else None) \
            or added.get(project) or 0
        last = db.execute(
            "SELECT * FROM events WHERE project = ? AND instance = ? "
            "AND message IS NOT NULL AND message != '' "
            "ORDER BY id DESC LIMIT 1", (project, aid)).fetchone()
        web = webs.get(project)
        if web and project not in web_up:
            web_up[project] = REGISTRY.is_up(web["port"], web["host"] or "127.0.0.1")
        badges = json.loads(row["detail"]) if row and row["detail"] else {}
        cause = db.execute("SELECT event, source, detail FROM events WHERE id = ?",
                           (row["last_event"],)).fetchone() \
            if row and row["last_event"] else None
        out.append({
            "project": project, "agent": aid, "type": atype,
            "label": inst["label"], "session": key, "alive": is_alive,
            "aliases": aliases.get(key, []),
            "state": state, "since": row["since"] if row else None,
            "updated": row["updated"] if row else None,
            "recent": recent, "pinned": pin,
            "hooked": bool(row and row["last_hook"]),
            "background_tasks": badges.get("background_tasks", 0),
            "reason": state_reason(cause),
            "usage": usnap["by_instance"].get((project, aid)),
            "web": web and {**web, "up": web_up[project]},
            "message": {"event": last["id"], "ts": last["ts"],
                        "excerpt": events.turn_text(last)[:400],
                        "transcript": last["transcript_path"],
                        # Rough: from a small local model (or Sonnet when asked),
                        # always shown as such and beside the full message.
                        "summary": events.summary_for(db, last["id"]),
                        "summary_pending": bool(CFG.get("summary_local"))
                            and last["state"] in events.DISMISSABLE
                            and events.summary_for(db, last["id"]) is None
                            and last["ts"] > time.time() - float(CFG["summary_window"]),
                        } if last else None,
        })
    out.sort(key=lambda x: (not x["pinned"], -x["recent"]))
    return {"instances": out, "services": usnap["services"] if CFG.get("usage") else {},
            "project_usage": usnap["by_project"], "now": time.time()}


def event_payload(eid: int) -> dict:
    row = status_db().execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if not row:
        raise KeyError(f"no event {eid}")
    out = dict(row)
    out["detail"] = json.loads(out["detail"]) if out["detail"] else {}
    out["message"] = events.turn_text(row)
    out["summary"] = events.summary_for(status_db(), eid)
    return out


def dismiss_instance(project: str, agent: str | None) -> dict:
    p = get_project(project)
    inst = get_instance(p, agent)
    res = events.record(status_db(), event="dismiss", source="app",
                        project=p["name"], instance=inst["id"],
                        agent_type=inst["type"],
                        session=events.session_key(inst["target"]))
    return {"project": p["name"], "agent": inst["id"], "state": res["state"]}

PIN_KEYS = {"agents": "pin_agent", "projects": "pin_project"}

def set_pin(project: str, view: str, pinned: bool) -> dict:
    """Pin a project to the top of the Agents view or the Projects view, or
    unpin it; the two are independent. Kept on its registry entry, so every
    front end sees the same pins."""
    if view not in PIN_KEYS:
        raise KeyError(f"no view {view!r} to pin in")
    cfg = REGISTRY.load()
    p = REGISTRY.find(cfg, get_project(project)["name"])
    if pinned:
        p[PIN_KEYS[view]] = True
    else:
        p.pop(PIN_KEYS[view], None)
    REGISTRY.save(cfg)
    return {"project": p["name"], "view": view, "pinned": pinned}

class Summarizer:
    """Gives each agent's newest finished turn a rough one-line summary from
    the local model. One at a time, newest first: a small model on the CPU
    answers in a few seconds, and the overview only ever shows an agent's latest
    turn, so older ones that were overtaken are left alone.

    It polls the store rather than being told, because the hooks write there
    directly and never talk to this server.

    A failure backs off, doubling up to half an hour: a model that times out
    has burned its whole timeout of CPU, and going straight on to the next
    agent's turn would only burn the next one too."""

    def __init__(self) -> None:
        self.failures = 0

    def run(self) -> None:
        while True:
            try:
                if not self.step():
                    time.sleep(3)
            except Exception:  # noqa: BLE001 — never take the server down
                traceback.print_exc()
                time.sleep(10)
            if self.failures:
                time.sleep(min(30 * 2 ** (self.failures - 1), 1800))

    def step(self) -> bool:
        db = status_db()
        now = time.time()
        row = db.execute(
            """SELECT e.* FROM events e
               JOIN (SELECT project, instance, MAX(id) AS id FROM events
                     WHERE state IN ('completed', 'needs_input', 'error')
                       AND (message != '' OR pane != '') AND project IS NOT NULL
                       AND ts > ? GROUP BY project, instance) latest
                 ON e.id = latest.id
               LEFT JOIN summaries s ON s.event_id = e.id AND s.source = 'granite'
               WHERE s.event_id IS NULL OR (s.error IS NOT NULL AND s.ts < ?)
               ORDER BY e.id DESC LIMIT 1""",
            (now - float(CFG["summary_window"]), now - float(CFG["summary_retry"]))).fetchone()
        if row is None:
            return False
        label = AGENTS.get(row["instance"], {}).get("label", row["instance"])
        try:
            out = summarize.summarize(CFG, row["project"], label,
                                      events.turn_text(row), row["pane"] or "")
            events.save_summary(db, row["id"], "granite", model=out["model"],
                                status=out["status"], summary=out["summary"],
                                ms=out["ms"])
        except summarize.SummaryError as e:
            events.save_summary(db, row["id"], "granite",
                                model=CFG["summary_local_model"], error=str(e))
            self.failures += 1
            print(f"summary for event {row['id']} failed ({self.failures} in a row): {e}",
                  file=sys.stderr)
            return False
        self.failures = 0
        return True

class UsageMonitor:
    """Indexes Claude Code and Antigravity transcripts for per-project token
    usage and refreshes each service's remaining quota/credits in events.db."""

    def run(self) -> None:
        while True:
            try:
                db = status_db()
                usage.sync_transcripts(db, load_projects())
                usage.sync_quotas(db)
            except Exception:  # noqa: BLE001 — never take the server down
                traceback.print_exc()
            time.sleep(15)


def _run_monitors() -> None:
    db = status_db()
    events.prune(db)
    usage.prune(db)
    threading.Thread(target=StatusMonitor().run, daemon=True,
                     name="status-monitor").start()
    if CFG.get("usage"):
        threading.Thread(target=UsageMonitor().run, daemon=True,
                         name="usage-monitor").start()
    if CFG.get("summary_local"):
        threading.Thread(target=Summarizer().run, daemon=True,
                         name="summarizer").start()


def start() -> None:
    """Run the monitors in this process once it holds the machine-wide lock —
    now, if no one else is, or whenever the holder exits. The lock is the open
    file, held until this process ends."""
    if not CFG.get("status_monitor"):
        return

    def wait_and_run():
        lock = open(LOCK_PATH, "a")
        fcntl.flock(lock, fcntl.LOCK_EX)      # blocks while another process monitors
        _run_monitors()
        threading.Event().wait()              # keep `lock` referenced, forever

    threading.Thread(target=wait_and_run, daemon=True, name="monitor-lock").start()
