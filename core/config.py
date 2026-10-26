"""Radio Room's configuration — one TOML file outside the repo.

    $RADIO_ROOM_CONFIG, else ~/.config/radio-room/config.toml

Nothing personal lives in the repository: where the project list is, which
agents to offer, which optional parts to run, and every setting of the voice
server come from this file. `config.example.toml` at the repo root documents
every key; a missing file means all defaults, which is the lean setup — the
project list, the desktop window and agent status, nothing else.

Ports are not configuration. Every listener asks the registry for its port
(see core/registry.py), so no number is ever written down here.

Stdlib only: the status hook imports this under whatever python3 an agent's
shell finds.
"""

from __future__ import annotations

import copy
import os
import shutil
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("RADIO_ROOM_CONFIG")
                   or "~/.config/radio-room/config.toml").expanduser()

DEFAULTS: dict = {
    # The project list (the registry). Kept outside the repo like this file.
    "projects": str(CONFIG_PATH.parent / "projects.toml"),
    # Agents to offer, by id (see core/registry.py AGENTS). Empty means every
    # built-in agent whose command is on PATH.
    "agents": [],
    # Per-agent overrides, e.g. [agent.copilot] cmd = "copilot --allow-all-tools".
    "agent": {},
    # Optional parts. Everything else that is optional switches itself off when
    # what it needs is absent: no GPU meter without nvidia-smi, no booking meter
    # without a registered `reserve`, no container sessions without docker.
    "features": {
        "voice": False,      # the voice server: advisor, speech, follow-the-desk
        "launcher": False,   # the desktop's launcher tab (needs a login shell
                             # that runs the launcher on SSH_CLIENT_WEB)
        "usage": False,      # Claude Code / Antigravity quota and token tracking
        "summaries": False,  # one-line turn summaries from a local Ollama model
    },
    # The registry project that serves the voice server.
    "voice_project": "radio-room",
    # Where `proj add --worktree` puts a new worktree: {repo} is the main
    # checkout, {parent} its parent directory, {reponame} its name, {name}
    # the new project's name.
    "worktree_dir": "{parent}/{reponame}.worktrees/{name}",
    # Jira (on when url is set): see config.example.toml.
    "jira": {},
    "status": {},            # agent status monitor settings (voice/status.py)
    "voice": {},             # voice server settings (voice/server.py)
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with open(path, "rb") as f:
        return _merge(DEFAULTS, tomllib.load(f))


CFG = load()


def feature(name: str) -> bool:
    return bool(CFG["features"].get(name))


def projects_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("RADIO_ROOM_PROJECTS")
                                   or CFG["projects"]))


def section(name: str, defaults: dict) -> dict:
    """One part's settings: its defaults, overlaid with the file's [name]."""
    return _merge(defaults, CFG.get(name) or {})


def on_path(cmd: str) -> bool:
    first = cmd.split()[0] if cmd.split() else ""
    return bool(first) and shutil.which(first) is not None
