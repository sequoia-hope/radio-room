#!/usr/bin/env python3
"""Put a terminal on one project's agent session — what a `?attach=` link
opens a tab to run.

    attach.py <project> [agent]

The attaching is the registry's own: `registry.launch_agent`, the same call
`proj agent` makes, so a host session and one inside a docker container are
reached the same way they always are.
What this adds is a refusal: a link only ever looks at an agent, so one that
is not running is reported and left alone rather than started. The tab then
carries on into a login shell, as every tab does.
"""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "core" / "registry.py"


class AttachError(Exception):
    pass


def load_registry():
    spec = importlib.util.spec_from_file_location("registry", REGISTRY)
    if spec is None or spec.loader is None:
        raise AttachError(f"no registry at {REGISTRY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def running(registry, target: dict) -> bool:
    if target["kind"] == "docker":
        return target.get("session", "default") in registry.docker_tmux_sessions(target["name"])
    return subprocess.run(["tmux", "has-session", "-t", f"={target['name']}"],
                          capture_output=True).returncode == 0


def resolve(project: str, agent: str | None = None):
    """(registry, project entry, agent, target) for a running agent session,
    or AttachError saying why there is nothing to attach to."""
    registry = load_registry()
    agent = agent or registry.DEFAULT_AGENT
    if agent not in registry.AGENTS:
        raise AttachError(f"unknown agent '{agent}'")
    p = next((x for x in registry.load()["projects"] if x["name"] == project), None)
    if p is None:
        raise AttachError(f"no project called '{project}'")
    target = registry.agent_target(p, agent)
    if target is None:
        raise AttachError(f"{project} has no {agent} session")
    if not running(registry, target):
        raise AttachError(f"{project}: its {agent} session isn't running")
    return registry, p, agent, target


def tab_name(target: dict) -> str | None:
    """What tabs.py will call a tab attached here — a host tmux client reads
    back as its session. Inside a container the client is out of sight."""
    return target["name"] if target["kind"] == "tmux" else None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        registry, p, agent, _ = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except AttachError as exc:
        print(f"{exc} — opening the launcher instead.")
        time.sleep(2)
        return 1
    registry.launch_agent(p, agent)   # execs tmux (or docker); returns only on failure
    return 1


if __name__ == "__main__":
    sys.exit(main())
