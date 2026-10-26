"""Jira: the tickets behind the projects — read them, link them, move them,
comment on them, and open new ones.

Configured by the [jira] table of Radio Room's config; on when `url` is set.
Works with Jira Cloud and Data Center/Server alike — which one is asked of the
server once (serverInfo) rather than configured:

    Cloud        basic auth with `email` and an API token; search through
                 /rest/api/3/search/jql (the v2 search is gone there)
    Data Center  a personal access token as a bearer token; /rest/api/2/search

Everything else goes through REST v2, which both still serve, so descriptions
and comments are plain text (wiki markup) rather than Atlassian's document
format.

The token is never in the config file. It comes from `token_cmd` (a command
that prints it — `secret-tool lookup service jira`, `pass jira`), else the
environment variable named by `token_env` (default JIRA_TOKEN), else
`token_file`.

Stdlib only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import config as radio_config

DEFAULTS = {
    "url": "",
    "email": "",                 # Cloud: the account the API token belongs to
    "token_cmd": "",
    "token_env": "JIRA_TOKEN",
    "token_file": "",
    "projects": [],              # project keys a branch name may cite
    "jql": "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
    "default_project": "",       # for new tickets
    "issue_type": "Task",
    "max_results": 50,
    "timeout": 20,
}

FIELDS = "summary,status,issuetype,priority,assignee,updated,project"


def settings() -> dict:
    return radio_config.section("jira", DEFAULTS)


def enabled() -> bool:
    return bool(settings()["url"])


# ── Ticket keys in text ──────────────────────────────────────────────────────

def keys_in(text: str) -> list[str]:
    """Ticket keys named in a branch (or any) name. With `projects` set, any
    case of those keys counts (abc-123-fix-login); without, only an
    upper-case KEY-123, so "utf-8" isn't mistaken for a ticket."""
    if not text:
        return []
    projects = [p.upper() for p in settings()["projects"]]
    if projects:
        alts = "|".join(re.escape(p) for p in projects)
        found = re.findall(rf"(?<![A-Za-z0-9])((?:{alts})-\d+)(?![A-Za-z0-9])", text, re.I)
    else:
        found = re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+-\d+)(?![A-Za-z0-9])", text)
    out = []
    for k in (f.upper() for f in found):
        if k not in out:
            out.append(k)
    return out


# ── The client ───────────────────────────────────────────────────────────────

class JiraError(RuntimeError):
    pass


def _token(s: dict) -> str:
    if s["token_cmd"]:
        try:
            r = subprocess.run(shlex.split(s["token_cmd"]), capture_output=True,
                               text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            raise JiraError(f"token_cmd failed: {e}")
        if r.returncode != 0 or not r.stdout.strip():
            raise JiraError(f"token_cmd gave no token ({r.stderr.strip() or 'exit ' + str(r.returncode)})")
        return r.stdout.strip()
    if os.environ.get(s["token_env"]):
        return os.environ[s["token_env"]].strip()
    if s["token_file"]:
        try:
            return open(os.path.expanduser(s["token_file"])).read().strip()
        except OSError as e:
            raise JiraError(f"token_file: {e}")
    raise JiraError(f"no Jira token: set token_cmd, ${s['token_env']} or token_file")


class Jira:
    def __init__(self, s: dict | None = None):
        self.s = s or settings()
        if not self.s["url"]:
            raise JiraError("Jira is not configured ([jira] url)")
        self.base = self.s["url"].rstrip("/")
        self._auth: str | None = None
        self._info: dict | None = None
        self._me: dict | None = None

    # -- plumbing ------------------------------------------------------------

    def auth(self) -> str:
        if self._auth is None:
            tok = _token(self.s)
            if self.s["email"]:
                raw = f"{self.s['email']}:{tok}".encode()
                self._auth = "Basic " + base64.b64encode(raw).decode()
            else:
                self._auth = "Bearer " + tok
        return self._auth

    def request(self, method: str, path: str, body: dict | None = None,
                params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self.auth(), "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.s["timeout"]) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise JiraError(f"{method} {path}: HTTP {e.code} {_error_text(e.read())}")
        except (urllib.error.URLError, OSError) as e:
            raise JiraError(f"can't reach {self.base}: {e}")
        return json.loads(raw) if raw.strip() else None

    def server_info(self) -> dict:
        if self._info is None:
            self._info = self.request("GET", "/rest/api/2/serverInfo") or {}
        return self._info

    def cloud(self) -> bool:
        return self.server_info().get("deploymentType") == "Cloud"

    def myself(self) -> dict:
        if self._me is None:
            self._me = self.request("GET", "/rest/api/2/myself") or {}
        return self._me

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    # -- reading -------------------------------------------------------------

    def search(self, jql: str | None = None, limit: int | None = None) -> list[dict]:
        params = {"jql": jql or self.s["jql"], "fields": FIELDS,
                  "maxResults": limit or self.s["max_results"]}
        path = "/rest/api/3/search/jql" if self.cloud() else "/rest/api/2/search"
        data = self.request("GET", path, params=params) or {}
        return [self._brief(i) for i in data.get("issues", [])]

    def issue(self, key: str) -> dict:
        data = self.request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                            params={"fields": FIELDS + ",description,comment"})
        out = self._brief(data)
        f = data.get("fields") or {}
        out["description"] = f.get("description") or ""
        out["comments"] = [{"author": (c.get("author") or {}).get("displayName", ""),
                            "created": c.get("created", ""), "body": c.get("body", "")}
                           for c in ((f.get("comment") or {}).get("comments") or [])]
        return out

    def transitions(self, key: str) -> list[dict]:
        data = self.request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions") or {}
        return [{"id": t["id"], "name": t["name"],
                 "to": (t.get("to") or {}).get("name", t["name"])}
                for t in data.get("transitions", [])]

    # -- writing -------------------------------------------------------------

    def transition(self, key: str, to: str) -> dict:
        """Move a ticket, by transition id, transition name or target status
        name (case-insensitive)."""
        ts = self.transitions(key)
        want = to.strip().lower()
        t = next((t for t in ts if want in (t["id"], t["name"].lower(), t["to"].lower())), None)
        if t is None:
            raise JiraError(f"{key} can't move to '{to}' from here; "
                            f"it can go to: {', '.join(x['to'] for x in ts) or 'nowhere'}")
        self.request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions",
                     {"transition": {"id": t["id"]}})
        return t

    def comment(self, key: str, text: str) -> None:
        self.request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                     {"body": text})

    def assign_to_me(self, key: str) -> None:
        me = self.myself()
        body = {"accountId": me["accountId"]} if self.cloud() else {"name": me.get("name")}
        self.request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}/assignee", body)

    def create(self, summary: str, description: str = "", project: str | None = None,
               issue_type: str | None = None, assign_me: bool = True) -> str:
        project = project or self.s["default_project"]
        if not project:
            raise JiraError("no project key: pass one or set [jira] default_project")
        fields = {"project": {"key": project}, "summary": summary,
                  "issuetype": {"name": issue_type or self.s["issue_type"]}}
        if description:
            fields["description"] = description
        data = self.request("POST", "/rest/api/2/issue", {"fields": fields}) or {}
        key = data.get("key")
        if not key:
            raise JiraError(f"Jira didn't say what it created: {data}")
        if assign_me:
            try:
                self.assign_to_me(key)
            except JiraError:
                pass   # created is what matters; assignment may be restricted
        return key

    # -- shape ---------------------------------------------------------------

    def _brief(self, i: dict) -> dict:
        f = i.get("fields") or {}
        status = f.get("status") or {}
        return {
            "key": i.get("key", ""),
            "summary": f.get("summary") or "",
            "status": status.get("name", ""),
            # new / indeterminate / done — for colouring
            "category": (status.get("statusCategory") or {}).get("key", ""),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "priority": (f.get("priority") or {}).get("name", ""),
            "assignee": (f.get("assignee") or {}).get("displayName", ""),
            "updated": f.get("updated", ""),
            "project": (f.get("project") or {}).get("key", ""),
            "url": self.browse_url(i.get("key", "")),
        }


def _error_text(raw: bytes) -> str:
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode(errors="replace")[:200]
    msgs = list(d.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (d.get("errors") or {}).items()]
    return "; ".join(msgs) or str(d)[:200]


def branch_for(key: str, summary: str = "") -> str:
    """The branch a ticket's work goes on: ABC-123-short-summary."""
    from git import slug
    return f"{key}-{slug(summary, 40)}" if summary else key
