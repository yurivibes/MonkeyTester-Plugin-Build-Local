"""Test helpers: a fake MonkeyTester viewer, fixture git repos, HTTP calls.

The fake viewer answers GET /health like the viewer, serves GET /ci/submit.py
(by default a small stub that records its argv and exits with `exit_code`;
`submit_source` replaces it, e.g. with the viewer's real submit.py) and records
POST /api/run-test (multipart field names only). No network beyond 127.0.0.1.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STUB_SUBMIT = r'''#!/usr/bin/env python3
"""Stub of the viewer's submit.py for build-local's tests."""
import json, os, sys
args = sys.argv[1:]
info = {"argv": args, "cwd": os.getcwd(), "files": {},
        "tests_dir": os.path.isdir(args[args.index("--tests-dir") + 1]) if "--tests-dir" in args else None}
for flag in ("--android-app", "--ios-app", "--android-build-log", "--ios-build-log"):
    if flag in args:
        with open(args[args.index(flag) + 1], "rb") as f:
            info["files"][flag] = f.read().decode(errors="replace")
with open(RECORD, "a") as f:
    f.write(json.dumps(info) + "\n")
print("--- run_fake started", flush=True)
sys.exit(EXIT_CODE)
'''


class FakeViewer:
    def __init__(self, record_path):
        self.record_path = Path(record_path)
        self.exit_code = 0
        self.submit_status = 200
        self.submit_source = None
        self.service = "monkeytester"
        self.uploads = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def records(self):
        if not self.record_path.exists():
            return []
        return [json.loads(line) for line in self.record_path.read_text().splitlines() if line.strip()]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, json.dumps({"ok": True, "service": fake.service, "api_version": 1}))
                if self.path == "/ci/submit.py":
                    if fake.submit_status != 200:
                        return self._send(fake.submit_status, "{}")
                    src = fake.submit_source
                    if src is None:
                        src = (STUB_SUBMIT.replace("RECORD", repr(str(fake.record_path)))
                               .replace("EXIT_CODE", str(fake.exit_code)))
                    return self._send(200, src, "text/x-python")
                return self._send(404, json.dumps({"error": "no route"}))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.path == "/api/run-test":
                    names = sorted(set(part.split(b'"')[1].decode() for part in raw.split(b'name="')[1:]
                                       if b'"' in part))
                    fake.uploads.append({"fields": names, "body": raw})
                    return self._send(200, json.dumps({"run_id": f"run_{len(fake.uploads)}"}))
                return self._send(404, json.dumps({"error": "no route"}))

        return Handler


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "-c", "init.defaultBranch=master", "-c", "commit.gpgsign=false", *args],
                   cwd=str(cwd), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(root, repo, branches):
    """A git repo at <root>/<owner>/<name> with one commit per branch.

    branches: {branch: {path: content}}. Returns {branch: [paths]}.
    """
    path = Path(root) / repo
    path.mkdir(parents=True)
    git(path, "init", "-q")
    files_by_branch = {}
    for branch, files in branches.items():
        git(path, "checkout", "-q", "--orphan", branch)
        subprocess.run(["git", "rm", "-rfq", "--ignore-unmatch", "."], cwd=str(path),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for rel, content in files.items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"{branch}")
        files_by_branch[branch] = sorted(files)
    return files_by_branch


def request(base, method, path, body=None, headers=None, raw=False):
    """(status, parsed JSON or bytes, headers)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload, hdrs = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        with e:
            status, payload, hdrs = e.code, e.read(), e.headers
    if raw:
        return status, payload, hdrs
    return status, json.loads(payload.decode() or "null"), hdrs


def wait_for(predicate, timeout=30, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still "exists"; check ps for it.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")
