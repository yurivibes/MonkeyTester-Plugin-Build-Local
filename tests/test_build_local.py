"""Contract tests for build-local: build_plugin.py + runner.py + sources/ against
the fake Gitea, the fake GitHub and a fake MonkeyTester viewer. No real forge is
contacted; no network beyond 127.0.0.1 and file:// fixture repos.
"""
import base64
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_plugin  # noqa: E402
import fake_gitea  # noqa: E402
import fake_github  # noqa: E402
import helpers  # noqa: E402
import sources
import runner as build_runner  # noqa: E402
from sources import RECIPES, PLATFORMS  # noqa: E402
from sources import gitea as gitea_src  # noqa: E402
from sources import github as github_src  # noqa: E402

from fake_gitea import GOOD, READ_ONLY, REPO  # GOOD = "good-token-0000abcd"

PAIR = "pair-secret-1234"
GHOST = "blackjackfruit/Ghost-Repo"
GH_TOKEN = "ghp_good_token_1234"

ANDROID_RECIPE = RECIPES["android"]
IOS_RECIPE = RECIPES["ios"]

# ─── fixture repo files (branch: {path: content}) ───

ANDROID_OK = '#!/bin/bash\necho "android build: starting"\nprintf fake-apk-bytes > "$VT_OUTPUT"\n'
IOS_OK = '#!/bin/bash\necho "ios build: starting"\nprintf fake-ipa-bytes > "$VT_OUTPUT"\n'
IOS_FAIL = ('#!/bin/bash\n'
            'echo "ios build: starting"\n'
            'echo "ios build: error: signing"\n'
            'echo "error: No matching files found."\n'
            'echo "error: signing"\n'
            'exit 1\n')
ANDROID_FAIL = '#!/bin/bash\necho "android build: error: gradle exited 1"\nexit 1\n'
SLOW = ('#!/bin/bash\n'
        'echo "slow-build: starting"\n'
        'pids="${VT_OUTPUT%/*}/pids.txt"\n'
        'printf \'%s\\n\' "$$" > "$pids"\n'
        'sleep 600 &\n'
        'printf \'%s\\n\' "$!" >> "$pids"\n'
        'sleep 600\n')

CASE_FILES = {
    "visual-tests/onboarding/INSTRUCTION.txt": "follow the onboarding flow",
    "visual-tests/device-setup/INSTRUCTION.txt": "set up the device",
    "visual-tests/notes.txt": "not a case",
}
MASTER_FILES = {"README.md": "app", ANDROID_RECIPE: ANDROID_OK, IOS_RECIPE: IOS_OK, **CASE_FILES}
FIXTURE_BRANCHES = {
    "master": MASTER_FILES,
    "feat/one-fails": {"README.md": "app", ANDROID_RECIPE: ANDROID_OK, IOS_RECIPE: IOS_FAIL, **CASE_FILES},
    "feat/all-fail": {"README.md": "app", ANDROID_RECIPE: ANDROID_FAIL, IOS_RECIPE: IOS_FAIL, **CASE_FILES},
    "feat/slow": {"README.md": "app", ANDROID_RECIPE: SLOW, **CASE_FILES},
    "feat/no-ios": {"README.md": "app", ANDROID_RECIPE: ANDROID_OK, **CASE_FILES},
    "feat/dash-error": {"README.md": "app", ANDROID_RECIPE: '#!/bin/bash\necho "error: --android-app=/etc/passwd"\nexit 1\n', **CASE_FILES},
    "feat/env-dump": {"README.md": "app", ANDROID_RECIPE: '#!/bin/bash\nenv > "$VT_OUTPUT"\n', **CASE_FILES},
}
GHOST_FILES = {"README.md": "app", ANDROID_RECIPE: "exit 0\n", IOS_RECIPE: "exit 0\n"}
API_FILES = {branch: {path: content for path, content in files.items()}
             for branch, files in FIXTURE_BRANCHES.items()}


class RunStub:
    """Stand-in for subprocess.run in detect_platforms."""

    def __init__(self, code):
        self.code = code

    def __call__(self, argv, **kwargs):
        class Result:
            returncode = self.code

        return Result()


class PluginCase(unittest.TestCase):
    """One fake Gitea + fake GitHub + fake viewer + plugin server per test."""

    pairing_token = ""
    platforms = list(PLATFORMS)

    def setUp(self):
        env = patch.dict(os.environ, {"BUILD_LOCAL_IMPORT": ""})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state_dir = self.dir / "state"
        self.record = self.dir / "viewer_records.jsonl"
        self.viewer = helpers.FakeViewer(self.record)
        self.addCleanup(self.viewer.stop)
        self.gitea, self.gitea_url, self.gs = fake_gitea.start()

        def stop_gitea(server=self.gitea):
            server.shutdown()
            server.server_close()

        self.addCleanup(stop_gitea)
        self.gh = fake_github.FakeGitHub().start()
        self.gh.add_repo(REPO, default="master", branches={b: sorted(f) for b, f in API_FILES.items()},
                         truncated=False)
        self.addCleanup(self.gh.stop)
        self.server = None

    def start_plugin(self):
        self.server = build_plugin.make_server("127.0.0.1", 0, self.state_dir,
                                               self.pairing_token, self.platforms)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop(server=self.server, thread=thread):
            build_plugin.stop_server(server)
            thread.join(5)

        self.addCleanup(stop)

    def restart_plugin(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.plugin.runner.stop()
        self.start_plugin()

    def call(self, method, path, body=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if self.pairing_token:
            h["X-Plugin-Token"] = self.pairing_token
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
                if raw:
                    return resp.status, content, resp.headers
                return resp.status, json.loads(content)
        except urllib.error.HTTPError as e:
            with e:
                if raw:
                    return e.code, e.read(), e.headers
                return e.code, json.loads(e.read() or b"{}")

    def configure(self, token=GOOD, forge_url=None, viewer=True):
        payload = {"forge_url": forge_url or self.gitea_url, "token": token}
        if viewer:
            payload["viewer_url"] = self.viewer.url
        return self.call("POST", "/config", payload)

    def configure_gh(self, token=GH_TOKEN):
        return self.call("POST", "/config", {"forge_kind": "github", "forge_url": self.gh.url,
                                             "token": token, "viewer_url": self.viewer.url})

    def start_build(self, repo=REPO, branch="master", platforms=None, cases=None, request_id="build_0123456789ab"):
        payload = {"repo": repo, "branch": branch,
                   "platforms": platforms or list(self.platforms),
                   "cases": cases if cases is not None else ["onboarding", "device-setup"],
                   "request_id": request_id}
        status, body = self.call("POST", "/builds", payload)
        self.assertEqual(status, 201, payload)
        return body["build_ref"]

    def wait_state(self, ref, wanted=("completed",), timeout=30):
        def done():
            status, body = self.call("GET", f"/builds/{ref}")
            return body if status == 200 and body.get("state") in wanted else None

        return helpers.wait_for(done, timeout=timeout)

    def body(self, ref):
        status, body = self.call("GET", f"/builds/{ref}")
        self.assertEqual(status, 200)
        return body

    def job(self, ref, name):
        for job in self.body(ref)["jobs"]:
            if job["name"] == name:
                return job
        raise AssertionError(f"no job {name}")

    def records(self):
        return self.viewer.records()

    def record_args(self, flag, index=0):
        """The value of `flag` in submit.py's argv: `--flag value` or `--flag=value`."""
        args = self.records()[index]["argv"]
        for i, arg in enumerate(args):
            if arg.startswith(flag + "="):
                return arg[len(flag) + 1:]
            if arg == flag:
                return args[i + 1]
        raise ValueError(f"{flag} not in submit.py argv")

    def out_dir(self, ref):
        return self.state_dir / "out" / ref

    def pids_dead(self, ref, timeout=10):
        path = self.out_dir(ref) / "pids.txt"
        if not path.is_file():
            return False
        pids = [int(line) for line in path.read_text().split() if line.strip().isdigit()]
        deadline = time.monotonic() + timeout
        while True:
            if all(not helpers.pid_alive(pid) for pid in pids):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def checkout(self, kind, repo=REPO):
        owner, name = repo.split("/", 1)
        return self.state_dir / "checkouts" / kind / owner / name

    def build_log(self, ref, offset=0):
        status, data, hdrs = self.call("GET", f"/builds/{ref}/log?offset={offset}", raw=True)
        self.assertEqual(status, 200)
        return data, hdrs

    def all_logs(self, ref):
        """Every text artifact a build produced."""
        texts = []
        out = self.out_dir(ref)
        if out.is_dir():
            for f in out.iterdir():
                if f.name.endswith(".log") or f.name == "submit.py":
                    texts.append(f.read_text(errors="replace"))
        for cfg in (self.state_dir / "config.json",):
            if cfg.is_file():
                pass  # config.json holds the token by design (mode 0600)
        git_cfg = self.checkout("gitea") / ".git/config"
        if git_cfg.is_file():
            texts.append(git_cfg.read_text())
        return texts


class DetectionUnitTests(unittest.TestCase):
    def test_platform_override_is_the_subset_in_contract_order(self):
        with patch.dict(os.environ, {"BUILD_LOCAL_PLATFORMS": "ios, android,ios,windows"}):
            self.assertEqual(build_plugin.detect_platforms(), ["android", "ios"])

    def test_override_beats_detection(self):
        with patch.dict(os.environ, {"BUILD_LOCAL_PLATFORMS": "android", "ANDROID_HOME": ""}):
            self.assertEqual(build_plugin.detect_platforms(system="linux", home="/nonexistent-home"),
                             ["android"])

    def test_android_sdk_env_and_paths(self):
        with tempfile.TemporaryDirectory() as sdk, tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"ANDROID_HOME": sdk}):
                self.assertEqual(build_plugin.detect_platforms(system="linux", home=home), ["android"])
            Path(home, "Android", "Sdk").mkdir(parents=True, exist_ok=True)
            self.assertEqual(build_plugin.detect_platforms(system="linux", home=home), ["android"])

    def test_ios_needs_xcodebuild_on_macos_only(self):
        with tempfile.TemporaryDirectory() as sdk, tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"ANDROID_HOME": sdk}):
                self.assertEqual(build_plugin.detect_platforms(system="darwin", home=home, run=RunStub(0)),
                                 ["android", "ios"])
                self.assertEqual(build_plugin.detect_platforms(system="darwin", home=home, run=RunStub(1)),
                                 ["android"])
                self.assertEqual(build_plugin.detect_platforms(system="linux", home=home, run=RunStub(0)),
                                 ["android"])


class PlatformGuardTests(PluginCase):
    platforms = ["android"]

    def setUp(self):
        super().setUp()
        self.start_plugin()
        self.configure()

    def test_health_platforms_and_recipes_match_the_contract(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], "monkeytester-build-plugin")
        self.assertEqual(body["contract_version"], 1)
        self.assertEqual(body["plugin"], {"name": "build-local", "version": build_plugin.PLUGIN_VERSION})
        self.assertEqual(body["platforms"], ["android"])
        self.assertEqual(body["recipes"], dict(RECIPES))
        self.assertEqual(body["capabilities"], ["logs", "cancel"])
        self.assertEqual(body["queue"], {"running": None, "waiting": 0})
        self.assertIsNone(body["error"])

    def test_host_cannot_build_other_platform(self):
        status, body = self.call("POST", "/builds",
                                 {"repo": REPO, "branch": "master", "platforms": ["android", "ios"],
                                  "cases": [], "request_id": "build_0123456789ab"})
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("can't build ios", body["error"])
        self.assertIn("it builds: android", body["error"])

    def test_env_override_without_server_argument(self):
        with patch.dict(os.environ, {"BUILD_LOCAL_PLATFORMS": "ios"}):
            plugin = build_plugin.Plugin(self.state_dir / "other", None, None)
            self.assertEqual(plugin.health()["platforms"], ["ios"])


class HealthAuthTests(PluginCase):
    pairing_token = PAIR

    def setUp(self):
        super().setUp()
        self.start_plugin()

    def test_health_needs_no_token(self):
        status, body = self.call("GET", "/health", headers={"X-Plugin-Token": "wrong"})
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], build_plugin.CONTRACT)
        self.assertEqual(body["forge"], {"kind": "gitea", "label": "Gitea"})
        self.assertTrue(body["auth_required"])

    def test_health_without_token_leaks_no_config(self):
        self.assertEqual(self.configure()[0], 200)
        status, raw, _ = self.call("GET", "/health", headers={"X-Plugin-Token": ""}, raw=True)
        self.assertEqual(status, 200)
        body = json.loads(raw)
        for key in ("viewer_url", "queue"):
            self.assertNotIn(key, body)
        self.assertNotIn("url", body["forge"])
        self.assertNotIn("user", body["forge"])
        text = raw.decode()
        for secret in (GOOD, PAIR, self.gitea_url, self.viewer.url):
            self.assertNotIn(secret, text)

    def test_health_with_token_is_the_full_contract_body(self):
        self.assertEqual(self.configure()[0], 200)
        status, raw, _ = self.call("GET", "/health", raw=True)
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(body["forge"]["configured"])
        self.assertEqual(body["forge"]["url"], self.gitea_url)
        self.assertEqual(body["viewer_url"], self.viewer.url)
        self.assertIn("queue", body)
        self.assertNotIn(GOOD, raw.decode())
        self.assertNotIn(PAIR, raw.decode())

    def test_every_other_route_needs_the_pairing_token(self):
        for method, path in (("GET", "/config"), ("POST", "/config"), ("GET", "/template?platform=android"),
                             ("GET", f"/repos/{REPO}"), ("GET", f"/repos/{REPO}/branches"),
                             ("GET", f"/repos/{REPO}/cases?ref=master"), ("POST", "/builds"),
                             ("GET", "/builds/br_0123456789abcdef"), ("GET", "/builds/br_0123456789abcdef/log"),
                             ("POST", "/builds/br_0123456789abcdef/cancel")):
            for headers in ({"X-Plugin-Token": ""}, {"X-Plugin-Token": "wrong"},
                            {"X-Plugin-Token": "", "Authorization": "Bearer wrong"}):
                status, body = self.call(method, path, {} if method == "POST" else None, headers=headers)
                self.assertEqual((status, body.get("code")), (401, "unauthorized"), (method, path))

    def test_bearer_header_is_accepted_too(self):
        status, _ = self.call("GET", "/config", headers={"X-Plugin-Token": "",
                                                         "Authorization": f"Bearer {self.pairing_token}"})
        self.assertEqual(status, 200)


class ConfigTests(PluginCase):
    def setUp(self):
        super().setUp()
        self.start_plugin()

    def test_token_checked_saved_privately_never_returned(self):
        status, body = self.configure()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        cfg = body["config"]
        self.assertEqual(cfg["forge_url"], self.gitea_url)
        self.assertEqual(cfg["user"], "monkeytester-bot")
        self.assertEqual(cfg["token"], {"configured": True, "last4": "abcd", "saved_at": cfg["token"]["saved_at"]})
        self.assertNotIn(GOOD, json.dumps(body))
        path = self.state_dir / "config.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["token"], GOOD)
        status, body = self.call("GET", "/config")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"forge_kind", "forge_url", "token", "user", "viewer_url"})
        self.assertNotIn(GOOD, json.dumps(body))
        self.assertEqual(body["token"]["last4"], "abcd")
        self.assertEqual(self.gs.calls[0][:2], ("GET", "/api/v1/user"))
        status, body = self.call("GET", "/health")
        self.assertEqual(body["forge"], {"kind": "gitea", "label": "Gitea", "url": self.gitea_url,
                                         "configured": True, "user": "monkeytester-bot"})
        self.assertEqual(body["viewer_url"], self.viewer.url)
        self.assertNotIn(GOOD, json.dumps(body))

    def test_rejected_token_stores_nothing(self):
        status, body = self.configure("wrong")
        self.assertEqual((status, body["code"], body["forge_status"]), (400, "forge_refused", 401))
        self.assertIn("rejected the token", body["error"])
        self.assertFalse((self.state_dir / "config.json").exists())
        self.gs.fail_user = 500
        status, body = self.configure()
        self.assertEqual((status, body["code"], body["forge_status"]), (400, "forge_refused", 500))

    def test_blank_token_keeps_the_saved_one_and_null_removes_it(self):
        self.configure()
        n = len(self.gs.calls)
        status, body = self.call("POST", "/config", {"token": ""})
        self.assertEqual(status, 200)
        self.assertTrue(body["config"]["token"]["configured"])
        self.assertEqual(len(self.gs.calls), n)
        status, body = self.call("POST", "/config", {"token": None})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["token"], {"configured": False, "last4": "", "saved_at": ""})
        self.assertEqual(body["config"]["user"], "")
        self.assertEqual(json.loads((self.state_dir / "config.json").read_text())["token"], "")

    def test_changing_the_url_needs_the_token_again(self):
        self.configure()
        status, body = self.call("POST", "/config", {"forge_url": "http://127.0.0.1:19999"})
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("needs the token again", body["error"])
        self.assertEqual(json.loads((self.state_dir / "config.json").read_text())["forge_url"], self.gitea_url)

    def test_bad_bodies_are_bad_requests(self):
        for payload in ({"forge_url": "ftp://x", "token": GOOD}, {"forge_url": "nope", "token": GOOD},
                        {"forge_url": self.gitea_url, "token": 5}, {"token": GOOD}, [1, 2],
                        {"viewer_url": "ftp://viewer"}, {"viewer_url": "http://127.0.0.1:19999"}):
            status, body = self.call("POST", "/config", payload)
            self.assertEqual((status, body["code"]), (400, "bad_request"), payload)
        self.assertFalse((self.state_dir / "config.json").exists())

    def test_not_configured_is_409(self):
        for method, path in (("GET", f"/repos/{REPO}"), ("GET", f"/repos/{REPO}/branches"),
                             ("GET", f"/repos/{REPO}/cases?ref=master")):
            status, body = self.call(method, path)
            self.assertEqual((status, body["code"]), (409, "not_configured"), path)
        status, body = self.call("POST", "/builds", {"repo": REPO, "branch": "master",
                                                     "platforms": ["android"], "cases": [],
                                                     "request_id": "build_0123456789ab"})
        self.assertEqual((status, body["code"]), (409, "not_configured"))

    def test_config_written_atomically_leaves_no_temp_files(self):
        self.configure()
        self.call("POST", "/config", {"token": None})
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), ["config.json"])


class RepoApiTests(PluginCase):
    def setUp(self):
        super().setUp()
        self.start_plugin()
        self.configure()

    def test_check_repo_ok_with_recipes(self):
        status, body = self.call("GET", f"/repos/{REPO}")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"repo": REPO, "ok": True, "reason": "", "default_branch": "master",
                                "branch": "master", "recipes": {"android": True, "ios": True}})

    def test_missing_script_does_not_block_but_reports(self):
        self.gs.repos[REPO]["branches"]["feat/no-ios"] = [
            f for f in self.gs.repos[REPO]["branches"]["master"]
            if f != ".monkeytester/build-ios.sh"]
        status, body = self.call("GET", f"/repos/{REPO}?branch=feat/no-ios")
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["branch"], "feat/no-ios")
        self.assertEqual(body["recipes"], {"android": True, "ios": False})

    def test_unknown_branch_fails_the_check_not_http(self):
        status, body = self.call("GET", f"/repos/{REPO}?branch=nope-not-here")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("branch nope-not-here not found", body["reason"])
        self.assertEqual(body["recipes"], {"android": None, "ios": None})

    def test_unknown_repo_is_a_failed_check_not_an_http_error(self):
        status, body = self.call("GET", "/repos/blackjackfruit/Nope")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("not found", body["reason"])

    def test_branches_paged_default_first(self):
        status, body = self.call("GET", f"/repos/{REPO}/branches")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "master")
        self.assertEqual(body["branches"][0], "master")
        pages = [c[2]["page"] for c in self.gs.calls if c[1].endswith("/branches")]
        self.assertEqual(pages, [["1"], ["2"]])

    def test_branches_of_unknown_repo_is_404(self):
        status, body = self.call("GET", "/repos/blackjackfruit/Nope/branches")
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_cases_and_recipes_come_from_the_branch_tree(self):
        status, body = self.call("GET", f"/repos/{REPO}/cases?ref=feat/b01")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"repo": REPO, "ref": "feat/b01",
                                "cases": ["device-setup", "onboarding"],
                                "recipes": {"android": True, "ios": True}})
        tree_calls = [c for c in self.gs.calls if "/git/trees/" in c[1]]
        self.assertTrue(tree_calls[-1][1].endswith("/git/trees/feat%2Fb01"))
        self.assertEqual(tree_calls[-1][2]["recursive"], ["true"])

    def test_cases_follow_truncated_tree_pages(self):
        self.gs.tree_page_size = 3
        status, body = self.call("GET", f"/repos/{REPO}/cases?ref=master")
        self.assertEqual(body["cases"], ["device-setup", "onboarding"])
        pages = [c[2]["page"] for c in self.gs.calls if "/git/trees/" in c[1]]
        self.assertEqual(pages, [["1"], ["2"], ["3"], ["4"]])

    def test_unknown_branch_cases_is_404(self):
        status, body = self.call("GET", f"/repos/{REPO}/cases?ref=missing")
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_bad_names_are_bad_requests(self):
        for path in ("/repos/../x", "/repos/a/..", f"/repos/{REPO}?branch=a..b",
                     f"/repos/{REPO}/cases", f"/repos/{REPO}/cases?ref=bad%20ref"):
            status, body = self.call("GET", path)
            self.assertIn(status, (400, 404), path)
            self.assertIn(body["code"], ("bad_request", "not_found"), path)
        status, body = self.call("GET", f"/repos/{REPO}/cases")
        self.assertEqual((status, body["code"]), (400, "bad_request"))

    def test_forge_unreachable_is_502(self):
        cfg = json.loads((self.state_dir / "config.json").read_text())
        cfg["forge_url"] = "http://127.0.0.1:19999"
        build_runner.write_private_json(self.state_dir / "config.json", cfg)
        status, body = self.call("GET", f"/repos/{REPO}/branches")
        self.assertEqual((status, body["code"]), (502, "forge_unreachable"))
        status, body = self.call("GET", f"/repos/{REPO}")
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("unreachable", body["reason"])


class GithubApiTests(PluginCase):
    def setUp(self):
        super().setUp()
        self.start_plugin()
        status, body = self.configure_gh()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["forge_url"], f"{self.gh.url}/api/v3")
        self.assertNotIn(GH_TOKEN, json.dumps(body))

    def test_token_checked_for_grhes_url(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(body["forge"], {"kind": "github", "label": "GitHub",
                                         "url": f"{self.gh.url}/api/v3",
                                         "configured": True, "user": "ci-bot"})

    def test_check_repo_and_cases_on_github(self):
        status, body = self.call("GET", f"/repos/{REPO}?branch=feat/one-fails")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["branch"], "feat/one-fails")
        self.assertEqual(body["recipes"], {"android": True, "ios": True})
        status, body = self.call("GET", f"/repos/{REPO}/cases?ref=master")
        self.assertEqual(status, 200)
        self.assertEqual(body["cases"], ["device-setup", "onboarding"])
        self.assertEqual(body["recipes"], {"android": True, "ios": True})

    def test_truncated_tree_falls_back_to_contents(self):
        self.gh.add_repo("ci-bot/Trunc-App", default="master",
                         branches={"master": sorted(MASTER_FILES)}, truncated=True)
        status, body = self.call("GET", f"/repos/ci-bot/Trunc-App/cases?ref=master")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cases"], ["device-setup", "onboarding"])
        self.assertEqual(body["recipes"], {"android": True, "ios": True})
        self.assertTrue(any("/contents/visual-tests" in r[1] for r in self.gh.calls()))

    def test_branches_default_first_github(self):
        status, body = self.call("GET", f"/repos/{REPO}/branches")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "master")
        self.assertEqual(body["branches"][0], "master")
        self.assertEqual(sorted(body["branches"]), sorted(FIXTURE_BRANCHES))


class FixtureCase(PluginCase):
    """PluginCase plus fixture git repos served as file:// clones (no git server)."""

    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.fixtures = Path(root.name)
        for repo, branches in ((REPO, FIXTURE_BRANCHES), (GHOST, {"master": GHOST_FILES})):
            helpers.make_repo(self.fixtures, repo,
                              {branch: {path: content for path, content in files.items()}
                               for branch, files in branches.items()})
        # the fetch-failure fixture exists for the API but its git dir is removed below
        override = f"file://{self.fixtures}/{{repo}}"
        for mod in (gitea_src, github_src):
            before = mod.CLONE_URL_OVERRIDE
            mod.CLONE_URL_OVERRIDE = override
            self.addCleanup(setattr, mod, "CLONE_URL_OVERRIDE", before)
        self.gs.repos.clear()
        self.gh.repos.clear()
        self.gh.add_repo(REPO, default="master",
                         branches={b: sorted(f) for b, f in API_FILES.items()})
        self.gh.add_repo(GHOST, default="master", branches={"master": sorted(GHOST_FILES)})
        self.start_plugin()
        self.configure()

    def api_branches(self, repo=REPO):
        self.gs.add_repo(repo, {b: sorted(f) for b, f in API_FILES.items()} if repo == REPO
                         else {"master": sorted(GHOST_FILES)})

    def start_plugin_here(self):
        pass


class BuildTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.gs.add_repo(REPO, {b: sorted(f) for b, f in API_FILES.items()})
        self.gs.add_repo(GHOST, {"master": sorted(GHOST_FILES)})

    def test_build_success_uploads_both_apps(self):
        ref = self.start_build(branch="master")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "success"))
        self.assertEqual([(j["name"], j["conclusion"]) for j in body["jobs"]],
                         [("Android", "success"), ("iOS", "success"), ("Upload", "success")])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.record_args("--build-request"), "build_0123456789ab")
        self.assertEqual(self.record_args("--repo"), REPO)
        self.assertEqual(self.record_args("--branch"), "master")
        self.assertEqual(self.record_args("--cases"), "device-setup,onboarding")
        args = self.records()[0]["argv"]
        self.assertIn("--no-wait", args)
        self.assertEqual(self.record_args("--android-app"), str(self.out_dir(ref) / "app.apk"))
        self.assertEqual(self.record_args("--ios-app"), str(self.out_dir(ref) / "app.ipa"))
        self.assertTrue(self.records()[0]["tests_dir"])
        for app in ("app.apk", "app.ipa"):
            self.assertGreater((self.out_dir(ref) / app).stat().st_size, 0)
        self.assertFalse(any(a.startswith("--ios-build-error") for a in args))

    def test_two_builds_second_is_queued(self):
        ref_a = self.start_build(branch="feat/slow", platforms=["android"], request_id="build_aaa")
        self.wait_state(ref_a, wanted=("in_progress",))
        ref_b = self.start_build(branch="master", request_id="build_bbb")
        status, body_b = self.call("GET", f"/builds/{ref_b}")
        self.assertEqual(status, 200)
        self.assertEqual(body_b["state"], "queued")
        status, health = self.call("GET", "/health")
        self.assertEqual(health["queue"], {"running": ref_a, "waiting": 1})
        self.assertEqual(self.job(ref_b, "Android")["status"], "queued")
        # clean up the slow build
        status, body = self.call("POST", f"/builds/{ref_b}/cancel")
        self.assertEqual((status, body["conclusion"]), (200, "cancelled"))
        status, body = self.call("POST", f"/builds/{ref_a}/cancel")
        self.assertEqual((status, body["conclusion"]), (200, "cancelled"))

    def test_one_platform_fails_still_success_with_error_and_log(self):
        ref = self.start_build(branch="feat/one-fails")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "success"))
        conclusions = {j["name"]: j["conclusion"] for j in body["jobs"]}
        self.assertEqual(conclusions, {"Android": "success", "iOS": "failure", "Upload": "success"})
        self.assertEqual(len(self.records()), 1)
        args = self.records()[0]["argv"]
        self.assertEqual(self.record_args("--ios-build-error"),
                         "signing No matching files found.")
        self.assertIn("--ios-build-log", args)
        self.assertIn("--android-app", args)
        self.assertNotIn("--ios-app", args)
        log_text = self.records()[0]["files"]["--ios-build-log"]
        self.assertIn("error: signing", log_text)
        self.assertIn("error: No matching files found.", log_text)
        self.assertTrue((self.out_dir(ref) / "android-build.log").is_file())
        self.assertTrue((self.out_dir(ref) / "ios-build.log").is_file())

    def test_all_platforms_fail_upload_then_failure(self):
        ref = self.start_build(branch="feat/all-fail")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "failure"))
        self.assertIn("every platform failed to build", body["error"])
        self.assertEqual(self.job(ref, "Upload")["conclusion"], "success")
        self.assertEqual(len(self.records()), 1)
        args = self.records()[0]["argv"]
        self.assertEqual(self.record_args("--android-build-error"), "gradle exited 1")
        self.assertEqual(self.record_args("--ios-build-error"), "signing No matching files found.")
        self.assertIn("--android-build-log", args)
        self.assertIn("--ios-build-log", args)
        self.assertNotIn("--android-app", args)

    def test_upload_refused_is_failure(self):
        self.viewer.exit_code = 1
        ref = self.start_build(branch="master")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "failure"))
        self.assertIn("upload failed", body["error"])
        self.assertEqual(self.job(ref, "Upload")["conclusion"], "failure")
        self.assertEqual(self.job(ref, "Android")["conclusion"], "success")
        self.assertEqual(len(self.records()), 1)

    def test_fetch_failure_nothing_uploaded(self):
        import shutil
        shutil.rmtree(self.fixtures / GHOST)
        ref = self.start_build(repo=GHOST, branch="master", platforms=["android"],
                               request_id="build_ghost000000")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "failure"))
        self.assertIn("fetch failed", body["error"])
        self.assertEqual(self.records(), [])
        self.assertEqual([j["conclusion"] for j in body["jobs"]], ["failure", "failure"])
        log, _ = self.build_log(ref)
        self.assertIn(b"$ git fetch", log)

    def test_timeout_kills_the_process_group(self):
        with patch.dict(os.environ, {"BUILD_LOCAL_TIMEOUT_ANDROID": "1"}):
            ref = self.start_build(branch="feat/slow", platforms=["android"],
                                   request_id="build_timeout00000")
            body = self.wait_state(ref)
            self.assertEqual((body["state"], body["conclusion"]), ("completed", "failure"))
            self.assertEqual(self.job(ref, "Android")["conclusion"], "failure")
            self.assertEqual(self.record_args("--android-build-error"), "timed out after 1 s")
            self.assertTrue(self.pids_dead(ref))
            log, _ = self.build_log(ref)
            self.assertIn(b"killed: timed out after 1 s", log)

    def test_cancel_queued_and_running_kills_the_group(self):
        ref_a = self.start_build(branch="feat/slow", platforms=["android"], request_id="build_run0000000")
        helpers.wait_for(lambda: (self.out_dir(ref_a) / "pids.txt").is_file(), timeout=20)
        ref_b = self.start_build(branch="master", request_id="build_queued00000")
        status, body_b = self.call("GET", f"/builds/{ref_b}")
        self.assertEqual(body_b["state"], "queued")
        status, body = self.call("POST", f"/builds/{ref_b}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(body["conclusion"], "cancelled")
        self.assertEqual(set(j["conclusion"] for j in body["jobs"]), {"cancelled"})
        status, health = self.call("GET", "/health")
        self.assertEqual(health["queue"], {"running": ref_a, "waiting": 0})
        status, body = self.call("POST", f"/builds/{ref_a}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(body["conclusion"], "cancelled")
        body = self.body(ref_a)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "cancelled"))
        self.assertEqual(set(j["conclusion"] for j in body["jobs"]), {"cancelled"})
        self.assertTrue(self.pids_dead(ref_a))
        self.assertEqual(self.records(), [])  # cancelled builds upload nothing
        log, hdrs = self.build_log(ref_a)
        self.assertIn(b"] cancelled", log)
        self.assertEqual(hdrs["X-Log-Complete"], "1")

    def test_running_build_logs_without_complete_flag(self):
        ref = self.start_build(branch="feat/slow", platforms=["android"], request_id="build_running000000")
        helpers.wait_for(lambda: (self.out_dir(ref) / "pids.txt").is_file(), timeout=20)
        status, data, hdrs = self.call("GET", f"/builds/{ref}/log?offset=0", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"] fetch ", data)
        self.assertNotIn("X-Log-Complete", hdrs)
        size = int(hdrs["X-Log-Size"])
        status, data2, hdrs2 = self.call("GET", f"/builds/{ref}/log?offset={size}", raw=True)
        self.assertEqual((status, data2), (200, b""))
        self.assertEqual(int(hdrs2["X-Log-Size"]), size)
        self.call("POST", f"/builds/{ref}/cancel")

    def test_restart_marks_interrupted_builds_failed(self):
        ref = self.start_build(branch="feat/slow", platforms=["android"], request_id="build_interrupt0000")
        self.wait_state(ref, wanted=("in_progress",))
        build_plugin.stop_server(self.server)
        self.restart_plugin()
        body = self.body(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "failure"))
        self.assertIn("interrupted", body["error"])
        self.assertEqual(set(j["conclusion"] for j in body["jobs"]), {"failure"})
        log, hdrs = self.build_log(ref)
        self.assertIn(b"interrupted", log)
        self.assertEqual(hdrs["X-Log-Complete"], "1")

    def test_log_offset_and_headers(self):
        ref = self.start_build(branch="feat/one-fails")
        body = self.wait_state(ref)
        data, hdrs = self.build_log(ref)
        self.assertIn(b"] fetch ", data)
        self.assertIn(b"$ git fetch", data)
        self.assertIn(b"] iOS: bash .monkeytester/build-ios.sh", data)
        self.assertEqual(int(hdrs["X-Log-Size"]), len(data))
        self.assertEqual(hdrs["X-Log-Complete"], "1")
        half = len(data) // 2
        data2, hdrs2 = self.build_log(ref, offset=half)
        self.assertEqual(data[half:], data2)
        self.assertEqual(int(hdrs2["X-Log-Size"]), len(data))
        self.assertEqual(hdrs2["X-Log-Complete"], "1")

    def test_bad_log_offsets_and_unknown_refs(self):
        for path in ("/builds/br_0123456789abcdef/log", "/builds/nope/log",
                     "/builds/br_0123456789abcdef/log?offset=-5", "/builds/br_0123456789abcdef/log?offset=abc"):
            status, body = self.call("GET", path)
            self.assertIn((status, body["code"]), ((404, "not_found"), (400, "bad_request")), path)

    def test_token_never_in_logs_or_git_config(self):
        ref = self.start_build(branch="master")
        self.wait_state(ref)
        texts = self.all_logs(ref)
        blob = "\n".join(texts)
        self.assertNotIn(GOOD, blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("extraHeader", blob)
        git_cfg = (self.checkout("gitea") / ".git/config").read_text()
        self.assertIn("file://", git_cfg)
        self.assertNotIn(GOOD, git_cfg)

    def test_github_build_credentials_stay_out_of_git_and_logs(self):
        status, body = self.configure_gh()
        self.assertEqual(status, 200, body)
        ref = self.start_build(branch="master", platforms=["android"], request_id="build_gh0000000000")
        body = self.wait_state(ref)
        self.assertEqual((body["state"], body["conclusion"]), ("completed", "success"))
        self.assertEqual(len(self.records()), 1)
        self.assertIn("--android-app", self.records()[0]["argv"])
        basic = base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()
        texts = []
        out = self.out_dir(ref)
        for f in out.iterdir():
            if f.name.endswith(".log"):
                texts.append(f.read_text(errors="replace"))
        git_cfg = (self.checkout("github") / ".git/config").read_text()
        blob = "\n".join(texts) + git_cfg
        self.assertIn("file://", git_cfg)
        for secret in (GH_TOKEN, basic, "Authorization", "extraHeader"):
            self.assertNotIn(secret, blob)

    def test_missing_recipe_refused_at_start(self):
        for payload in ({"repo": REPO, "branch": "feat/no-ios", "platforms": ["android", "ios"],
                        "cases": [], "request_id": "build_noios0000000"},
                       {"repo": REPO, "branch": "feat/not-a-branch", "platforms": ["android"],
                        "cases": [], "request_id": "build_gone00000000"}):
            status, body = self.call("POST", "/builds", payload)
            if payload["branch"] == "feat/no-ios":
                self.assertEqual((status, body["code"]), (400, "bad_request"))
                self.assertIn(".monkeytester/build-ios.sh", body["error"])
                self.assertIn("GET /template", body["error"])
            else:
                self.assertEqual((status, body["code"]), (404, "not_found"))
        self.assertEqual(self.records(), [])

    def test_bad_build_bodies(self):
        for bad in ({"repo": "nope"}, {"repo": "../x", "branch": "master"},
                    {"repo": REPO, "branch": "a b", "platforms": ["android"], "cases": [],
                     "request_id": "build_x0000000000"},
                    {"repo": REPO, "branch": "master", "platforms": ["windows"], "cases": [],
                     "request_id": "build_x0000000000"},
                    {"repo": REPO, "branch": "master", "platforms": ["android"],
                     "cases": ["../x"], "request_id": "build_x0000000000"},
                    {"repo": REPO, "branch": "master", "platforms": ["android"],
                     "cases": [], "request_id": "bad id"},
                    []):
            status, body = self.call("POST", "/builds", bad)
            self.assertEqual((status, body["code"]), (400, "bad_request"), bad)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/builds", data=b"{not json",
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()

    def test_unknown_routes_and_refs_404(self):
        for method, path in (("GET", "/nope"), ("DELETE", "/config"),
                             ("POST", f"/builds/{REPO}"), ("GET", f"/repos/{REPO}/nope")):
            status, body = self.call(method, path)
            self.assertEqual((status, body["code"]), (404, "not_found"), (method, path))


class TemplateTests(PluginCase):
    def setUp(self):
        super().setUp()
        self.start_plugin()

    def test_template_served_as_plain_text(self):
        for platform, needles in (("android", ("gradlew assembleDebug", "VT_OUTPUT", "set -euo pipefail",
                                               "sdk.dir")),
                                  ("ios", ("xcodebuild archive", "exportArchive", "VT_OUTPUT",
                                           "set -euo pipefail", "UDID"))):
            status, raw, hdrs = self.call("GET", f"/template?platform={platform}", raw=True)
            self.assertEqual(status, 200)
            self.assertTrue(hdrs["Content-Type"].startswith("text/plain"))
            text = raw.decode()
            for needle in needles:
                self.assertIn(needle, text)
        for path in ("/template", "/template?platform=windows"):
            status, body = self.call("GET", path)
            self.assertEqual((status, body["code"]), (400, "bad_request"), path)


class MigrationTests(unittest.TestCase):
    """BUILD_LOCAL_IMPORT: one-time import of the viewer's old gitea_config.json."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.source = self.dir / "gitea_config.json"
        self.legacy = {"url": "http://gitea.example:3000/", "token": "legacy-token-5678wxyz",
                       "user": "legacy-bot", "saved_at": "2026-01-01T00:00:00+0000",
                       "repos": ["o/r"], "branches": {"o/r": "dev"}}
        self.source.write_text(json.dumps(self.legacy))
        os.chmod(self.source, 0o600)

    def boot(self, env):
        err = io.StringIO()
        with patch.dict(os.environ, env), redirect_stderr(err):
            plugin = build_plugin.Plugin(self.dir / "state", None, ["android"])
        return plugin, err.getvalue()

    def test_first_start_imports_url_token_user_only(self):
        before = self.source.read_bytes()
        plugin, log = self.boot({"BUILD_LOCAL_IMPORT": str(self.source)})
        cfg = json.loads((self.dir / "state" / "config.json").read_text())
        self.assertEqual(cfg["forge_url"], "http://gitea.example:3000")
        self.assertEqual(cfg["token"], "legacy-token-5678wxyz")
        self.assertEqual(cfg["user"], "legacy-bot")
        self.assertEqual(cfg["forge_kind"], "gitea")
        self.assertNotIn("repos", cfg)
        self.assertNotIn("branches", cfg)
        self.assertEqual(stat.S_IMODE((self.dir / "state" / "config.json").stat().st_mode), 0o600)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("imported", log)
        self.assertNotIn("legacy-token", log)
        self.assertTrue(plugin.health()["forge"]["configured"])

    def test_existing_config_never_overwritten_and_import_once(self):
        state = self.dir / "state"
        state.mkdir()
        build_runner.write_private_json(state / "config.json", {"forge_url": "http://mine", "token": ""})
        _, log = self.boot({"BUILD_LOCAL_IMPORT": str(self.source)})
        self.assertEqual(json.loads((state / "config.json").read_text())["forge_url"], "http://mine")
        self.assertEqual(log, "")

    def test_broken_or_unrelated_source_imports_nothing(self):
        self.source.unlink()
        self.boot({"BUILD_LOCAL_IMPORT": str(self.source)})
        self.assertFalse((self.dir / "state" / "config.json").exists())
        for content in ("{broken", "[1, 2]", '{"repos": ["o/r"]}', "{}"):
            self.source.write_text(content)
            self.boot({"BUILD_LOCAL_IMPORT": str(self.source)})
            self.assertFalse((self.dir / "state" / "config.json").exists(), content)

    def test_empty_env_imports_nothing(self):
        self.boot({"BUILD_LOCAL_IMPORT": ""})
        self.assertFalse((self.dir / "state" / "config.json").exists())


class RunnerUnitTests(unittest.TestCase):
    def test_build_error_message_collects_distinct_error_lines(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "x-build.log"
            log.write_text("step: starting\nios build: error: signing\n"
                           "error: No matching files found.\nerror: signing\n"
                           "plain line\n")
            self.assertEqual(build_runner.build_error_message(log),
                             "signing No matching files found.")
            log2 = Path(d) / "empty-build.log"
            log2.write_text("no errors here\n")
            self.assertEqual(build_runner.build_error_message(log2), "")
            long_log = Path(d) / "long-build.log"
            long_log.write_text("".join(f"error: line {i}\n" for i in range(10)) + "x" * 2000 + "\n")
            msg = build_runner.build_error_message(long_log)
            self.assertLessEqual(len(msg), 1500)
            self.assertTrue(msg.startswith("line 5 line 6"))

    def test_platform_timeout_env(self):
        with patch.dict(os.environ, {"BUILD_LOCAL_TIMEOUT_ANDROID": "7.5"}):
            self.assertEqual(build_runner.platform_timeout("android"), 7.5)
            self.assertEqual(build_runner.platform_timeout("ios"), 3600)
        with patch.dict(os.environ, {"BUILD_LOCAL_TIMEOUT_ANDROID": "junk"}):
            self.assertEqual(build_runner.platform_timeout("android"), 3600)
        with patch.dict(os.environ, {"BUILD_LOCAL_TIMEOUT_ANDROID": "0"}):
            self.assertEqual(build_runner.platform_timeout("android"), 3600)

    def test_write_private_json_mode_and_atomicity(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s" / "config.json"
            build_runner.write_private_json(path, {"token": "***"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sorted(p.name for p in Path(d).joinpath("s").iterdir()), ["config.json"])
            build_runner.write_private_json(path, {"token": "second"})
            self.assertEqual(json.loads(path.read_text()), {"token": "second"})

    def test_child_env_drops_plugin_secrets(self):
        with patch.dict(os.environ, {"PLUGIN_TOKEN": "s3cret-pair", "BUILD_LOCAL_IMPORT": "/x",
                                     "SOMETHING_ELSE": "keep"}):
            env = build_runner.child_env({"VT_PLATFORM": "android"})
            self.assertNotIn("PLUGIN_TOKEN", env)
            self.assertNotIn("BUILD_LOCAL_IMPORT", env)
            self.assertEqual(env["SOMETHING_ELSE"], "keep")
            self.assertEqual(env["VT_PLATFORM"], "android")



# ─── Security hardening (SECURITY.md) ───

class BindTests(unittest.TestCase):
    """Token mandatory on non-loopback binds; loopback may run without one."""

    def test_is_loopback(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(build_plugin.is_loopback(host), host)
        for host in ("0.0.0.0", "::", "", "192.168.125.71", "10.0.0.1", "example.com", "fe80::1"):
            self.assertFalse(build_plugin.is_loopback(host), host)

    def test_bind_error(self):
        self.assertIsNone(build_plugin.bind_error("127.0.0.1", ""))
        self.assertIsNone(build_plugin.bind_error("0.0.0.0", "tok"))
        for host in ("0.0.0.0", "::", "192.168.125.71"):
            msg = build_plugin.bind_error(host, "")
            self.assertIn("PLUGIN_TOKEN", msg)
            self.assertIn("EnvironmentVariables", msg)
        self.assertIsNotNone(build_plugin.bind_error("0.0.0.0", "   "))

    def test_make_server_refuses_lan_without_token(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            build_plugin.make_server("0.0.0.0", 0, d, "", ["android"])

    def _run_main(self, host, token=None, wait_for_listen=False):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in ("PLUGIN_TOKEN", "PLUGIN_HOST")}
        env.update(PLUGIN_HOST=host, PLUGIN_PORT="0", BUILD_PLUGIN_STATE_DIR=tmp.name,
                   BUILD_LOCAL_PLATFORMS="android", BUILD_LOCAL_IMPORT="")
        if token is not None:
            env["PLUGIN_TOKEN"] = token
        proc = subprocess.Popen([sys.executable, str(ROOT / "build_plugin.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if not wait_for_listen:
            _, err = proc.communicate(timeout=30)
            return proc.returncode, err
        line = proc.stderr.readline()
        proc.terminate()
        proc.communicate(timeout=30)
        return None, line

    def test_main_exits_nonzero_on_lan_bind_without_token(self):
        for host in ("0.0.0.0", "::", "192.0.2.10"):
            rc, err = self._run_main(host)
            self.assertEqual(rc, 2, (host, err))
            self.assertIn("PLUGIN_TOKEN", err)
            self.assertIn("refusing to listen", err)

    def test_main_starts_on_loopback_without_token(self):
        _, line = self._run_main("127.0.0.1", wait_for_listen=True)
        self.assertIn("127.0.0.1", line)
        self.assertIn("without a pairing token", line)


class RequestHardeningTests(PluginCase):
    pairing_token = PAIR

    def setUp(self):
        super().setUp()
        self.start_plugin()

    def raw_request(self, method, path, body=b"", headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self.addCleanup(conn.close)
        conn.putrequest(method, path)
        h = {"X-Plugin-Token": PAIR}
        h.update(headers or {})
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_oversize_body_is_413(self):
        status, body = self.raw_request("POST", "/config",
                                        headers={"Content-Length": str(build_plugin.MAX_BODY + 1)})
        self.assertEqual((status, body["code"]), (413, "too_large"))

    def test_bad_json_is_400(self):
        for raw in (b"{not json", b"\xff\xfe", b"[" * 100000):
            status, body = self.raw_request("POST", "/config", raw,
                                            {"Content-Length": str(len(raw)), "Content-Type": "application/json"})
            self.assertEqual((status, body["code"]), (400, "bad_request"), raw[:10])

    def test_bad_content_length_is_400_and_chunked_411(self):
        status, _ = self.raw_request("POST", "/config", headers={"Content-Length": "-5"})
        self.assertEqual(status, 400)
        status, _ = self.raw_request("POST", "/config", headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)
        status, _ = self.raw_request("POST", "/config", b"0\r\n\r\n", {"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)

    def test_internal_error_returns_no_traceback(self):
        def boom(ref):
            raise RuntimeError("very secret internal detail")

        with patch.object(self.server.plugin, "build_status", boom), redirect_stderr(io.StringIO()):
            status, raw, _ = self.call("GET", "/builds/br_0123456789abcdef", raw=True)
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(raw), {"error": "internal error: RuntimeError", "code": "internal"})
        self.assertNotIn(b"Traceback", raw)
        self.assertNotIn(b"secret", raw)

    def test_traversal_in_paths_is_refused(self):
        for path in ("/builds/..%2F..%2Fconfig.json", "/builds/%2e%2e/log", "/builds/..%2Fbuilds.json/log",
                     "/builds/br_0123456789abcdef%2F..%2F..%2Fconfig.json/log"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 404, path)
        for path in (f"/repos/..%2F..%2Fetc/x/branches", "/repos/../x/branches", "/repos/a%00b/x/branches"):
            status, body = self.call("GET", path)
            self.assertIn(status, (400, 404), path)

    def test_branch_names_that_look_like_options_are_refused(self):
        self.assertEqual(self.configure()[0], 200)
        for branch in ("-oops", "--upload-pack=touch /tmp/x", "a..b", "a b", "a\x00b", "/abs", "x/"):
            status, body = self.call("POST", "/builds", {"repo": REPO, "branch": branch,
                                                         "platforms": ["android"], "request_id": "r1"})
            self.assertEqual((status, body.get("code")), (400, "bad_request"), branch)
            status, _ = self.call("GET", f"/repos/{REPO}/cases?ref={urllib.parse.quote(branch)}")
            self.assertEqual(status, 400, branch)


class PathAndGitUnitTests(unittest.TestCase):
    def test_contained_rejects_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"
            root.mkdir()
            self.assertEqual(build_runner.contained(root, "a", "b"), Path(os.path.realpath(root)) / "a" / "b")
            for parts in (("..",), (".",), ("a", ".."), ("/etc",), ("a/b",), ("a\x00",), ("",), ("a\\b",)):
                with self.assertRaises(ValueError, msg=parts):
                    build_runner.contained(root, *parts)
            (root / "link").symlink_to(d)
            with self.assertRaises(ValueError):
                build_runner.contained(root, "link")

    def test_out_dir_needs_a_build_ref(self):
        with tempfile.TemporaryDirectory() as d:
            r = build_runner.Runner(d, dict, ["android"])
            for ref in ("..", "../x", "br_0123", "br_0123456789abcdeg"):
                with self.assertRaises(ValueError):
                    r.out_dir(ref)
            self.assertIsNone(r.read_log("../builds.json"))

    def test_git_env_restricts_transports_and_drops_secrets(self):
        with patch.dict(os.environ, {"PLUGIN_TOKEN": "pair-xyz", "GIT_CONFIG_KEY_9": "core.sshCommand"}):
            env = build_runner.git_env([("http.x.extraHeader", "Authorization: token t")])
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "http:https:ssh")
        self.assertEqual(env["GIT_PROTOCOL_FROM_USER"], "0")
        self.assertNotIn("PLUGIN_TOKEN", env)
        self.assertNotIn("GIT_CONFIG_KEY_9", env)
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertIn("protocol.allow", keys)
        self.assertNotIn("protocol.file.allow", keys)
        self.assertNotIn("protocol.ext.allow", keys)

    def test_git_refuses_ext_transport_with_git_env(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "pwned"
            env = build_runner.git_env([])
            proc = subprocess.run(["git", "ls-remote", "--", f"ext::sh -c touch% {marker}"], cwd=d, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(marker.exists())
            proc = subprocess.run(["git", "ls-remote", "--", f"file://{d}"], cwd=d, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(b"not allowed", proc.stderr)

    def test_urlopen_is_http_only(self):
        for url in ("http://h:1", "https://h"):
            self.assertEqual(sources.require_http(url), url)
        for url in ("file:///etc/passwd", "ftp://h", "", None, "javascript:x"):
            with self.assertRaises(sources.SourceError):
                sources.require_http(url)
        with self.assertRaises(sources.SourceError):
            gitea_src.api("file:///etc", "t", "GET", "/user")
        with self.assertRaises(build_plugin.PluginError):
            build_plugin.check_viewer("file:///etc")

    def test_no_shell_true_anywhere(self):
        for f in ("build_plugin.py", "runner.py", "sources/gitea.py", "sources/github.py"):
            self.assertNotIn("shell=True", (ROOT / f).read_text(), f)


class BuildHardeningTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.gs.add_repo(REPO, {b: sorted(f) for b, f in API_FILES.items()})

    def test_build_script_never_sees_the_pairing_token(self):
        with patch.dict(os.environ, {"PLUGIN_TOKEN": "pair-token-in-env-5678", "BUILD_LOCAL_IMPORT": "/x/y"}):
            ref = self.start_build(branch="feat/env-dump", platforms=["android"])
            body = self.wait_state(ref)
        self.assertEqual(body["conclusion"], "success", body)
        dumped = (self.out_dir(ref) / "app.apk").read_text()
        self.assertIn("VT_PLATFORM=android", dumped)
        self.assertNotIn("PLUGIN_TOKEN", dumped)
        self.assertNotIn("pair-token-in-env-5678", dumped)
        self.assertNotIn("BUILD_LOCAL_IMPORT", dumped)
        self.assertNotIn(GOOD, dumped)

    def test_ext_clone_url_is_refused(self):
        marker = self.dir / "pwned"
        gitea_src.CLONE_URL_OVERRIDE = f"ext::sh -c touch% {marker}"
        ref = self.start_build(branch="master", platforms=["android"])
        body = self.wait_state(ref)
        self.assertEqual(body["conclusion"], "failure")
        self.assertIn("transport", body["error"])
        self.assertFalse(marker.exists())

    def test_option_like_error_text_reaches_submit_as_one_value(self):
        ref = self.start_build(branch="feat/dash-error", platforms=["android"])
        self.wait_state(ref)
        args = self.records()[0]["argv"]
        self.assertIn("--android-build-error=--android-app=/etc/passwd", args)
        self.assertNotIn("--android-app", args)
        self.assertNotIn("--android-app=/etc/passwd", args)


if __name__ == "__main__":
    unittest.main()
