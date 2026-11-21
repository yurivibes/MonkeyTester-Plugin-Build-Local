"""In-process fake of the read-only GitHub REST API endpoints build-local uses.

Copied from the archived build-github tests (fake_github.py) without the
Actions endpoints. Response shapes follow docs.github.com (REST API version
2022-11-28); only the fields the plugin reads, plus a few neighbours, are filled
in. The fake serves under /api/v3 like GitHub Enterprise Server, so the plugin's
GHES URL handling is exercised too. It records git's smart-HTTP discovery
request (/<owner>/<repo>.git/info/refs) and answers it 404. No network access:
it binds 127.0.0.1 on a free port.
"""
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/v3"
RECIPES = [".monkeytester/build-android.sh", ".monkeytester/build-ios.sh"]


def sha(text):
    return hashlib.sha1(text.encode()).hexdigest()


class FakeGitHub:
    def __init__(self):
        self.tokens = {"ghp_good_token_1234": "ci-bot", "github_pat_readonly_5678": "reader"}
        self.repos = {}
        self.requests = []      # (method, path, query dict, lower-cased headers dict)
        self.git_requests = []  # (path, lower-cased headers dict)
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    # ─── setup helpers ───

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def add_repo(self, repo, default="master", branches=None, truncated=False, users=None):
        """branches: {name: [file paths]}."""
        branches = branches or {default: [*RECIPES, "README.md"]}
        self.repos[repo] = {"default_branch": default, "branches": branches,
                            "truncated": truncated, "users": users or {"ci-bot", "reader"}}

    def calls(self, method=None, contains=""):
        return [r for r in self.requests if (method is None or r[0] == method) and contains in r[1]]

    # ─── tree helpers ───

    def _resolve(self, repo, ref):
        """Branch name or commit sha -> (branch name, files) or None."""
        branches = self.repos[repo]["branches"]
        if ref in branches:
            return ref, branches[ref]
        for name, files in branches.items():
            if sha(name) == ref or sha("tree:" + name) == ref:
                return name, files
        return None

    @staticmethod
    def _tree_entries(files):
        dirs, out = set(), []
        for path in files:
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            out.append({"path": path, "mode": "100644", "type": "blob", "sha": sha(path), "size": 10})
        out += [{"path": d, "mode": "040000", "type": "tree", "sha": sha(d)} for d in dirs]
        return sorted(out, key=lambda e: e["path"])

    # ─── HTTP ───

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send(self, status, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                headers = {k.lower(): v for k, v in self.headers.items()}
                if parsed.path.endswith("/info/refs"):
                    fake.git_requests.append((parsed.path, headers))
                    return self._send(404, {"message": "Not Found"})
                fake.requests.append(("GET", parsed.path, query, headers))
                if not parsed.path.startswith(PREFIX + "/"):
                    return self._send(404, {"message": "Not Found"})
                # Split before unquoting: an encoded %2F stays inside one segment.
                parts = [urllib.parse.unquote(p) for p in parsed.path[len(PREFIX):].split("/") if p]
                auth = self.headers.get("Authorization") or ""
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                user = fake.tokens.get(token)
                if not user:
                    return self._send(401, {"message": "Bad credentials",
                                            "documentation_url": "https://docs.github.com/rest"})
                return self._route(parts, query, user)

            def _route(self, parts, query, user):
                if parts == ["user"]:
                    return self._send(200, {"login": user, "id": 1, "type": "User"})
                if len(parts) < 3 or parts[0] != "repos":
                    return self._send(404, {"message": "Not Found"})
                repo = f"{parts[1]}/{parts[2]}"
                info = fake.repos.get(repo)
                if info is None or user not in info["users"]:
                    return self._send(404, {"message": "Not Found"})
                rest = parts[3:]
                if not rest:
                    return self._send(200, {
                        "full_name": repo, "default_branch": info["default_branch"], "private": True,
                        "permissions": {"admin": False, "maintain": False, "push": False,
                                        "triage": False, "pull": True}})
                if rest == ["branches"]:
                    names = list(info["branches"])
                    per_page = min(int(query.get("per_page", 30)), 100)
                    page = int(query.get("page", 1))
                    chunk = names[(page - 1) * per_page: page * per_page]
                    return self._send(200, [{"name": n, "commit": {"sha": sha(n), "url": ""}, "protected": False}
                                            for n in chunk])
                if len(rest) == 2 and rest[0] == "branches":
                    name = rest[1]
                    if name not in info["branches"]:
                        return self._send(404, {"message": "Branch not found"})
                    return self._send(200, {"name": name, "commit": {
                        "sha": sha(name), "url": "", "commit": {"tree": {"sha": sha("tree:" + name), "url": ""},
                                                                "message": "x"}}, "protected": False})
                if len(rest) == 3 and rest[:2] == ["git", "trees"]:
                    found = fake._resolve(repo, rest[2])
                    if not found:
                        return self._send(404, {"message": "Not Found"})
                    entries = fake._tree_entries(found[1])
                    if info["truncated"]:
                        return self._send(200, {"sha": rest[2], "tree": entries[:2], "truncated": True})
                    return self._send(200, {"sha": rest[2], "tree": entries, "truncated": False})
                if rest and rest[0] == "contents":
                    path = "/".join(rest[1:])
                    found = fake._resolve(repo, query.get("ref") or info["default_branch"])
                    if not found:
                        return self._send(404, {"message": "No commit found for the ref"})
                    files = found[1]
                    if path in files:
                        return self._send(200, {"type": "file", "name": path.split("/")[-1], "path": path,
                                                "sha": sha(path), "size": 10, "encoding": "base64",
                                                "content": ""})
                    children = {}
                    for f in files:
                        if f.startswith(path + "/"):
                            child = f[len(path) + 1:].split("/")
                            children[child[0]] = "file" if len(child) == 1 else "dir"
                    if not children:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, [{"type": t, "name": n, "path": f"{path}/{n}", "sha": sha(n)}
                                            for n, t in sorted(children.items())])
                return self._send(404, {"message": "Not Found"})

        return Handler
