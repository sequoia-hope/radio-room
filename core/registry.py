#!/usr/bin/env python3
"""Central project + port registry.

The project list (projects.toml, wherever core/config.py says it is) is the
single source of truth for the projects on a machine: which directory, which
tmux session runs each agent, and which port serves its web instance. The
desktop window, the voice server, the hub and `proj` all read it here.

Schema
------
    [hub]
    port     = 9000            # the dashboard itself
    bind     = "local"         # local, tailscale, any, or an address (core/listen.py)
    pool     = [8010, 8099]    # inclusive range the allocator draws from
    reserved = [8035]          # ports inside the pool never to hand out

    [[projects]]
    name      = "magnet"
    dir       = "~/src/magnet"
    command   = "./launch.sh"     # optional: a launch script naming its session
    tmux      = "magnet"          # optional: the agent session, said outright
    web_port  = 8010              # allocated from the pool, or adopted (see fixed)
    web_fixed = false             # true => the project owns this port itself
    web_start = "python3 serve.py $PORT"   # how the hub brings the web app up
    web_stop  = "systemctl --user stop x"  # only if another supervisor owns it
    web_desc  = "Planar maglev motor simulator"
    web_path  = "/"               # path appended when building the URL
    web_host  = "10.0.0.5"        # only when the service binds one specific IP
    web_scheme = "https"          # default http
    web_url   = "https://x.example"  # public URL of a proxy in front of web_port
    web_extra = [{ name = "mobile", port = 9190, path = "/" }]
    pin_agent   = true            # its card stays first in the Agents view
    pin_project = true            # first in the Projects view (independent)

Only `name` and `dir` are required. A project with neither `command` nor
`tmux` gets a session named after it.

Two classes of port, kept honestly distinct:

  allocated (web_fixed = false)
      The registry owns the number. The hub passes it to the process as $PORT
      and as the PORT environment variable, so `web_start` must honour it.

  adopted (web_fixed = true)
      The project hardcodes the port for its own reasons — a tool's well-known
      default, or ports published from a docker-compose file. The registry
      records the number so the dashboard can link to it, and the allocator
      knows never to reuse it, but nothing is rewritten.

`web_url` changes where the dashboard's link points, and nothing else. When a
proxy fronts a project — tailscale serve terminating TLS on 443, say, so a
phone gets a real certificate instead of a self-signed warning — that URL is
the one to click, but it is emphatically not the project's port: `web_port`
stays the socket the app itself binds, so the health check still says whether
the *app* is up rather than the proxy, the allocator still reserves the right
number, and stop_web still kills the right listener. Registering the proxy's
port as `web_port` would aim all three at the proxy — with 443 that means Stop
killing tailscaled.

A project with no `web_start` is link-only: the dashboard shows its status but
offers no start/stop, because something outside this registry owns its lifecycle.

A project *with* `web_stop` is externally supervised: some other thing — a
systemd user unit, a pair of hand-rolled start/stop scripts — already owns the
process. The registry runs those commands directly instead of wrapping the
server in a tmux session it would then have to kill, which would only fight the
real supervisor (systemd would restart what we killed).
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import config as radio_config  # noqa: E402
import git  # noqa: E402
import jira  # noqa: E402

CONFIG_PATH = radio_config.projects_path()

DEFAULT_HUB = {
    "port": 8000,
    "bind": "local",       # core/listen.py: local, tailscale, any, or an address
    "pool": [8010, 8099],
    "reserved": [],
}

# tmux session that the hub creates for a project's web instance. Prefixed so a
# web server never collides with the agent session of the same name.
WEB_SESSION_PREFIX = "web-"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ── Config I/O ───────────────────────────────────────────────────────────────

def load() -> dict:
    """Read the project list, filling in hub defaults for keys that are absent."""
    if not CONFIG_PATH.exists():
        return {"hub": copy.deepcopy(DEFAULT_HUB), "projects": []}
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    hub = copy.deepcopy(DEFAULT_HUB)
    hub.update(cfg.get("hub", {}))
    cfg["hub"] = hub
    cfg.setdefault("projects", [])
    for p in cfg["projects"]:
        p["dir"] = os.path.expanduser(p["dir"])
    return cfg


def save(cfg: dict) -> None:
    """Write the project list atomically, so a crash mid-write can't truncate
    the file everything else depends on."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text(dumps(cfg))
    os.replace(tmp, CONFIG_PATH)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)   # a JSON string is a TOML basic string
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        if any(isinstance(x, dict) for x in v):
            return "[\n" + "".join(f"    {_toml_value(x)},\n" for x in v) + "]"
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def dumps(cfg: dict) -> str:
    """The project list as TOML — just enough of a writer for its shape (top
    level keys, the [hub] table, the [[projects]] array, values that are
    scalars, lists and inline tables), so writing needs no third-party
    package."""
    out = []
    tables = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in cfg.items()
              if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
    for k, v in cfg.items():
        if k not in tables and k not in arrays:
            out.append(f"{_toml_key(k)} = {_toml_value(v)}")
    for k, v in tables.items():
        out.append(f"\n[{_toml_key(k)}]")
        out.extend(f"{_toml_key(a)} = {_toml_value(b)}" for a, b in v.items())
    for k, rows in arrays.items():
        for row in rows:
            out.append(f"\n[[{_toml_key(k)}]]")
            out.extend(f"{_toml_key(a)} = {_toml_value(b)}" for a, b in row.items())
    return "\n".join(out).lstrip("\n") + "\n"


def find(cfg: dict, name: str) -> dict | None:
    return next((p for p in cfg["projects"] if p["name"] == name), None)


def projects_for_dir(cfg: dict, path: str | Path) -> list[dict]:
    """Every project whose directory contains `path`, deepest match only.

    This is the registry's read side. A project always knows where it lives, so
    a directory is the identity it can look itself up by without hardcoding its
    own registry name — which is what `proj port` resolves. Matching walks up
    from the path, so it answers from anywhere inside a project tree.

    A directory can belong to more than one project: a second agent session
    registered against the same tree, say. Every project at the deepest matching depth is returned rather than an
    arbitrary one, so the caller can refuse to guess.
    """
    target = Path(path).expanduser().resolve()
    best = -1
    matches: list[dict] = []
    for p in cfg["projects"]:
        try:
            pdir = Path(p["dir"]).expanduser().resolve()
        except OSError:
            continue
        if target != pdir and pdir not in target.parents:
            continue
        depth = len(pdir.parts)
        if depth > best:
            best, matches = depth, [p]
        elif depth == best:
            matches.append(p)
    return matches


# ── Port bookkeeping ─────────────────────────────────────────────────────────

def project_ports(p: dict) -> list[int]:
    """Every port a single project claims — primary plus any extras."""
    ports = []
    if p.get("web_port"):
        ports.append(int(p["web_port"]))
    for extra in p.get("web_extra", []):
        if extra.get("port"):
            ports.append(int(extra["port"]))
    return ports


def registered_ports(cfg: dict) -> dict[int, str]:
    """Map of every port claimed in the registry -> the project claiming it."""
    claimed: dict[int, str] = {}
    hub_port = int(cfg["hub"]["port"])
    claimed[hub_port] = "hub"
    for p in cfg["projects"]:
        for port in project_ports(p):
            claimed.setdefault(port, p["name"])
    return claimed


def listening_ports() -> set[int]:
    """Ports with a live TCP listener, from `ss`.

    Consulted in addition to the registry so the allocator never hands out a
    port that some unregistered process is already sitting on.
    """
    try:
        out = subprocess.run(
            ["ss", "-Htln"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    ports = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        # Local address is host:port, where host may be an IPv6 literal in
        # brackets or a bare '*'. The port is always after the final colon.
        _, _, port = fields[3].rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return ports


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(raw.decode("utf-8", "replace").split("\0")).strip()


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def listeners() -> dict[int, list[dict]]:
    """Every TCP listener keyed by port: pid, command line and working directory.

    listening_ports() answers "is this port taken", which is all the allocator
    needs. The audit needs "taken by what" — the working directory is what
    distinguishes an ad-hoc `python3 -m http.server` started inside a registered
    project from the server the registry itself brought up.
    """
    try:
        out = subprocess.run(["ss", "-Htlnp"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, list[dict]] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        _, _, port = fields[3].rpartition(":")
        if not port.isdigit():
            continue
        seen = found.setdefault(int(port), [])
        for pid in sorted({int(m) for m in re.findall(r"pid=(\d+)", line)}):
            if any(proc["pid"] == pid for proc in seen):
                continue
            seen.append({"pid": pid, "cmd": _proc_cmdline(pid), "cwd": _proc_cwd(pid)})
    return found


def allocate_port(cfg: dict, *, avoid_live: bool = True) -> int:
    """Lowest free port in the configured pool.

    Skips ports claimed in the registry, ports listed as reserved, and (unless
    disabled) ports with a live listener.
    """
    lo, hi = cfg["hub"]["pool"]
    taken = set(registered_ports(cfg))
    taken |= set(cfg["hub"].get("reserved", []))
    if avoid_live:
        taken |= listening_ports()
    for port in range(int(lo), int(hi) + 1):
        if port not in taken:
            return port
    raise RuntimeError(f"no free port in pool {lo}-{hi}")


def is_up(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """True if something accepts a TCP connection on the port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ── tmux ─────────────────────────────────────────────────────────────────────

def tmux_sessions() -> set[str]:
    try:
        out = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()  # no server running
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


_TMUX_RE = re.compile(r'^\s*(?:SESSION|DEFAULT_SESSION)=["\']?([^"\'\s]+)', re.M)
_DOCKER_RE = re.compile(r'^\s*CONTAINER=["\']?([^"\'\s]+)', re.M)
_BASE_TARGET_CACHE: dict[tuple[str, str, str, str], dict | None] = {}
_BASE_TARGET_DISK_LOADED = False
_WORK_DIR_CACHE: dict[tuple[str, str], str] = {}


def _target_cache_path() -> Path:
    run_dir = Path(f"/run/user/{os.getuid()}")
    if run_dir.is_dir():
        return run_dir / "launcher_target_cache.json"
    return Path(f"/tmp/launcher_target_cache_{os.getuid()}.json")


def _load_disk_target_cache() -> None:
    global _BASE_TARGET_DISK_LOADED
    if _BASE_TARGET_DISK_LOADED:
        return
    _BASE_TARGET_DISK_LOADED = True
    try:
        p = _target_cache_path()
        if not p.is_file() or (time.time() - p.stat().st_mtime) > 300:
            return
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            for k_str, val in data.items():
                parts = k_str.split("\x1f")
                if len(parts) == 4:
                    _BASE_TARGET_CACHE[(parts[0], parts[1], parts[2], parts[3])] = val
    except Exception:
        pass


def save_target_cache() -> None:
    try:
        p = _target_cache_path()
        payload = {"\x1f".join(k): v for k, v in _BASE_TARGET_CACHE.items()}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, p)
    except Exception:
        pass

# Every agent a project can run. `suffix` names its session beside the
# project's base one (`<session><suffix>` on the host; inside a container,
# `<suffix without the dash>` beside `default`). `type` is the name the status
# store and the pane classifier know it by. Commands, labels and the rest can be
# overridden per agent in the config file's [agent.<id>] tables, and agents not
# built in can be added the same way.
BUILTIN_AGENTS: dict[str, dict[str, str]] = {
    "claude": {
        "label": "Claude Code",
        "type": "claude-code",
        "cmd": "claude --dangerously-skip-permissions",
        "suffix": "",
    },
    "agy": {
        "label": "Antigravity",
        "type": "antigravity",
        "cmd": "agy --dangerously-skip-permissions",
        "suffix": "-agy",
    },
    "copilot": {
        "label": "Copilot",
        "type": "copilot",
        "cmd": "copilot",
        "suffix": "-copilot",
    },
}


def _agents() -> dict[str, dict[str, str]]:
    """The agents offered here: the config's `agents` list, in its order, or —
    with none listed — every built-in agent whose command is on PATH (and
    Claude regardless, if nothing is, so a bare machine still has one)."""
    merged = {k: dict(v) for k, v in BUILTIN_AGENTS.items()}
    for aid, over in (radio_config.CFG.get("agent") or {}).items():
        base = merged.setdefault(aid, {"label": aid, "type": aid, "cmd": aid,
                                       "suffix": f"-{aid}"})
        base.update({k: str(v) for k, v in over.items()})
    wanted = list(radio_config.CFG.get("agents") or [])
    if not wanted:
        wanted = [a for a, m in merged.items() if radio_config.on_path(m["cmd"])] \
            or ["claude"]
    unknown = [a for a in wanted if a not in merged]
    if unknown:
        raise SystemExit(f"config: unknown agent(s) {unknown}; "
                         f"define them under [agent.<id>] with a cmd")
    return {a: merged[a] for a in wanted}


AGENTS = _agents()
# The first agent offered is the default: the one a project's base session
# runs, and the one meant by every caller that names none.
DEFAULT_AGENT = next(iter(AGENTS))


def tmux_safe_name(name: str) -> str:
    """A session name tmux can address without interpreting '.' or ':' as
    window/pane separators."""
    return name.translate(str.maketrans(".:", "__"))


def docker_containers() -> set[str]:
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def docker_tmux_sessions(container: str) -> set[str]:
    """Tmux sessions running inside a Docker container."""
    try:
        out = subprocess.run(
            ["docker", "exec", container, "tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _resolve_command_script(p: dict) -> Path | None:
    cmd = p.get("command", "").strip()
    if not cmd.startswith(("./", "../")):
        return None
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return None
    return (Path(p["dir"]).expanduser() / parts[0]).resolve()


def _base_agent_target(p: dict) -> dict | None:
    """Resolve the primary (claude/default) target from project config or its
    launch script."""
    if p.get("tmux"):
        return {"kind": "tmux", "name": p["tmux"]}
    if not _BASE_TARGET_DISK_LOADED:
        _load_disk_target_cache()
    cache_key = (
        p.get("name", ""),
        p.get("dir", ""),
        p.get("command", ""),
        p.get("tmux", ""),
    )
    if cache_key in _BASE_TARGET_CACHE:
        cached = _BASE_TARGET_CACHE[cache_key]
        return dict(cached) if cached is not None else None

    result: dict | None = None
    script = _resolve_command_script(p)
    if script is not None:
        try:
            text = script.read_text(errors="replace")
        except OSError:
            text = None
        if text is not None:
            m = _DOCKER_RE.search(text)
            if m:
                m_sess = _TMUX_RE.search(text)
                sess = m_sess.group(1) if m_sess else "default"
                result = {"kind": "docker", "name": m.group(1), "session": sess}
            else:
                m = _TMUX_RE.search(text)
                if m:
                    result = {"kind": "tmux", "name": m.group(1)}
    if result is None and p.get("name"):
        result = {"kind": "tmux", "name": tmux_safe_name(p["name"])}
    _BASE_TARGET_CACHE[cache_key] = result
    return dict(result) if result is not None else None


def agent_target(p: dict, agent: str = DEFAULT_AGENT) -> dict | None:
    """Where the project puts the given agent — a host tmux session or (for
    containerised projects) a tmux session inside its Docker container.

    The default agent keeps the project's base session name (`<session>`); each
    other agent runs in a parallel session named by its suffix
    (`<session>-agy` on the host, or `agy` inside the container).
    """
    base = _base_agent_target(p)
    if base is None:
        return None
    meta = AGENTS.get(agent) or BUILTIN_AGENTS.get(agent) or {"suffix": f"-{agent}"}
    suffix = "" if agent == DEFAULT_AGENT else (meta.get("suffix") or f"-{agent}")
    if not suffix:
        return base
    if base["kind"] == "docker":
        base_sess = base.get("session", "default")
        sess = suffix.lstrip("-") if base_sess == "default" else f"{base_sess}{suffix}"
        return {"kind": "docker", "name": base["name"], "session": sess}
    return {"kind": "tmux", "name": f"{base['name']}{suffix}"}


def agent_work_dir(p: dict) -> str:
    """Working directory for the project's agent tmux session.

    Sub-projects whose command points at a parent `../launch.sh` share that
    parent script's directory (matching `DIR=$(dirname $0)` inside launch.sh);
    everything else uses `p['dir']`.
    """
    cache_key = (p.get("dir", ""), p.get("command", ""))
    if cache_key in _WORK_DIR_CACHE:
        return _WORK_DIR_CACHE[cache_key]
    script = _resolve_command_script(p)
    if script is not None and script.exists():
        res = str(script.parent)
    else:
        res = str(Path(p["dir"]).expanduser().resolve())
    _WORK_DIR_CACHE[cache_key] = res
    return res


def _sync_agy_container_token(*, background: bool = False) -> None:
    """Sync Antigravity's OAuth token from the host keyring to the file-based
    token path (`~/.gemini/antigravity-cli/antigravity-oauth-token`) that agy
    reads when it detects a container or SSH environment."""
    token_dir = Path.home() / ".gemini" / "antigravity-cli"
    if not token_dir.is_dir():
        return
    token_file = token_dir / "antigravity-oauth-token"
    try:
        st = token_file.stat()
        if st.st_size > 0 and (time.time() - st.st_mtime) < 300:
            return
    except OSError:
        pass
    env = {**os.environ}
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    cmd = [
        "/usr/bin/python3", "-c",
        "import json, os, secretstorage; "
        "p = os.path.expanduser('~/.gemini/antigravity-cli/antigravity-oauth-token'); "
        "bus = secretstorage.dbus_init(); "
        "col = secretstorage.get_default_collection(bus); "
        "items = list(col.search_items({'service': 'gemini', 'username': 'antigravity'})); "
        "sec = items[0].get_secret() if items else b''; "
        "kexp = (json.loads(sec).get('token') or {}).get('expiry', '') if sec else ''; "
        "fexp = (json.loads(open(p, 'rb').read()).get('token') or {}).get('expiry', '') if os.path.exists(p) else ''; "
        "sec and (not fexp or kexp >= fexp) and (open(p, 'wb').write(sec), os.chmod(p, 0o600)); "
        "os.path.exists(p) and os.utime(p, None)",
    ]
    try:
        if background:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        else:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=3,
                env=env,
            )
    except Exception:
        pass


def agent_command(p: dict, agent: str) -> str:
    """The command that starts `agent` for `p`: the project's own
    `agent_cmd.<agent>` if it has one, else the agent's."""
    return (p.get("agent_cmd") or {}).get(agent) or AGENTS[agent]["cmd"]


def start_agent(p: dict, agent: str = DEFAULT_AGENT, *, force_restart: bool = False) -> dict:
    """Ensure the chosen agent's session exists for `p`, without attaching.

    The half of launch_agent that something with no terminal of its own — the
    voice operator, say — can call: the session is created detached, exactly
    as the launcher would create it, and its target is returned.
    """
    if agent not in AGENTS:
        raise ValueError(f"unknown agent '{agent}' (expected one of {list(AGENTS)})")

    if agent == "agy":
        token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        has_token_file = token_file.is_file() and token_file.stat().st_size > 0
        _sync_agy_container_token(background=has_token_file)

    # Make sure ~/.local/bin and ~/.opencode/bin are on PATH before spawning
    # tmux or exec'ing project scripts.
    home = str(Path.home())
    extra_paths = [f"{home}/.local/bin", f"{home}/.opencode/bin"]
    cur_path = os.environ.get("PATH", "")
    path_parts = cur_path.split(":") if cur_path else []
    for ep in reversed(extra_paths):
        if ep not in path_parts:
            path_parts.insert(0, ep)
    os.environ["PATH"] = ":".join(path_parts)

    target = agent_target(p, agent)
    if target is None:
        raise RuntimeError(f"could not resolve agent target for '{p.get('name')}'")

    if target["kind"] == "docker":
        container = target["name"]
        session = target.get("session", "default")
        target = {**target, "session": session}
        # Without container_dir the session starts in the container's WORKDIR.
        workspace = p.get("container_dir")
        q_sess = shlex.quote(session)
        q_eq_sess = shlex.quote(f"={session}")
        q_ws = f"-c {shlex.quote(workspace)} " if workspace else ""
        q_cmd = shlex.quote(f"{agent_command(p, agent)}; exec bash")
        kill = f"tmux kill-session -t {q_eq_sess} 2>/dev/null || true; " if force_restart else ""
        inner = (
            f"{kill}if ! tmux has-session -t {q_eq_sess} 2>/dev/null; then "
            f"tmux new-session -d -s {q_sess} {q_ws}{q_cmd}; fi"
        )
        subprocess.run(["docker", "exec", container, "bash", "-c", inner], check=True)
        return target

    session = target["name"]
    work_dir = agent_work_dir(p)
    if not os.path.isdir(work_dir):
        raise OSError(f"directory does not exist: {work_dir}")

    # `=` anchors an exact session match so e.g. "finance" never matches
    # "finance-llm" when "finance" isn't running yet.
    has_session = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
    ).returncode == 0

    if force_restart and has_session:
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)
        has_session = False

    if not has_session:
        agent_cmd = agent_command(p, agent)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", work_dir,
             f"{agent_cmd}; exec bash"],
            check=True,
        )
        print(f"Created tmux session '{session}' ({agent}) in {work_dir}")
    return target


def launch_agent(p: dict, agent: str = DEFAULT_AGENT, *, force_restart: bool = False) -> None:
    """Ensure the chosen agent's session exists for `p` and attach/switch to it.

    Replaces the current process via os.execvp.
    """
    target = start_agent(p, agent, force_restart=force_restart)

    # Name the terminal tab for the project (and agent when non-default).
    tag = f" ({agent})" if agent != DEFAULT_AGENT else ""
    title = "".join(c for c in f"{p['name']}{tag}" if c.isprintable())
    sys.stdout.write(f"\033]0;{title}\a")
    sys.stdout.flush()

    if target["kind"] == "docker":
        q_eq_sess = shlex.quote(f"={target['session']}")
        os.execvp("docker", ["docker", "exec", "-it", target["name"], "bash", "-c",
                             f"exec tmux attach-session -t {q_eq_sess}"])

    session = target["name"]
    os.chdir(agent_work_dir(p))
    if os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", f"={session}"])
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={session}"])


def stop_agent(p: dict, agent: str = DEFAULT_AGENT) -> tuple[bool, str]:
    """Kill one agent session — tmux on the host, or tmux in its container."""
    t = agent_target(p, agent)
    if t is None:
        return False, "no session"
    if t["kind"] == "docker":
        cmd = ["docker", "exec", t["name"], "tmux", "kill-session",
               "-t", f"={t.get('session', 'default')}"]
    else:
        cmd = ["tmux", "kill-session", "-t", f"={t['name']}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "").strip()


def web_session(p: dict) -> str:
    """tmux session name for the project's hub-managed web instance."""
    return p.get("web_session") or f"{WEB_SESSION_PREFIX}{p['name']}"


# ── Web instance lifecycle ───────────────────────────────────────────────────

def _run(command: str, cwd: str, port: int | None) -> tuple[bool, str]:
    """Run a supervisor command directly and report what it said.

    start_new_session detaches the command into its own session. Without it, a
    start script that backgrounds its services with a plain `&` (player's does)
    leaves them in the hub's process group — so restarting the hub would kill
    the very services it had started.
    """
    env = {**os.environ}
    if port:
        env["PORT"] = str(port)
        command = command.replace("$PORT", str(port)).replace("${PORT}", str(port))
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=120,
                           start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    # Hand-rolled start/stop scripts tend to be chatty and colourful; strip the
    # escapes so the last line is readable in a toast or a terminal.
    clean = _ANSI_RE.sub("", r.stdout + r.stderr)
    output = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if r.returncode != 0:
        return False, output[-1] if output else f"exit {r.returncode}"
    return True, output[-1] if output else ""


def start_web(p: dict) -> tuple[bool, str]:
    """Bring a project's web instance up in its own tmux session.

    tmux is already this machine's process supervisor, so a web instance is
    just another session — inspectable with `tmux attach`, and surviving the
    hub being restarted. Externally supervised projects (those with `web_stop`)
    skip tmux entirely and hand off to whatever already owns them.
    """
    if not p.get("web_start"):
        return False, f"{p['name']} has no web_start command"
    port = p.get("web_port")
    if not port:
        return False, f"{p['name']} has no web_port"

    if p.get("web_stop"):
        ok, message = _run(p["web_start"], p["dir"], port)
        return ok, message or f"started {p['name']}"

    session = web_session(p)
    if session in tmux_sessions():
        return True, f"already running in tmux session '{session}'"

    # $PORT is substituted textually and exported, so a start command can use
    # either form: "serve.py $PORT" or a script that reads ${PORT}.
    command = p["web_start"].replace("$PORT", str(port)).replace("${PORT}", str(port))
    env = {**os.environ, "PORT": str(port)}
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", p["dir"],
             f"{command}; exec bash"],
            check=True, capture_output=True, text=True, timeout=15, env=env,
        )
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or str(e)).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return True, f"started '{session}' on port {port}"


def port_listeners(port: int) -> set[int]:
    """PIDs listening on a TCP port."""
    try:
        out = subprocess.run(["ss", "-Htlnp", f"sport = :{int(port)}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    return {int(m) for m in re.findall(r"pid=(\d+)", out)}


def stop_web(p: dict) -> tuple[bool, str]:
    """Stop a project's web instance.

    Killing the tmux session is not always enough: a start script may daemonise
    (bikesim's setsids vite into its own session), leaving the server running
    with the pane gone. So after killing the session, anything still holding the
    project's registered port is terminated too — scoped to that one port, which
    the registry guarantees belongs to this project.

    Externally supervised projects instead run their own stop command, so we
    never race a supervisor that would just restart what we killed.
    """
    if p.get("web_stop"):
        ok, message = _run(p["web_stop"], p["dir"], p.get("web_port"))
        return ok, message or f"stopped {p['name']}"

    done = []
    session = web_session(p)
    if session in tmux_sessions():
        try:
            # `=` anchors the name. The membership test above already makes this
            # exact — tmux prefers an exact match over a prefix — but a bare
            # target is a prefix match, and "web-finance" is a prefix of
            # "web-finance-llm". Anchoring keeps that safety here rather than
            # resting on a guard three lines up that a refactor could drop.
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"],
                           check=True, capture_output=True, text=True, timeout=10)
            done.append(f"killed tmux session '{session}'")
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)

    port = p.get("web_port")
    if port:
        for sig in ("-TERM", "-KILL"):
            pids = port_listeners(port)
            if not pids:
                break
            for pid in pids:
                try:
                    subprocess.run(["kill", sig, str(pid)],
                                   capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            done.append(f"{sig.lstrip('-').lower()} {' '.join(map(str, sorted(pids)))} on :{port}")
            for _ in range(10):
                if not is_up(port):
                    break
                time.sleep(0.15)

    return True, "; ".join(done) if done else "not running"


# ── Worktrees ────────────────────────────────────────────────────────────────

def resolve_repo(cfg: dict, repo: str) -> str:
    """A repository named by a registered project or a path inside it."""
    p = find(cfg, repo)
    path = p["dir"] if p else os.path.expanduser(repo)
    if git.info(path) is None:
        raise ValueError(f"'{repo}' is neither a registered project nor in a git repository")
    return path


def add_worktree_project(name: str, repo: str, *, branch: str | None = None,
                         base: str | None = None, subdir: str = "",
                         ticket: str | None = None, desc: str = "") -> dict:
    """A new worktree of `repo` on `branch` (made from `base` if new), and a
    project registered at it (or at `subdir` inside it), with no web
    instance. A ticket links it and, with no branch given, names the branch."""
    cfg = load()
    if find(cfg, name):
        raise ValueError(f"'{name}' is already registered")
    src = resolve_repo(cfg, repo)
    branch = branch or (ticket and jira.branch_for(ticket)) or name
    path = git.worktree_path(src, name, radio_config.CFG["worktree_dir"])
    if path.exists():
        raise ValueError(f"{path} already exists")
    git.add_worktree(src, path, branch, base)
    directory = path / subdir if subdir else path
    project = {"name": name, "dir": str(directory), "added": int(time.time())}
    if desc:
        project["web_desc"] = desc
    if ticket:
        project["jira"] = [ticket]
    cfg = load()
    cfg["projects"].append(project)
    save(cfg)
    return project


def remove_project(name: str, *, worktree: bool = False, force: bool = False) -> list[str]:
    """Unregister a project — and with `worktree`, remove the linked worktree
    it lives in (refused when that has uncommitted work, unless `force`)."""
    cfg = load()
    p = find(cfg, name)
    if p is None:
        raise KeyError(f"'{name}' is not registered")
    done = []
    if worktree:
        gi = git.info(p["dir"])
        if not gi or not gi["worktree"]:
            raise ValueError(f"{name} is not in a linked worktree")
        if not force and git.dirty(gi["root"]):
            raise ValueError(f"{gi['root']} has uncommitted changes (force to remove anyway)")
        others = [x["name"] for x in cfg["projects"] if x["name"] != name
                  and (git.info(x["dir"]) or {}).get("root") == gi["root"]]
        if others:
            raise ValueError(f"{', '.join(others)} also live in {gi['root']}")
        git.remove_worktree(gi["root"], force=force)
        done.append(f"removed worktree {gi['root']} (branch {gi['branch']} kept)")
    cfg["projects"] = [x for x in cfg["projects"] if x["name"] != name]
    if cfg.get("auto_launch_project") == name:
        cfg["auto_launch_project"] = cfg["projects"][0]["name"] if cfg["projects"] else ""
    save(cfg)
    done.append(f"unregistered {name}")
    return done


# ── Tickets ──────────────────────────────────────────────────────────────────

def tickets(p: dict, gi: dict | None = None) -> list[str]:
    """The Jira keys a project is linked to: those its entry names (`jira`),
    then any its branch names (ABC-123-fix-login), without repeats."""
    keys = list(p.get("jira") or [])
    if gi is None:
        gi = git.info(p["dir"])
    for k in jira.keys_in((gi or {}).get("branch") or ""):
        if k not in keys:
            keys.append(k)
    return keys


def link_ticket(name: str, key: str, unlink: bool = False) -> dict:
    cfg = load()
    p = find(cfg, name)
    if p is None:
        raise KeyError(f"no project called '{name}'")
    keys = [k for k in (p.get("jira") or []) if k != key]
    if not unlink:
        keys.append(key)
    if keys:
        p["jira"] = keys
    else:
        p.pop("jira", None)
    save(cfg)
    return p


# ── Status snapshot ──────────────────────────────────────────────────────────

def status(cfg: dict) -> list[dict]:
    """Everything the dashboard needs about every project, in one pass."""
    sessions = tmux_sessions()
    containers = None  # only shelled out for if some project is docker-based
    container_sessions: dict[str, set[str]] = {}
    out = []
    for p in cfg["projects"]:
        port = p.get("web_port")
        host = p.get("web_host") or "127.0.0.1"
        targets = {a: agent_target(p, a) for a in AGENTS}
        if containers is None and any(t and t["kind"] == "docker" for t in targets.values()):
            containers = docker_containers()

        def _target_up(t: dict | None) -> bool:
            if not t:
                return False
            if t["kind"] == "docker":
                cname = t["name"]
                if cname not in (containers or set()):
                    return False
                if cname not in container_sessions:
                    container_sessions[cname] = docker_tmux_sessions(cname)
                return t.get("session", "default") in container_sessions[cname]
            return t["name"] in sessions

        def _target_label(t: dict | None) -> str | None:
            if not t:
                return None
            if t["kind"] == "docker":
                return f"{t['name']}:{t.get('session', 'default')}"
            return t["name"]

        agents = {a: {"label": AGENTS[a]["label"], "session": _target_label(t),
                      "kind": t["kind"] if t else None, "up": _target_up(t)}
                  for a, t in targets.items()}
        base = targets.get(DEFAULT_AGENT)
        gi = git.info(p["dir"])
        entry = {
            "name": p["name"],
            "dir": p["dir"],
            "command": p.get("command", ""),
            "desc": p.get("web_desc", ""),
            "port": port,
            "path": p.get("web_path", "/"),
            "host": p.get("web_host"),
            "scheme": p.get("web_scheme", "http"),
            "fixed": bool(p.get("web_fixed")),
            "managed": bool(p.get("web_start")),
            "supervised": bool(p.get("web_stop")),
            "agent_session": base["name"] if base else None,
            "agent_kind": base["kind"] if base else None,
            "agent_up": any(x["up"] for x in agents.values()),
            "agents": agents,       # every offered agent, in AGENTS order
            "git": gi,              # repo, branch, worktree, subdir (core/git.py)
            "tickets": tickets(p, gi),
            "jira": list(p.get("jira") or []),   # the explicitly linked ones
            "pinned": bool(p.get("pin_project")),   # first in the projects view
            "web_session": web_session(p),
            "url": p.get("web_url"),
            "up": is_up(port, host) if port else False,
            "extra": [],
        }
        entry["web_session_up"] = entry["web_session"] in sessions
        for extra in p.get("web_extra", []):
            entry["extra"].append({
                "name": extra.get("name", ""),
                "port": extra.get("port"),
                "path": extra.get("path", "/"),
                "host": extra.get("host"),
                "scheme": extra.get("scheme", "http"),
                "up": is_up(extra["port"], extra.get("host") or host) if extra.get("port") else False,
            })
        out.append(entry)
    return out
