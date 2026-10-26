#!/usr/bin/env python3
"""Start a project's agent session, or attach if it is already running.

    start.py <project> [agent]

What `proj agent` does — the registry's own launch_agent, which creates the
session if need be and then execs tmux (or docker) — so a session started
from the window is the session `proj agent` would have started. attach.py, by contrast, only ever looks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "terminal"))
from attach import AttachError, load_registry  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        registry = load_registry()
    except AttachError as exc:
        print(exc)
        return 1
    p = registry.find(registry.load(), sys.argv[1])
    if p is None:
        print(f"no project called '{sys.argv[1]}'")
        return 1
    registry.launch_agent(p, sys.argv[2] if len(sys.argv) > 2 else registry.DEFAULT_AGENT)
    return 1   # launch_agent execs; reaching here means it failed


if __name__ == "__main__":
    sys.exit(main())
