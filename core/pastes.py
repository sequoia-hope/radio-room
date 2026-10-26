"""Images pasted from a phone, put where an agent can read them.

A phone's clipboard is nowhere near the agent, so Ctrl+V's image paste can't
reach it. The pages upload the image instead: it is written here, and the page
pastes its path into the agent's terminal. Claude Code turns a pasted path to
an image file into an attached image, the same `[Image #1]` a Ctrl+V gives.

An agent in a container can't see this machine's files, so for one of those
the image is copied into the container and the path is the container's.

Files are kept a week, then swept on the next upload.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path

DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "radio-room" / "pastes"
CONTAINER_DIR = "/tmp/radio-room-pastes"
LIMIT = 30 * 1024 * 1024
KEEP = 7 * 24 * 3600


def kind(data: bytes) -> str | None:
    """The image type, by its first bytes: only those Claude takes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save(data: bytes) -> Path:
    """Write an upload to DIR and return its path. ValueError if it isn't an
    image Claude can read."""
    ext = kind(data)
    if ext is None:
        raise ValueError("not a PNG, JPEG, GIF or WebP image")
    DIR.mkdir(parents=True, exist_ok=True)
    sweep()
    path = DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
    path.write_bytes(data)
    return path


def sweep() -> None:
    cutoff = time.time() - KEEP
    for old in DIR.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def into_container(path: Path, container: str) -> str:
    """Copy a saved image into a container; the path it has there."""
    subprocess.run(["docker", "exec", container, "mkdir", "-p", CONTAINER_DIR],
                   check=True, capture_output=True, timeout=15)
    inside = f"{CONTAINER_DIR}/{path.name}"
    subprocess.run(["docker", "cp", str(path), f"{container}:{inside}"],
                   check=True, capture_output=True, timeout=30)
    return inside
