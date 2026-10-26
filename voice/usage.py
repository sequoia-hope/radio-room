"""Per-project token usage and per-service remaining quota/credits.

Two jobs, both backed by events.db so the HTTP handlers read straight from
SQLite without waiting on transcript scans or CLI subprocesses:

1. Per-project usage (usage_files, usage_turns)
   Indexes Claude Code (~/.claude/projects/**/*.jsonl) and Antigravity
   (~/.gemini/antigravity-cli/brain/*/transcript.jsonl) transcripts by
   (mtime, size), deduplicating Claude's multi-block assistant records by
   message.id and Antigravity's by step_index. Every turn is mapped back to
   its launcher project via events.db, conversation_summaries.db, or cwd.

2. Per-service remaining credits and rate-limit buckets (service_quota)
   - Claude Code: read from ~/.claude.json (cachedUsageUtilization), which
     Claude Code updates automatically with 5h/7d utilization percentages,
     reset times, promotional dollar credit balances, and extra-usage state.
   - Antigravity: read via `agy -p "/usage" --output-format json` and
     `agy -p "/credits" --output-format json`, which answer non-interactively
     without starting a turn or spending quota.

Stdlib only, matching events.py and server.py.
"""

from datetime import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import events

AGY_QUOTA_TTL = 300.0       # refresh agy quota every 5 min
AGY_POST_TURN_MIN = 30.0    # or 30s after an agy turn finishes

CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"
AGY_BRAIN = AGY_DIR / "brain"
AGY_SUMMARIES_DB = AGY_DIR / "conversation_summaries.db"
AGY_HISTORY = AGY_DIR / "history.jsonl"

# Directories that moved or live inside a container, mapped to their current
# project name in the registry: the config file's [status] dir_aliases.
def _dir_aliases() -> dict[Path, str]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
    import config as radio_config
    raw = (radio_config.CFG.get("status") or {}).get("dir_aliases") or {}
    return {Path(os.path.expanduser(d)): name for d, name in raw.items()}


DIR_ALIASES = _dir_aliases()

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_files (
    path    TEXT PRIMARY KEY,
    agent   TEXT NOT NULL,
    project TEXT,
    cwd     TEXT,
    mtime   REAL NOT NULL,
    size    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_turns (
    path          TEXT NOT NULL,
    turn_id       TEXT NOT NULL,
    ts            REAL NOT NULL,
    agent         TEXT NOT NULL,
    project       TEXT,
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (path, turn_id)
);
CREATE INDEX IF NOT EXISTS usage_turns_proj ON usage_turns(project, agent, ts);
CREATE INDEX IF NOT EXISTS usage_turns_ts ON usage_turns(agent, ts);

CREATE TABLE IF NOT EXISTS service_quota (
    service TEXT PRIMARY KEY,
    ts      REAL NOT NULL,
    data    TEXT NOT NULL
);
"""

# Fast byte regexes for Claude Code's large JSONL lines (avoiding json.loads on
# multi-hundred-KB tool/context blobs when we only need the assistant usage).
_CWD_RE = re.compile(rb'"cwd":"([^"]+)"')
_MID_RE = re.compile(rb'"id":"(msg_[^"]+)"')
_TS_RE = re.compile(rb'"timestamp":"([^"]+)"')
_MOD_RE = re.compile(rb'"model":"([^"]+)"')
_IN_RE = re.compile(rb'"input_tokens":(\d+)')
_CC_RE = re.compile(rb'"cache_creation_input_tokens":(\d+)')
_CR_RE = re.compile(rb'"cache_read_input_tokens":(\d+)')
_OUT_RE = re.compile(rb'"output_tokens":(\d+)')
_AGY_MOD_RE = re.compile(r"setting `Model Selection` from [^\n]+ to ([^\n.]+)\.")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def prune(db: sqlite3.Connection, days: int = events.RETENTION_DAYS) -> None:
    ensure_schema(db)
    cutoff = time.time() - days * 86400
    db.execute("DELETE FROM usage_turns WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM usage_files WHERE mtime < ? AND path NOT IN "
               "(SELECT DISTINCT path FROM usage_turns)", (cutoff,))


def _iso_ts(val: str | None, default: float) -> float:
    if not val:
        return default
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def _weighted(inp: int, cw: int, cr: int, out: int) -> float:
    """Relative quota weight across token kinds (output ~5x input, cache read
    ~0.1x input, cache write ~1.25x input)."""
    return inp + cw * 1.25 + cr * 0.1 + out * 5.0


# ── Mapping transcripts to projects ──────────────────────────────────────────

class ProjectResolver:
    """Maps a workspace directory, Claude transcript file, or Antigravity
    conversation ID to a project name from the project list."""

    def __init__(self, db: sqlite3.Connection, projects: list[dict]):
        # Longest directory path first so nested projects match specifically.
        self.dir_map: list[tuple[Path, str]] = []
        self.name_map: dict[str, str] = {}
        self.slug_map: list[tuple[str, str]] = []
        for p in projects:
            name = p["name"]
            self.name_map[name.replace("_", "-")] = name
            try:
                rp = Path(p["dir"]).resolve()
            except OSError:
                rp = Path(p["dir"])
            self.dir_map.append((rp, name))
            slug1 = "-" + str(rp).lstrip("/").replace("/", "-")
            slug2 = slug1.replace("_", "-")
            self.slug_map.append((slug1, name))
            if slug2 != slug1:
                self.slug_map.append((slug2, name))
        self.dir_map.sort(key=lambda x: len(str(x[0])), reverse=True)
        self.slug_map.sort(key=lambda x: len(x[0]), reverse=True)

        # What hooks have already recorded in events.db.
        self.by_session: dict[str, str] = {}
        self.by_path: dict[str, str] = {}
        for r in db.execute(
            "SELECT agent_session, transcript_path, project FROM events "
            "WHERE project IS NOT NULL"
        ):
            proj = r["project"]
            if r["agent_session"]:
                self.by_session[r["agent_session"]] = proj
            tpath = r["transcript_path"]
            if tpath:
                self.by_path[tpath] = proj
                if "/brain/" in tpath:
                    cid = tpath.split("/brain/")[1].split("/")[0]
                    self.by_session[cid] = proj

        # Antigravity conversation_summaries.db + history.jsonl.
        self.agy_uris: dict[str, str] = {}
        self.agy_parents: dict[str, str] = {}
        if AGY_SUMMARIES_DB.exists():
            try:
                sdb = sqlite3.connect(f"file:{AGY_SUMMARIES_DB}?mode=ro",
                                      uri=True, timeout=2)
                for cid, pid, wu in sdb.execute(
                    "SELECT conversation_id, parent_conversation_id, workspace_uris "
                    "FROM conversation_summaries"
                ):
                    if pid:
                        self.agy_parents[cid] = pid
                    try:
                        uris = json.loads(wu)
                        if uris:
                            self.agy_uris[cid] = uris[0]
                    except (ValueError, TypeError):
                        pass
                sdb.close()
            except sqlite3.Error:
                pass
        if AGY_HISTORY.exists():
            try:
                for line in AGY_HISTORY.read_text(errors="replace").splitlines():
                    rec = json.loads(line)
                    cid = rec.get("conversationId")
                    ws = rec.get("workspace")
                    if cid and ws and cid not in self.agy_uris:
                        self.agy_uris[cid] = ws
            except (OSError, ValueError):
                pass

    def resolve_cwd(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        if cwd.startswith("file://"):
            cwd = cwd[7:]
        if cwd.startswith("/tmp/"):
            return None
        p = Path(cwd)
        for alias_dir, proj in DIR_ALIASES.items():
            if p == alias_dir or alias_dir in p.parents:
                return proj
        try:
            pr = p.resolve()
        except OSError:
            pr = p
        for pdir, name in self.dir_map:
            if pr == pdir or pdir in pr.parents:
                return name
        return self.name_map.get(p.name.replace("_", "-"))

    def resolve_slug(self, slug: str) -> str | None:
        for alias_dir, name in DIR_ALIASES.items():
            if slug == str(alias_dir).replace("/", "-"):
                return name
        if slug.startswith("-tmp-"):
            return None
        for prefix, name in self.slug_map:
            if slug == prefix or slug.startswith(prefix + "-"):
                return name
        return None

    def resolve_agy(self, cid: str) -> tuple[str | None, str | None]:
        cur = cid
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur in self.by_session:
                return self.by_session[cur], self.agy_uris.get(cur)
            uri = self.agy_uris.get(cur)
            if uri and (proj := self.resolve_cwd(uri)):
                return proj, uri
            cur = self.agy_parents.get(cur)
        return None, self.agy_uris.get(cid)


# ── Parsing transcripts ──────────────────────────────────────────────────────

def _parse_claude_file(path: Path) -> tuple[str | None, list[tuple]]:
    """Return (cwd, [(turn_id, ts, model, inp, cw, cr, out), ...])."""
    cwd = None
    turns: dict[str, tuple] = {}
    mtime = path.stat().st_mtime
    with open(path, "rb") as f:
        for line in f:
            if cwd is None and b'"cwd":"' in line:
                if m := _CWD_RE.search(line):
                    cwd = m.group(1).decode("utf-8", "replace")
            u_pos = line.find(b'"usage":{')
            if u_pos < 0 or b'"type":"assistant"' not in line:
                continue
            m_id = _MID_RE.search(line)
            if not m_id:
                continue
            u_slice = line[u_pos:u_pos + 320]
            m_ts = _TS_RE.search(line)
            m_mod = _MOD_RE.search(line)
            inp = int(m.group(1)) if (m := _IN_RE.search(u_slice)) else 0
            cw = int(m.group(1)) if (m := _CC_RE.search(u_slice)) else 0
            cr = int(m.group(1)) if (m := _CR_RE.search(u_slice)) else 0
            out = int(m.group(1)) if (m := _OUT_RE.search(u_slice)) else 0
            if not (inp or cw or cr or out):
                continue
            ts = _iso_ts(m_ts.group(1).decode("utf-8", "replace") if m_ts else None, mtime)
            model = m_mod.group(1).decode("utf-8", "replace") if m_mod else None
            tid = m_id.group(1).decode("utf-8", "replace")
            turns[tid] = (tid, ts, model, inp, cw, cr, out)
    return cwd, list(turns.values())


def _parse_agy_file(path: Path) -> list[tuple]:
    """Return [(turn_id, ts, model, inp, cw, cr, out), ...] from an Antigravity
    transcript.jsonl file."""
    turns: dict[str, tuple] = {}
    model = None
    mtime = path.stat().st_mtime
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "Model Selection" in line:
                if m := _AGY_MOD_RE.search(line):
                    model = m.group(1).strip()
            if '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "PLANNER_RESPONSE":
                continue
            inp = int(rec.get("input_tokens") or 0)
            cr = int(rec.get("cache_read_tokens") or 0)
            out = int(rec.get("output_tokens") or 0)
            if not (inp or cr or out):
                continue
            tid = str(rec.get("step_index") if rec.get("step_index") is not None else len(turns))
            ts = _iso_ts(rec.get("created_at"), mtime)
            turns[tid] = (tid, ts, model, inp, 0, cr, out)
    return list(turns.values())


def sync_transcripts(db: sqlite3.Connection, projects: list[dict],
                     days: int = events.RETENTION_DAYS) -> int:
    """Incrementally index any Claude Code or Antigravity transcript modified
    in the last `days` days whose (mtime, size) changed. Returns the number of
    files parsed."""
    ensure_schema(db)
    cutoff = time.time() - days * 86400
    known = {
        r["path"]: (r["mtime"], r["size"], r["project"], r["cwd"])
        for r in db.execute("SELECT path, mtime, size, project, cwd FROM usage_files")
    }
    resolver = ProjectResolver(db, projects)
    parsed = 0

    # 1. Claude Code transcripts (~/.claude/projects/**/*.jsonl)
    if CLAUDE_PROJECTS.exists():
        for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(p)
            prev = known.get(key)
            if prev and abs(prev[0] - st.st_mtime) < 1e-3 and prev[1] == st.st_size:
                if prev[2] is None:
                    proj = (resolver.by_path.get(key)
                            or resolver.resolve_cwd(prev[3])
                            or resolver.resolve_slug(p.parent.name))
                    if proj:
                        db.execute("UPDATE usage_files SET project = ? WHERE path = ?", (proj, key))
                        db.execute("UPDATE usage_turns SET project = ? WHERE path = ?", (proj, key))
                continue
            try:
                cwd, turns = _parse_claude_file(p)
            except OSError:
                continue
            slug = p.parent.parent.parent.name if p.parent.name == "subagents" else p.parent.name
            proj = (resolver.by_path.get(key)
                    or resolver.by_session.get(p.stem)
                    or resolver.resolve_cwd(cwd)
                    or resolver.resolve_slug(slug))
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT OR REPLACE INTO usage_files (path, agent, project, cwd, mtime, size) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, "claude", proj, cwd, st.st_mtime, st.st_size))
                if turns:
                    db.executemany(
                        "INSERT OR REPLACE INTO usage_turns "
                        "(path, turn_id, ts, agent, project, model, input_tokens, "
                        "cache_write, cache_read, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(key, tid, ts, "claude", proj, mod, inp, cw, cr, out)
                         for tid, ts, mod, inp, cw, cr, out in turns])
                db.execute("COMMIT")
                parsed += 1
            except BaseException:
                db.execute("ROLLBACK")
                raise

    # 2. Antigravity transcripts (~/.gemini/antigravity-cli/brain/*/transcript.jsonl)
    if AGY_BRAIN.exists():
        for p in AGY_BRAIN.glob("*/.system_generated/logs/transcript.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(p)
            cid = p.parent.parent.parent.name
            prev = known.get(key)
            if prev and abs(prev[0] - st.st_mtime) < 1e-3 and prev[1] == st.st_size:
                if prev[2] is None:
                    proj, uri = resolver.resolve_agy(cid)
                    if proj:
                        db.execute("UPDATE usage_files SET project = ?, cwd = COALESCE(cwd, ?) "
                                   "WHERE path = ?", (proj, uri, key))
                        db.execute("UPDATE usage_turns SET project = ? WHERE path = ?", (proj, key))
                continue
            try:
                turns = _parse_agy_file(p)
            except OSError:
                continue
            proj, uri = resolver.resolve_agy(cid)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT OR REPLACE INTO usage_files (path, agent, project, cwd, mtime, size) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, "agy", proj, uri, st.st_mtime, st.st_size))
                if turns:
                    db.executemany(
                        "INSERT OR REPLACE INTO usage_turns "
                        "(path, turn_id, ts, agent, project, model, input_tokens, "
                        "cache_write, cache_read, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(key, tid, ts, "agy", proj, mod, inp, cw, cr, out)
                         for tid, ts, mod, inp, cw, cr, out in turns])
                db.execute("COMMIT")
                parsed += 1
            except BaseException:
                db.execute("ROLLBACK")
                raise

    return parsed


# ── Service remaining credits & rate-limit quotas ────────────────────────────

def read_claude_quota() -> dict | None:
    """Extract Claude Code rate-limit windows and remaining dollar credits from
    ~/.claude.json (cachedUsageUtilization)."""
    try:
        raw = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return None
    cu = raw.get("cachedUsageUtilization")
    if not isinstance(cu, dict):
        return None
    util = cu.get("utilization") or {}
    fetched_ms = cu.get("fetchedAtMs")
    fetched_ts = float(fetched_ms) / 1000.0 if fetched_ms else CLAUDE_JSON.stat().st_mtime

    buckets = []
    fh = util.get("five_hour")
    if isinstance(fh, dict) and fh.get("utilization") is not None:
        used = round(float(fh["utilization"]), 1)
        buckets.append({
            "id": "5h",
            "label": "5h",
            "name": "5-Hour Window",
            "window": "5h",
            "used_pct": used,
            "remaining_pct": round(max(0.0, 100.0 - used), 1),
            "resets_at": fh.get("resets_at"),
        })
    sd = util.get("seven_day")
    if isinstance(sd, dict) and sd.get("utilization") is not None:
        used = round(float(sd["utilization"]), 1)
        buckets.append({
            "id": "7d",
            "label": "7d",
            "name": "Weekly Window",
            "window": "weekly",
            "used_pct": used,
            "remaining_pct": round(max(0.0, 100.0 - used), 1),
            "resets_at": sd.get("resets_at"),
        })
    for lim in util.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = (lim.get("scope") or {}).get("model") or {}
        mname = scope.get("display_name") or scope.get("id")
        if mname and lim.get("percent") is not None:
            used = round(float(lim["percent"]), 1)
            buckets.append({
                "id": f"7d-{mname.lower()}",
                "label": f"{mname} 7d",
                "name": f"{mname} Weekly",
                "window": "weekly",
                "used_pct": used,
                "remaining_pct": round(max(0.0, 100.0 - used), 1),
                "resets_at": lim.get("resets_at"),
            })

    # Promotional / dollar credit buckets (e.g. iguana_necktie: $250 grant).
    grants = []
    for k, v in util.items():
        if k in ("five_hour", "seven_day", "extra_usage", "spend", "limits",
                 "seven_day_breakdown", "weekly_scoped_shares"):
            continue
        if isinstance(v, dict) and v.get("limit_dollars") is not None:
            lim_d = float(v["limit_dollars"] or 0)
            rem_d = float(v["remaining_dollars"]) if v.get("remaining_dollars") is not None else max(0.0, lim_d - float(v.get("used_dollars") or 0))
            used_d = float(v["used_dollars"]) if v.get("used_dollars") is not None else max(0.0, lim_d - rem_d)
            used_pct = float(v.get("utilization") or (used_d / lim_d * 100.0 if lim_d else 0.0))
            grants.append({
                "key": k,
                "limit_dollars": round(lim_d, 2),
                "used_dollars": round(used_d, 2),
                "remaining_dollars": round(rem_d, 2),
                "used_pct": round(used_pct, 1),
                "remaining_pct": round(max(0.0, 100.0 - used_pct), 1),
                "resets_at": v.get("resets_at"),
            })

    extra = util.get("extra_usage") or {}
    dec = int(extra.get("decimal_places") if extra.get("decimal_places") is not None else 2)
    scale = 10 ** dec
    extra_info = {
        "enabled": bool(extra.get("is_enabled")),
        "disabled_reason": extra.get("disabled_reason") or raw.get("cachedExtraUsageDisabledReason"),
        "used_dollars": round(float(extra.get("used_credits") or 0) / scale, 2),
        "limit_dollars": round(float(extra.get("monthly_limit") or 0) / scale, 2),
        "currency": extra.get("currency") or "USD",
    }

    primary_grant = grants[0] if grants else None
    credits = {
        "kind": "usd",
        "remaining_dollars": primary_grant["remaining_dollars"] if primary_grant else None,
        "limit_dollars": primary_grant["limit_dollars"] if primary_grant else None,
        "used_dollars": primary_grant["used_dollars"] if primary_grant else None,
        "resets_at": primary_grant["resets_at"] if primary_grant else None,
        "grants": grants,
        "extra_usage": extra_info,
    }
    return {
        "service": "claude",
        "label": "Claude Code",
        "fetched_at": fetched_ts,
        "buckets": buckets,
        "credits": credits,
    }


def fetch_agy_quota() -> dict | None:
    """Query Antigravity's non-interactive `/usage` and `/credits` commands."""
    agy_bin = shutil.which("agy") or str(AGY_DIR / "bin" / "agy")
    if not Path(agy_bin).exists():
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["VP_STATUS_HOOK"] = "off"

    def run_cmd(slash: str) -> dict | None:
        try:
            p = subprocess.run(
                [agy_bin, "-p", slash, "--output-format", "json"],
                capture_output=True, text=True, env=env, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0 or not p.stdout.strip():
            return None
        for line in reversed(p.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    u_res = run_cmd("/usage")
    c_res = run_cmd("/credits")
    if not u_res and not c_res:
        return None

    buckets = []
    groups = (((u_res or {}).get("command") or {}).get("data") or {}).get("groups") or []
    for g in groups:
        gname = g.get("name") or ""
        short_group = ("Gemini" if "Gemini" in gname
                       else "Claude/GPT" if "Claude" in gname else gname)
        g_buckets = sorted(
            g.get("buckets") or [],
            key=lambda x: 0 if (x.get("window") == "5h" or "5h" in (x.get("id") or "")) else 1
        )
        for b in g_buckets:
            rem_frac = b.get("remaining_fraction")
            if rem_frac is None:
                continue
            rem_pct = round(max(0.0, min(100.0, float(rem_frac) * 100.0)), 1)
            used_pct = round(max(0.0, 100.0 - rem_pct), 1)
            win = b.get("window") or ("5h" if "5h" in (b.get("id") or "") else "weekly")
            wlabel = "5h" if win == "5h" else "7d"
            buckets.append({
                "id": b.get("id") or f"{short_group.lower()}-{wlabel}",
                "group": gname,
                "label": f"{short_group} {wlabel}",
                "name": f"{gname} · {b.get('name') or wlabel}",
                "window": win,
                "used_pct": used_pct,
                "remaining_pct": rem_pct,
                "resets_at": b.get("reset_time"),
                "description": b.get("description"),
            })

    c_data = (((c_res or {}).get("command") or {}).get("data") or {})
    rem_credits = c_data.get("remaining_credits")
    now = time.time()
    return {
        "service": "agy",
        "label": "Antigravity",
        "fetched_at": now,
        "buckets": buckets,
        "credits": {
            "kind": "credits",
            "remaining_credits": int(rem_credits) if rem_credits is not None else None,
        },
    }


_claude_mtime: float = 0.0


def sync_quotas(db: sqlite3.Connection, force: bool = False) -> None:
    """Keep service_quota up to date for both Claude Code and Antigravity."""
    global _claude_mtime
    ensure_schema(db)
    now = time.time()

    # 1. Claude Code (~/.claude.json is local and cheap)
    try:
        mtime = CLAUDE_JSON.stat().st_mtime if CLAUDE_JSON.exists() else 0.0
    except OSError:
        mtime = 0.0
    if force or mtime != _claude_mtime:
        cq = read_claude_quota()
        if cq:
            db.execute("INSERT OR REPLACE INTO service_quota VALUES (?,?,?)",
                       ("claude", now, json.dumps(cq)))
            _claude_mtime = mtime

    # 2. Antigravity (`agy -p "/usage"` takes ~2-3s, so throttle thoughtfully)
    row = db.execute("SELECT ts FROM service_quota WHERE service = 'agy'").fetchone()
    last_ts = float(row["ts"]) if row else 0.0
    need_agy = force or (now - last_ts >= AGY_QUOTA_TTL)
    if not need_agy and now - last_ts >= AGY_POST_TURN_MIN:
        # Refresh soon after an Antigravity turn finishes.
        ev = db.execute(
            "SELECT MAX(ts) AS ts FROM events WHERE agent_type = 'antigravity' "
            "AND event IN ('stop', 'stop_error', 'settled')"
        ).fetchone()
        if ev and ev["ts"] and float(ev["ts"]) > last_ts:
            need_agy = True
    if need_agy:
        aq = fetch_agy_quota()
        if aq:
            db.execute("INSERT OR REPLACE INTO service_quota VALUES (?,?,?)",
                       ("agy", now, json.dumps(aq)))


# ── Querying snapshots for the UI ────────────────────────────────────────────

def _empty_window() -> dict:
    return {
        "turns": 0,
        "input_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "active_tokens": 0,
        "total_tokens": 0,
        "weighted": 0.0,
    }


def _add_window(dst: dict, src: dict) -> None:
    for k in ("turns", "input_tokens", "cache_write_tokens", "cache_read_tokens",
              "output_tokens", "active_tokens", "total_tokens"):
        dst[k] += src.get(k, 0)
    dst["weighted"] = round(dst.get("weighted", 0.0) + src.get("weighted", 0.0), 1)


def snapshot(db: sqlite3.Connection,
             aliases_by_owner: dict[str, list[str]] | None = None) -> dict:
    """Return service quotas + 5h/24h/7d token usage per service, per project,
    and per agent instance."""
    ensure_schema(db)
    now = time.time()
    t_5h = now - 5 * 3600
    t_24h = now - 24 * 3600
    t_7d = now - 7 * 86400

    rows = db.execute(
        """SELECT
               project,
               agent,
               SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS turns_5h,
               SUM(CASE WHEN ts >= ? THEN input_tokens ELSE 0 END) AS in_5h,
               SUM(CASE WHEN ts >= ? THEN cache_write ELSE 0 END) AS cw_5h,
               SUM(CASE WHEN ts >= ? THEN cache_read ELSE 0 END) AS cr_5h,
               SUM(CASE WHEN ts >= ? THEN output_tokens ELSE 0 END) AS out_5h,
               SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS turns_24h,
               SUM(CASE WHEN ts >= ? THEN input_tokens ELSE 0 END) AS in_24h,
               SUM(CASE WHEN ts >= ? THEN cache_write ELSE 0 END) AS cw_24h,
               SUM(CASE WHEN ts >= ? THEN cache_read ELSE 0 END) AS cr_24h,
               SUM(CASE WHEN ts >= ? THEN output_tokens ELSE 0 END) AS out_24h,
               COUNT(*) AS turns_7d,
               SUM(input_tokens) AS in_7d,
               SUM(cache_write) AS cw_7d,
               SUM(cache_read) AS cr_7d,
               SUM(output_tokens) AS out_7d,
               MAX(ts) AS last_ts
           FROM usage_turns
           WHERE ts >= ?
           GROUP BY project, agent""",
        (t_5h, t_5h, t_5h, t_5h, t_5h,
         t_24h, t_24h, t_24h, t_24h, t_24h,
         t_7d),
    ).fetchall()

    def make_win(turns, inp, cw, cr, out) -> dict:
        inp, cw, cr, out = int(inp or 0), int(cw or 0), int(cr or 0), int(out or 0)
        return {
            "turns": int(turns or 0),
            "input_tokens": inp,
            "cache_write_tokens": cw,
            "cache_read_tokens": cr,
            "output_tokens": out,
            "active_tokens": inp + cw + out,
            "total_tokens": inp + cw + cr + out,
            "weighted": round(_weighted(inp, cw, cr, out), 1),
        }

    svc_totals = {
        a: {"h5": _empty_window(), "d1": _empty_window(), "d7": _empty_window()}
        for a in ("claude", "agy")
    }
    raw_pair: dict[tuple[str, str], dict] = {}

    for r in rows:
        agent = r["agent"]
        if agent not in svc_totals:
            continue
        w5 = make_win(r["turns_5h"], r["in_5h"], r["cw_5h"], r["cr_5h"], r["out_5h"])
        w1 = make_win(r["turns_24h"], r["in_24h"], r["cw_24h"], r["cr_24h"], r["out_24h"])
        w7 = make_win(r["turns_7d"], r["in_7d"], r["cw_7d"], r["cr_7d"], r["out_7d"])
        _add_window(svc_totals[agent]["h5"], w5)
        _add_window(svc_totals[agent]["d1"], w1)
        _add_window(svc_totals[agent]["d7"], w7)
        proj = r["project"]
        if proj:
            raw_pair[(proj, agent)] = {
                "h5": w5, "d1": w1, "d7": w7, "last_ts": r["last_ts"],
            }

    def finalize_entry(agent: str, entry: dict) -> dict:
        svc_w = svc_totals[agent]["d7"]["weighted"]
        w7 = entry["d7"]["weighted"]
        share = round(w7 / svc_w * 100.0, 1) if svc_w > 0 and w7 > 0 else 0.0
        return {**entry, "share_7d_pct": share}

    # Per-project usage (each project on its own, plus rolled-up with aliases
    # for session owners on the Agents home screen).
    all_projects = {p for p, _ in raw_pair}
    if aliases_by_owner:
        all_projects |= set(aliases_by_owner.keys())

    by_project: dict[str, dict] = {}
    by_instance: dict[tuple[str, str], dict] = {}

    for proj in all_projects:
        group = [proj] + (aliases_by_owner.get(proj, []) if aliases_by_owner else [])
        proj_agents = {}
        for agent in ("claude", "agy"):
            merged = {"h5": _empty_window(), "d1": _empty_window(),
                      "d7": _empty_window(), "last_ts": None}
            for member in dict.fromkeys(group):
                item = raw_pair.get((member, agent))
                if not item:
                    continue
                _add_window(merged["h5"], item["h5"])
                _add_window(merged["d1"], item["d1"])
                _add_window(merged["d7"], item["d7"])
                if item["last_ts"] and (merged["last_ts"] is None or item["last_ts"] > merged["last_ts"]):
                    merged["last_ts"] = item["last_ts"]
            if merged["d7"]["turns"] > 0:
                fin = finalize_entry(agent, merged)
                by_instance[(proj, agent)] = fin
                proj_agents[agent] = fin
        if proj_agents:
            by_project[proj] = proj_agents

    # Service quotas + totals
    quotas = {}
    for r in db.execute("SELECT service, ts, data FROM service_quota"):
        try:
            quotas[r["service"]] = json.loads(r["data"])
        except json.JSONDecodeError:
            pass
    if "claude" not in quotas:
        if cq := read_claude_quota():
            quotas["claude"] = cq

    services = {}
    for svc, label in (("claude", "Claude Code"), ("agy", "Antigravity")):
        q = quotas.get(svc) or {"service": svc, "label": label,
                                "fetched_at": None, "buckets": [], "credits": None}
        services[svc] = {
            **q,
            "usage": svc_totals[svc],
        }

    return {
        "services": services,
        "by_instance": by_instance,
        "by_project": by_project,
    }
