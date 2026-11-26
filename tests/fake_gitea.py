"""An in-process fake Gitea (read-only API subset build-local uses).

Covers the
read-only calls: GET /user, repo, branches (paged), one branch, contents, git
trees (paged). Response shapes follow Gitea's API (docs.gitea.com/api, swagger
v1.27). It also records git's smart-HTTP discovery request
(/<owner>/<repo>.git/info/refs) so tests can see the credentials git sent, and
answers it 404. No network access beyond 127.0.0.1.
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD = "good-token-0000abcd"
READ_ONLY = "read-only-token-9999"
REPO = "blackjackfruit/TemperatureSensor-App"
RECIPES = [".monkeytester/build-android.sh", ".monkeytester/build-ios.sh"]
DEFAULT_FILES = ["README.md", *RECIPES, "mt-tests/onboarding/INSTRUCTION.txt",
                 "mt-tests/device-setup/INSTRUCTION.txt", "mt-tests/notes.txt"]


class State:
    def __init__(self):
        self.calls = []
        self.git_requests = []          # (path, headers dict)
        self.repos = {}
        self.tree_page_size = 1000
        self.fail_user = None           # HTTP status for GET /user
        branches = [f"feat/b{i:02d}" for i in range(60)] + ["master", "develop"]
        self.add_repo(REPO, {b: list(DEFAULT_FILES) for b in branches})

    def add_repo(self, repo, branches, default="master"):
        """branches: {name: [file paths]}."""
        self.repos[repo] = {"default": default, "branches": branches}


def tree_entries(files):
    dirs, out = set(), []
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
        out.append({"path": path, "mode": "100644", "type": "blob", "sha": "b" * 40, "size": 10})
    out += [{"path": d, "mode": "040000", "type": "tree", "sha": "t" * 40} for d in dirs]
    return sorted(out, key=lambda e: e["path"])


class FakeGitea(BaseHTTPRequestHandler):
    state: State = None

    def log_message(self, *args):
        pass

    def reply(self, status, payload=None):
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        auth = self.headers.get("Authorization") or ""
        return auth[len("token "):] if auth.startswith("token ") else ""

    def do_GET(self):
        st = self.state
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path.endswith("/info/refs"):
            st.git_requests.append((path, {k.lower(): v for k, v in self.headers.items()}))
            return self.reply(404, {"message": "not a git server"})
        st.calls.append(("GET", path, query))
        if self.token() not in (GOOD, READ_ONLY):
            return self.reply(401, {"message": "token is required", "url": "http://gitea/api/swagger"})
        if path == "/api/v1/user":
            if st.fail_user:
                return self.reply(st.fail_user, {"message": "boom"})
            return self.reply(200, {"id": 1, "login": "monkeytester-bot", "username": "monkeytester-bot"})
        if not path.startswith("/api/v1/repos/"):
            return self.reply(404, {"message": "no route"})
        parts = [urllib.parse.unquote(p) for p in path[len("/api/v1/repos/"):].split("/")]
        repo = "/".join(parts[:2])
        info = st.repos.get(repo)
        if info is None:
            return self.reply(404, {"message": "not found"})
        rest = parts[2:]
        branches = info["branches"]
        if not rest:
            # A read-only token: push false, pull true.
            return self.reply(200, {"full_name": repo, "default_branch": info["default"],
                                    "permissions": {"admin": False, "push": False, "pull": True}})
        if rest == ["branches"]:
            page, limit = int(query["page"][0]), int(query["limit"][0])
            names = list(branches)
            chunk = names[(page - 1) * limit:page * limit]
            return self.reply(200, [{"name": n, "commit": {"id": "abc"}, "protected": False} for n in chunk])
        if rest[0] == "branches":
            name = "/".join(rest[1:])
            if name in branches:
                return self.reply(200, {"name": name})
            return self.reply(404, {"message": "branch not found"})
        if rest[0] == "contents":
            ref = query.get("ref", [info["default"]])[0]
            if ref not in branches:
                return self.reply(404, {"message": "object does not exist"})
            fpath = "/".join(rest[1:])
            if fpath in branches[ref]:
                return self.reply(200, {"type": "file", "name": fpath.split("/")[-1], "path": fpath})
            if any(f.startswith(fpath + "/") for f in branches[ref]):
                return self.reply(200, [{"type": "dir", "name": "x"}])
            return self.reply(404, {"message": "GetContentsOrList", "errors": []})
        if rest[:2] == ["git", "trees"]:
            ref = "/".join(rest[2:])
            if ref not in branches:
                # Gitea 1.27: GetTreeBySHA -> 400 "sha not found [<ref>]"
                return self.reply(400, {"message": f"sha not found [{ref}]", "url": "http://gitea/api/swagger"})
            tree = tree_entries(branches[ref])
            page = int(query.get("page", ["1"])[0])
            per = min(int(query.get("per_page", ["1000"])[0]), st.tree_page_size)
            start, end = per * (page - 1), per * page
            return self.reply(200, {"sha": "c5d8", "url": "http://gitea/tree", "tree": tree[start:end],
                                    "truncated": end < len(tree), "page": page, "total_count": len(tree)})
        return self.reply(404, {"message": "not found"})


def start():
    """(server, url, state)."""
    state = State()
    handler = type("BoundFakeGitea", (FakeGitea,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", state
