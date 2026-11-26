"""GitHub (github.com and GitHub Enterprise Server), read-only (build-local
source): token
check, repo + default branch, branch exists, branches with paging, cases and
build scripts from the git tree (contents-API fallback when the tree is
truncated). No Actions, no write access needed.

forge_url is the REST API base: https://api.github.com for github.com, or
https://<host>/api/v3 for GitHub Enterprise Server. normalize_url() turns
https://github.com into the former and a bare GHES host into the latter.

git fetch: https://github.com/<owner>/<repo>.git (GHES: the web host of the API
base) with `Authorization: Basic base64(x-access-token:<token>)`, scoped to that
host (http.<host>/.extraHeader) and passed per command.
Standard library only.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from sources import (CASE_RE, CASES_DIR, PLATFORMS, RECIPES, SourceError, cases_from_tree, override_url,
                     recipes_from_tree, require_http)

KIND = "github"
LABEL = "GitHub"
DEFAULT_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

PER_PAGE = 100
MAX_BRANCH_PAGES = 50       # 5000 branches
MAX_FALLBACK_CASES = 300    # contents-API case checks when the git tree is truncated

# Tests only: a file:// URL template ({owner}, {name}, {repo}) used instead of
# the GitHub clone URL, so fixture repos can be fetched without a git server.
CLONE_URL_OVERRIDE = None


# ─── URL and HTTP ───

def normalize_url(url):
    """The REST API base for a URL the user typed. Blank means github.com."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_URL
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SourceError("GitHub URL must look like https://api.github.com or https://<ghes-host>/api/v3",
                          "bad_request", 400)
    host = parsed.netloc.lower()
    if host in ("github.com", "www.github.com", "api.github.com") and parsed.path in ("", "/"):
        return DEFAULT_URL
    if not parsed.path:
        # A bare GitHub Enterprise Server host: its REST API lives under /api/v3.
        return f"{parsed.scheme}://{parsed.netloc}/api/v3"
    return url


def web_base(forge_url):
    """The web (git) host for an API base: https://github.com, or the GHES host."""
    parsed = urllib.parse.urlparse(forge_url)
    if parsed.netloc.lower() == "api.github.com":
        return "https://github.com"
    return f"{parsed.scheme}://{parsed.netloc}"


def api(base, token, method, path, params=None, timeout=20):
    """Call the GitHub REST API. Returns (status, parsed JSON or None).

    Raises SourceError(forge_unreachable) when GitHub can't be reached. Never puts
    the token in a message.
    """
    full = base.rstrip("/") + path
    if params:
        full += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "monkeytester-build-local",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(require_http(full), headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - require_http: http(s) only
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"GitHub unreachable at {base}: {getattr(e, 'reason', e)}",
                          "forge_unreachable", 502) from None
    try:
        payload = json.loads(raw.decode() or "null")
    except (ValueError, UnicodeDecodeError):
        payload = None
    return status, payload


def _message(status, payload):
    if isinstance(payload, dict) and payload.get("message"):
        return f"HTTP {status}: {payload['message']}"
    return f"HTTP {status}"


def _refused(what, status, payload):
    return SourceError(f"{what}: GitHub answered {_message(status, payload)}", "forge_refused",
                       400 if status and 400 <= status < 500 else 502, status)


def _q(part):
    """One path segment (branch names may contain '/', which must be encoded)."""
    return urllib.parse.quote(part, safe="")


def _call(cfg, method, path, **kw):
    return api(cfg["forge_url"], cfg["token"], method, path, **kw)


# ─── Token ───

def check_token(forge_url, token):
    """The login the token belongs to (GET /user). Raises SourceError when refused."""
    status, payload = api(forge_url, token, "GET", "/user")
    if status in (401, 403):
        raise SourceError(f"GitHub rejected the token ({_message(status, payload)})", "forge_refused",
                          400, status)
    if status != 200 or not isinstance(payload, dict) or not payload.get("login"):
        raise SourceError(f"Checking the token failed: GitHub answered {_message(status, payload)}",
                          "forge_refused", 400, status)
    return payload["login"]


# ─── git fetch ───

def clone_url(cfg, repo):
    if CLONE_URL_OVERRIDE:
        return override_url(CLONE_URL_OVERRIDE, repo)
    return f"{web_base(cfg['forge_url'])}/{repo}.git"


def git_auth_config(cfg, repo):
    """The fetch credentials as per-command git config, scoped to the git host."""
    basic = base64.b64encode(f"x-access-token:{cfg['token']}".encode()).decode()
    return [(f"http.{web_base(cfg['forge_url'])}/.extraHeader", f"Authorization: Basic {basic}")]


# ─── Repos, branches, cases ───

def _repo_info(cfg, repo):
    """GET /repos/{o}/{r}. Raises not_found / forge_refused."""
    status, payload = _call(cfg, "GET", f"/repos/{repo}")
    if status == 404:
        raise SourceError(f"{repo} not found, or the token can't see it", "not_found", 404, 404)
    if status != 200 or not isinstance(payload, dict):
        raise _refused(f"Reading {repo} failed", status, payload)
    return payload


def has_file(cfg, repo, path, ref):
    """True/False: does ref have the file? None when GitHub couldn't answer."""
    try:
        status, payload = _call(cfg, "GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
    except SourceError:
        return None
    if status == 200:
        return isinstance(payload, dict) and payload.get("type") == "file"
    if status == 404:
        return False
    return None


def check_repo(cfg, repo, branch=""):
    """The contract's GET /repos/<owner>/<repo> body: ok means the token can
    read the repo and the branch exists. A missing build script does not make
    ok false."""
    out = {"repo": repo, "ok": False, "reason": "", "default_branch": "", "branch": branch,
           "recipes": {p: None for p in PLATFORMS}}
    try:
        info = _repo_info(cfg, repo)
    except SourceError as e:
        return {**out, "reason": e.message}
    out["default_branch"] = info.get("default_branch") or ""
    branch = branch or out["default_branch"]
    out["branch"] = branch
    if branch != out["default_branch"]:
        try:
            status, payload = _call(cfg, "GET", f"/repos/{repo}/branches/{_q(branch)}")
        except SourceError as e:
            return {**out, "reason": e.message}
        if status == 404:
            return {**out, "reason": f"branch {branch} not found"}
        if status != 200:
            return {**out, "reason": f"checking branch {branch} failed: {_message(status, payload)}"}
    out["recipes"] = {p: has_file(cfg, repo, RECIPES[p], branch) for p in PLATFORMS}
    return {**out, "ok": True}


def list_branches(cfg, repo):
    """{repo, default, branches}: every branch (paged), the default first."""
    default = _repo_info(cfg, repo).get("default_branch") or ""
    names = []
    for page in range(1, MAX_BRANCH_PAGES + 1):
        status, payload = _call(cfg, "GET", f"/repos/{repo}/branches",
                                params={"per_page": PER_PAGE, "page": page})
        if status != 200 or not isinstance(payload, list):
            raise _refused(f"Listing branches of {repo} failed", status, payload)
        names += [b["name"] for b in payload if isinstance(b, dict) and b.get("name")]
        if len(payload) < PER_PAGE:
            break
    names = list(dict.fromkeys(names))
    names.sort(key=lambda n: (n != default, n.lower()))
    return {"repo": repo, "default": default, "branches": names}


def _branch_commit(cfg, repo, ref):
    """(commit sha, tree sha) of a branch. Unknown branch -> not_found."""
    status, payload = _call(cfg, "GET", f"/repos/{repo}/branches/{_q(ref)}")
    if status == 404:
        raise SourceError(f"Branch {ref} not found in {repo}", "not_found", 404, 404)
    if status != 200 or not isinstance(payload, dict):
        raise _refused(f"Reading branch {ref} failed", status, payload)
    commit = payload.get("commit") or {}
    tree = ((commit.get("commit") or {}).get("tree") or {}).get("sha") or ""
    return commit.get("sha") or "", tree


def _cases_from_contents(cfg, repo, sha):
    """Fallback when the recursive tree is truncated: list mt-tests/ and check
    each folder for INSTRUCTION.txt through the contents API."""
    status, payload = _call(cfg, "GET", f"/repos/{repo}/contents/{CASES_DIR}", params={"ref": sha})
    if status == 404:
        return []
    if status != 200 or not isinstance(payload, list):
        raise _refused(f"Listing {CASES_DIR}/ failed", status, payload)
    dirs = sorted(e["name"] for e in payload
                  if isinstance(e, dict) and e.get("type") == "dir" and CASE_RE.match(e.get("name") or ""))
    cases = []
    for name in dirs[:MAX_FALLBACK_CASES]:
        status, entry = _call(cfg, "GET", f"/repos/{repo}/contents/{CASES_DIR}/{_q(name)}/INSTRUCTION.txt",
                              params={"ref": sha})
        if status == 200 and isinstance(entry, dict) and entry.get("type") == "file":
            cases.append(name)
    return cases


def list_cases(cfg, repo, ref):
    """{repo, ref, cases, recipes} for one branch. Unknown branch -> not_found."""
    sha, tree_sha = _branch_commit(cfg, repo, ref)
    status, payload = _call(cfg, "GET", f"/repos/{repo}/git/trees/{_q(tree_sha or sha)}",
                            params={"recursive": "1"})
    if status != 200 or not isinstance(payload, dict):
        raise _refused(f"Reading the tree of {ref} failed", status, payload)
    entries = payload.get("tree") or []
    if payload.get("truncated"):
        cases = _cases_from_contents(cfg, repo, sha or ref)
        recipes = {p: has_file(cfg, repo, RECIPES[p], sha or ref) for p in PLATFORMS}
    else:
        cases = cases_from_tree(entries)
        recipes = recipes_from_tree(entries)
    return {"repo": repo, "ref": ref, "cases": cases, "recipes": recipes}
