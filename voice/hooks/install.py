#!/usr/bin/env python3
"""Wire hooks/vp_event.py into the agents — or take it out.

    hooks/install.py             show what would change (changes nothing)
    hooks/install.py --apply     make the change, keeping a .bak of each file
    hooks/install.py --remove    show the removal; add --apply to do it

Only the agents offered here (Radio Room's config, or those on PATH) are
touched, unless --agents names others. The files are global: every session
of that agent on the machine reads them. In shared files only entries that run
vp_event.py are ever added or removed; everything else is left as found.

  ~/.claude/settings.json          Claude Code (sessions started after the edit)
  ~/.gemini/config/hooks.json      Antigravity (one named hook, "voice-prompt")
  ~/.copilot/hooks/radio-room.json   Copilot CLI (a file of its own; $COPILOT_HOME)
"""

import argparse
import difflib
import json
import os
import shutil
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "vp_event.py"
MARK = "vp_event.py"

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "StopFailure",
                 "Notification", "SessionEnd"]

AGY_HOOKS = Path.home() / ".gemini" / "config" / "hooks.json"
AGY_NAME = "voice-prompt"
AGY_EVENTS = ["SessionStart", "PreInvocation", "Stop"]

COPILOT_HOOKS = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") \
    / "hooks" / "radio-room.json"
COPILOT_EVENTS = ["sessionStart", "userPromptSubmitted", "agentStop",
                  "errorOccurred", "notification", "sessionEnd"]


def claude_cmd() -> str:
    return f'python3 "{HOOK}" claude'


def agy_cmd(event: str) -> str:
    # agy's payload doesn't name its event, so the command does.
    return f'python3 "{HOOK}" agy {event}'


def copilot_cmd(event: str) -> str:
    # Copilot's camelCase payloads don't name their event either.
    return f'python3 "{HOOK}" copilot {event}'


def ours(group: dict) -> bool:
    return any(MARK in (h.get("command") or "") for h in group.get("hooks", []))


def edit_claude(cfg: dict, remove: bool) -> dict:
    cfg = json.loads(json.dumps(cfg))
    hooks = cfg.setdefault("hooks", {})
    for event in CLAUDE_EVENTS:
        groups = [g for g in hooks.get(event, []) if not ours(g)]
        if not remove:
            groups.append({"hooks": [{"type": "command", "command": claude_cmd(),
                                      "timeout": 10}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks")
    return cfg


def edit_agy(cfg: dict, remove: bool) -> dict:
    cfg = json.loads(json.dumps(cfg))
    cfg.pop(AGY_NAME, None)
    if not remove:
        cfg[AGY_NAME] = {e: [{"type": "command", "command": agy_cmd(e), "timeout": 5}]
                         for e in AGY_EVENTS}
    return cfg


def edit_copilot(cfg: dict, remove: bool) -> dict | None:
    """The whole file is ours; removing it means deleting it."""
    if remove:
        return None
    return {"version": 1, "hooks": {e: [{"type": "command", "bash": copilot_cmd(e),
                                         "timeoutSec": 10}]
                                    for e in COPILOT_EVENTS}}


TARGETS = {"claude": (CLAUDE_SETTINGS, edit_claude),
           "agy": (AGY_HOOKS, edit_agy),
           "copilot": (COPILOT_HOOKS, edit_copilot)}


def offered() -> list[str]:
    sys.path.insert(0, str(HOOK.parent.parent.parent / "core"))
    import registry
    return [a for a in registry.AGENTS if a in TARGETS]


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def dump(cfg: dict) -> str:
    return json.dumps(cfg, indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the change")
    ap.add_argument("--remove", action="store_true", help="take the hooks out")
    ap.add_argument("--agents", help=f"comma-separated, from {', '.join(TARGETS)} "
                                     "(default: the agents offered here)")
    args = ap.parse_args()
    agents = args.agents.split(",") if args.agents else offered()
    unknown = [a for a in agents if a not in TARGETS]
    if unknown:
        ap.error(f"no hooks for {unknown}")

    changed = False
    for path, edit in (TARGETS[a] for a in agents):
        before = path.read_text() if path.exists() else ""
        new = edit(load(path), args.remove)
        if new is None:
            if not path.exists():
                print(f"{path}: already absent")
                continue
            changed = True
            print(f"{path}: remove")
            if args.apply:
                path.unlink()
                print(f"{path}: removed")
            continue
        after = dump(new)
        if before.strip() and json.loads(before) == json.loads(after):
            print(f"{path}: already as wanted")
            continue
        changed = True
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=str(path), tofile=f"{path} (new)"))
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(after)
            os.replace(tmp, path)
            print(f"{path}: written (previous version in {path.name}.bak)")
    if changed and not args.apply:
        print("\nNothing written. Run again with --apply to make this change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
