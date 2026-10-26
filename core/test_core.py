#!/usr/bin/env python3
"""Tests for core/: the Jira client against a fake Jira (Cloud and Data
Center), ticket keys in branch names, worktree projects in a scratch
repository, and what a `bind` setting listens on. Nothing here touches the real config, registry or Jira.

    python3 core/test_core.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRATCH = tempfile.mkdtemp(prefix="radio-room-test-")
os.environ["RADIO_ROOM_CONFIG"] = str(Path(SCRATCH) / "config.toml")
Path(os.environ["RADIO_ROOM_CONFIG"]).write_text(f'''
projects = "{SCRATCH}/projects.toml"
agents = ["claude"]
worktree_dir = "{{parent}}/{{reponame}}.wt/{{name}}"
[jira]
projects = ["ABC"]
''')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import git  # noqa: E402
import jira  # noqa: E402
import listen  # noqa: E402
import registry  # noqa: E402


class FakeJira(BaseHTTPRequestHandler):
    """Just enough of Jira's REST API, recording what it was asked."""
    deployment = "Cloud"
    calls: list = []

    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def issue(self, key):
        return {"key": key, "fields": {
            "summary": "Login fails on Safari", "project": {"key": "ABC"},
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
            "assignee": {"displayName": "Me"}, "updated": "2026-10-24T10:00:00.000+0000",
            "description": "Steps: open Safari.",
            "comment": {"comments": [{"author": {"displayName": "Ann"},
                                      "created": "2026-10-23T09:00", "body": "Seen it too"}]}}}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        FakeJira.calls.append(("GET", u.path, q, self.headers.get("Authorization")))
        if u.path == "/rest/api/2/serverInfo":
            return self.reply(200, {"deploymentType": self.deployment})
        if u.path == "/rest/api/2/myself":
            return self.reply(200, {"accountId": "acc-1", "name": "me"})
        if u.path in ("/rest/api/3/search/jql", "/rest/api/2/search"):
            return self.reply(200, {"issues": [self.issue("ABC-123"), self.issue("ABC-7")]})
        if u.path == "/rest/api/2/issue/ABC-123":
            return self.reply(200, self.issue("ABC-123"))
        if u.path == "/rest/api/2/issue/ABC-123/transitions":
            return self.reply(200, {"transitions": [
                {"id": "11", "name": "Start progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}}]})
        if u.path == "/rest/api/2/issue/NOPE-1":
            return self.reply(404, {"errorMessages": ["Issue does not exist"]})
        self.reply(404, {"errorMessages": [f"no {u.path}"]})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_POST(self):
        b = self.body()
        FakeJira.calls.append(("POST", self.path, b, self.headers.get("Authorization")))
        if self.path == "/rest/api/2/issue":
            return self.reply(201, {"key": "ABC-200"})
        self.reply(204)

    def do_PUT(self):
        FakeJira.calls.append(("PUT", self.path, self.body(), self.headers.get("Authorization")))
        self.reply(204)


class JiraClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeJira)   # an ephemeral port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self, deployment, email=""):
        FakeJira.deployment = deployment
        FakeJira.calls = []
        os.environ["JIRA_TEST_TOKEN"] = "tok"
        return jira.Jira({**jira.DEFAULTS, "url": self.url, "email": email,
                          "token_env": "JIRA_TEST_TOKEN", "default_project": "ABC"})

    def test_cloud_search_uses_v3_jql_and_basic_auth(self):
        j = self.client("Cloud", email="me@example.com")
        issues = j.search()
        self.assertEqual([i["key"] for i in issues], ["ABC-123", "ABC-7"])
        self.assertEqual(issues[0]["status"], "To Do")
        self.assertEqual(issues[0]["category"], "new")
        self.assertTrue(issues[0]["url"].endswith("/browse/ABC-123"))
        get = [c for c in FakeJira.calls if c[1].endswith("/search/jql")][0]
        self.assertIn("currentUser()", get[2]["jql"][0])
        self.assertTrue(get[3].startswith("Basic "))

    def test_datacenter_search_uses_v2_and_bearer(self):
        j = self.client("Server")
        j.search("project = ABC")
        get = [c for c in FakeJira.calls if "search" in c[1]][0]
        self.assertEqual(get[1], "/rest/api/2/search")
        self.assertEqual(get[3], "Bearer tok")

    def test_issue_detail(self):
        i = self.client("Cloud").issue("ABC-123")
        self.assertEqual(i["description"], "Steps: open Safari.")
        self.assertEqual(i["comments"][0]["author"], "Ann")

    def test_transition_by_status_name(self):
        j = self.client("Cloud")
        t = j.transition("ABC-123", "in progress")
        self.assertEqual(t["id"], "11")
        post = [c for c in FakeJira.calls if c[0] == "POST"][0]
        self.assertEqual(post[2], {"transition": {"id": "11"}})
        with self.assertRaises(jira.JiraError) as e:
            j.transition("ABC-123", "Blocked")
        self.assertIn("In Progress", str(e.exception))

    def test_comment(self):
        self.client("Cloud").comment("ABC-123", "Fixed in abc-123 branch")
        post = [c for c in FakeJira.calls if c[0] == "POST"][0]
        self.assertEqual(post[1], "/rest/api/2/issue/ABC-123/comment")
        self.assertEqual(post[2], {"body": "Fixed in abc-123 branch"})

    def test_create_assigns_by_account_on_cloud_and_name_on_dc(self):
        key = self.client("Cloud").create("New thing", "details")
        self.assertEqual(key, "ABC-200")
        put = [c for c in FakeJira.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2], {"accountId": "acc-1"})
        create = [c for c in FakeJira.calls if c[1] == "/rest/api/2/issue"][0]
        self.assertEqual(create[2]["fields"]["project"], {"key": "ABC"})
        self.assertEqual(create[2]["fields"]["issuetype"], {"name": "Task"})
        self.client("Server").create("Other")
        put = [c for c in FakeJira.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2], {"name": "me"})

    def test_errors_carry_jiras_message(self):
        with self.assertRaises(jira.JiraError) as e:
            self.client("Cloud").issue("NOPE-1")
        self.assertIn("Issue does not exist", str(e.exception))

    def test_no_token_is_an_error_not_a_crash(self):
        j = jira.Jira({**jira.DEFAULTS, "url": self.url, "token_env": "NO_SUCH_VAR_X"})
        with self.assertRaises(jira.JiraError):
            j.search()


class Keys(unittest.TestCase):
    def test_keys_in_branch_names(self):
        self.assertEqual(jira.keys_in("abc-123-fix-login"), ["ABC-123"])
        self.assertEqual(jira.keys_in("feature/ABC-9_and_abc-10"), ["ABC-9", "ABC-10"])
        self.assertEqual(jira.keys_in("utf-8-cleanup"), [])
        self.assertEqual(jira.keys_in("XYZ-1"), [])          # not a configured project
        self.assertEqual(jira.keys_in("abc-1234x"), [])       # not a whole key

    def test_branch_for(self):
        self.assertEqual(jira.branch_for("ABC-1", "Login fails on Safari!"),
                         "ABC-1-login-fails-on-safari")


class Worktrees(unittest.TestCase):
    def setUp(self):
        self.repo = Path(SCRATCH) / "mono"
        if not self.repo.exists():
            (self.repo / "services" / "api").mkdir(parents=True)
            (self.repo / "services" / "api" / "x.txt").write_text("x")
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "init"]):
                subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=env)
        cfg = registry.load()
        if not registry.find(cfg, "mono"):
            cfg["projects"].append({"name": "mono", "dir": str(self.repo)})
            registry.save(cfg)

    def test_worktree_project_lifecycle(self):
        p = registry.add_worktree_project("login", "mono", ticket="ABC-5",
                                          subdir="services/api")
        self.assertTrue(p["dir"].endswith("mono.wt/login/services/api"))
        e = next(x for x in registry.status(registry.load()) if x["name"] == "login")
        self.assertEqual(e["git"]["branch"], "ABC-5")
        self.assertTrue(e["git"]["worktree"])
        self.assertEqual(e["git"]["subdir"], "services/api")
        self.assertEqual(e["git"]["repo"], (git.info(self.repo) or {})["repo"])
        self.assertEqual(e["tickets"], ["ABC-5"])
        Path(p["dir"], "dirty.txt").write_text("wip")
        with self.assertRaises(ValueError):
            registry.remove_project("login", worktree=True)
        registry.remove_project("login", worktree=True, force=True)
        self.assertFalse(Path(p["dir"]).exists())
        self.assertIsNone(registry.find(registry.load(), "login"))

    def test_main_checkout_is_never_removed(self):
        with self.assertRaises(ValueError):
            registry.remove_project("mono", worktree=True)

    def test_link_and_unlink(self):
        registry.link_ticket("mono", "ABC-9")
        self.assertIn("ABC-9", registry.tickets(registry.find(registry.load(), "mono")))
        registry.link_ticket("mono", "ABC-9", unlink=True)
        self.assertNotIn("jira", registry.find(registry.load(), "mono"))


class Bind(unittest.TestCase):
    TS = "100.101.102.103"

    def addrs(self, bind, ts=TS):
        return listen.addresses(bind, tailscale_ip=lambda: ts)

    def test_no_setting_means_this_machine_only(self):
        self.assertEqual(self.addrs(None), ["127.0.0.1"])
        self.assertEqual(self.addrs("local"), ["127.0.0.1"])

    def test_tailscale_is_loopback_and_the_tailnet_but_not_the_lan(self):
        addrs = self.addrs("tailscale")
        self.assertEqual(addrs, ["127.0.0.1", self.TS])
        self.assertEqual(listen.exposed(addrs, tailscale_ip=lambda: self.TS), [])

    def test_tailscale_without_tailscale_fails_rather_than_falling_back(self):
        with self.assertRaises(listen.BindError):
            self.addrs("tailscale", ts=None)

    def test_any_and_lan_addresses_count_as_exposed(self):
        ts = lambda: self.TS  # noqa: E731
        self.assertEqual(listen.exposed(self.addrs("any"), tailscale_ip=ts), ["0.0.0.0"])
        self.assertEqual(listen.exposed(["192.168.1.20"], tailscale_ip=ts), ["192.168.1.20"])
        self.assertEqual(listen.exposed(["::1", "127.0.0.1"], tailscale_ip=ts), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
