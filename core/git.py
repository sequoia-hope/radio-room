"""What git says about a project directory — and making and removing worktrees.

A project is a directory. At a desk with one big monorepo, several projects
are the same repository checked out in different worktrees (each on its own
branch), often a subdirectory of one; elsewhere a project is a standalone
repo, or not under git at all. `info()` tells these apart:

    repo      the repository's common git dir — the same for every worktree
              of it, so it is what groups projects together
    name      a short name for the repository (its main checkout's directory)
    root      this checkout's top level
    subdir    where the project sits inside it ("" at the top)
    branch    the checked-out branch (None when detached)
    worktree  True for a linked worktree, False for the main checkout

Stdlib only, and every call is bounded: a slow or broken repo costs a
timeout, never a hang.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

_CACHE: dict[str, tuple[float, dict | None]] = {}
TTL = 15.0


def _git(cwd: str | Path, *args: str, timeout: float = 5) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def info(path: str | Path) -> dict | None:
    """Git facts for a directory, or None outside git. Cached for a few
    seconds, since the views ask for every project on every poll."""
    path = str(Path(path).expanduser())
    now = time.monotonic()
    hit = _CACHE.get(path)
    if hit and now - hit[0] < TTL:
        return hit[1]
    out = _git(path, "rev-parse", "--path-format=absolute", "--show-toplevel",
               "--git-common-dir", "--git-dir", "--abbrev-ref", "HEAD") \
        if os.path.isdir(path) else None
    result = None
    if out:
        lines = out.splitlines()
        if len(lines) == 4:
            root, common, gitdir, branch = lines
            main = Path(common).parent if Path(common).name == ".git" else Path(common)
            try:
                subdir = str(Path(path).resolve().relative_to(Path(root).resolve()))
            except ValueError:
                subdir = ""
            result = {
                "repo": str(Path(common).resolve()),
                "name": main.name.removesuffix(".git"),
                "root": root,
                "subdir": "" if subdir == "." else subdir,
                "branch": None if branch == "HEAD" else branch,
                "worktree": Path(gitdir).resolve() != Path(common).resolve(),
            }
    _CACHE[path] = (now, result)
    return result


def worktrees(repo: str | Path) -> list[dict]:
    """Every checkout of a repository: [{path, branch, main}], main first.
    `repo` is any directory inside any of its checkouts."""
    out = _git(repo, "worktree", "list", "--porcelain")
    if out is None:
        return []
    trees, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                trees.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur = {"path": line[9:], "branch": None, "main": not trees}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "bare":
            cur["bare"] = True
    return [t for t in trees if not t.get("bare")]


def slug(text: str, limit: int = 40) -> str:
    """A branch- and directory-safe rendering of free text."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.").lower()
    return s[:limit].rstrip("-.") or "work"


def worktree_path(repo: str | Path, name: str, template: str) -> Path:
    """Where a new worktree goes: the config's `worktree_dir` template, with
    {repo} the main checkout, {parent} its parent, {reponame} its name and
    {name} the new project's name."""
    i = info(repo)
    if i is None:
        raise ValueError(f"{repo} is not in a git repository")
    trees = worktrees(repo)
    main = Path(trees[0]["path"]) if trees else Path(i["root"])
    return Path(os.path.expanduser(template.format(
        repo=main, parent=main.parent, reponame=main.name, name=name)))


def add_worktree(repo: str | Path, path: Path, branch: str,
                 base: str | None = None) -> str:
    """Check `branch` out at `path`: the existing branch if there is one,
    otherwise a new one from `base` (default: the repo's HEAD). Returns
    git's own account of what it did; raises RuntimeError with git's error."""
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") is not None
    args = ["worktree", "add"]
    args += [str(path), branch] if exists else ["-b", branch, str(path)] + ([base] if base else [])
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "git worktree add failed")
    _CACHE.clear()
    return (r.stderr or r.stdout).strip()


def dirty(path: str | Path) -> bool:
    out = _git(path, "status", "--porcelain", "--untracked-files=normal")
    return bool(out)


def remove_worktree(path: str | Path, force: bool = False) -> None:
    """Remove a linked worktree (never the main checkout). Without `force`,
    git refuses when it has uncommitted changes, and so do we."""
    i = info(path)
    if i is None or not i["worktree"]:
        raise RuntimeError(f"{path} is not a linked worktree")
    # From the main checkout: git won't remove the worktree it is run in.
    main = worktrees(path)[0]["path"]
    args = ["worktree", "remove", i["root"]] + (["--force"] if force else [])
    r = subprocess.run(["git", "-C", main, *args], capture_output=True,
                       text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    _CACHE.clear()
