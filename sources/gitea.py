"""Gitea, read-only (build-local source): token check, repo + default branch, branch exists,
branches with paging, cases and build scripts from the git tree. No Actions,
no write access needed.

Gitea API: <url>/api/v1, header `Authorization: token <token>`.
git fetch: `<url>/<owner>/<repo>.git` with the same header, scoped to the
Gitea URL (http.<url>/.extraHeader) and passed per command.
Standard library only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from sources import PLATFORMS, RECIPES, SourceError, cases_from_tree, override_url, recipes_from_tree, require_http

KIND = "gitea"
LABEL = "Gitea"
DEFAULT_URL = ""  # the user enters the Gitea URL

BRANCH_PAGE = 50     # Gitea's default MAX_RESPONSE_ITEMS
BRANCH_MAX_PAGES = 100
TREE_PAGE = 1000     # Gitea caps per_page at [api] DEFAULT_GIT_TREES_PER_PAGE (1000)
TREE_MAX_PAGES = 50

# Tests only: a file:// URL template ({owner}, {name}, {repo}) used instead of
# the Gitea clone URL, so fixture repos can be fetched without a git server.
CLONE_URL_OVERRIDE = None


# ─── HTTP ───

def api(url, token, method, path, params=None, timeout=20):
    """Call the Gitea API. Returns (status, parsed JSON or None).

    Raises SourceError(forge_unreachable) when Gitea can't be reached. Never
    puts the token in a message.
    """
    full = f"{url}/api/v1{path}"
    if params:
        full += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "Authorization": f"token {token}"}
    req = urllib.request.Request(require_http(full), headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - require_http: http(s) only
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"Gitea unreachable at {url}: {getattr(e, 'reason', e)}",
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


def refused(what, status, payload):
    """SourceError for a Gitea error answer: 4xx -> 400, 5xx -> 502."""
    return SourceError(f"{what}: {_message(status, payload)}", "forge_refused",
                       400 if status and 400 <= status < 500 else 502, status)


def _get(cfg, path, params=None):
    return api(cfg["forge_url"], cfg["token"], "GET", path, params=params)


def _repo_path(repo):
    owner, name = repo.split("/", 1)
    return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _ref(ref):
    return urllib.parse.quote(ref, safe="")


# ─── Config ───

def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SourceError("Gitea URL must look like http://host:3000", "bad_request", 400)
    return url


def check_token(forge_url, token):
    """The token's user login. Raises SourceError when Gitea refuses it."""
    status, payload = api(forge_url, token, "GET", "/user")
    if status in (401, 403):
        raise SourceError(f"Gitea rejected the token ({_message(status, payload)})", "forge_refused", 400, status)
    if status != 200 or not isinstance(payload, dict):
        raise refused("Checking the token failed", status, payload)
    return payload.get("login") or payload.get("username") or ""


# ─── git fetch ───

def clone_url(cfg, repo):
    if CLONE_URL_OVERRIDE:
        return override_url(CLONE_URL_OVERRIDE, repo)
    return f"{cfg['forge_url']}/{repo}.git"


def git_auth_config(cfg, repo):
    """The fetch credentials as per-command git config, scoped to the Gitea URL."""
    return [(f"http.{cfg['forge_url']}/.extraHeader", f"Authorization: token {cfg['token']}")]


# ─── Repos, branches, cases ───

def has_file(cfg, repo, path, ref=""):
    """True/False: does ref (default branch when empty) have the file? None
    when Gitea couldn't answer."""
    params = {"ref": ref} if ref else None
    try:
        status, payload = _get(cfg, f"{_repo_path(repo)}/contents/{path}", params)
    except SourceError:
        return None
    if status == 200:
        return not isinstance(payload, list)  # a list is a directory
    if status == 404:
        return False
    return None


def check_repo(cfg, repo, branch=""):
    """Contract GET /repos/<o>/<r>: {repo, ok, reason, default_branch, branch,
    recipes}. A missing build script does not make ok false."""
    out = {"repo": repo, "ok": False, "reason": "", "default_branch": "", "branch": branch,
           "recipes": {p: None for p in PLATFORMS}}
    try:
        status, payload = _get(cfg, _repo_path(repo))
    except SourceError as e:
        return {**out, "reason": str(e)}
    if status == 404:
        return {**out, "reason": "not found, or the token's user can't see it"}
    if status != 200 or not isinstance(payload, dict):
        return {**out, "reason": _message(status, payload)}
    out["default_branch"] = payload.get("default_branch") or ""
    if branch and branch != out["default_branch"]:
        try:
            b_status, b_payload = _get(cfg, f"{_repo_path(repo)}/branches/{_ref(branch)}")
        except SourceError as e:
            return {**out, "reason": str(e)}
        if b_status == 404:
            return {**out, "reason": f"branch {branch} not found"}
        if b_status != 200:
            return {**out, "reason": _message(b_status, b_payload)}
    out["branch"] = branch or out["default_branch"]
    out["recipes"] = {p: has_file(cfg, repo, RECIPES[p], out["branch"]) for p in PLATFORMS}
    return {**out, "ok": True}


def _default_branch(cfg, repo):
    status, info = _get(cfg, _repo_path(repo))
    if status == 404:
        raise SourceError(f"{repo} not found, or the token's user can't see it", "not_found", 404, 404)
    if status != 200 or not isinstance(info, dict):
        raise refused(f"Reading {repo} failed", status, info)
    return info.get("default_branch") or ""


def list_branches(cfg, repo):
    """{repo, default, branches}: every branch (paged), default first."""
    default = _default_branch(cfg, repo)
    names = []
    for page in range(1, BRANCH_MAX_PAGES + 1):
        status, payload = _get(cfg, f"{_repo_path(repo)}/branches", {"page": page, "limit": BRANCH_PAGE})
        if status != 200 or not isinstance(payload, list):
            raise refused("Listing branches failed", status, payload)
        names += [b["name"] for b in payload if isinstance(b, dict) and b.get("name") and b["name"] not in names]
        if len(payload) < BRANCH_PAGE:
            break
    names.sort(key=lambda n: (n != default, n.lower()))
    return {"repo": repo, "default": default, "branches": names}


def list_cases(cfg, repo, ref):
    """{repo, ref, cases, recipes} for that branch. Unknown branch -> not_found."""
    entries = []
    for page in range(1, TREE_MAX_PAGES + 1):
        status, payload = _get(cfg, f"{_repo_path(repo)}/git/trees/{_ref(ref)}",
                               {"recursive": "true", "per_page": TREE_PAGE, "page": page})
        # Gitea answers an unknown ref with 400 "sha not found [<ref>]", an
        # unknown repo with 404.
        if status == 404 or (status == 400 and "not found" in _message(status, payload).lower()):
            raise SourceError(f"Branch {ref} not found in {repo}", "not_found", 404, status)
        if status != 200 or not isinstance(payload, dict):
            raise refused(f"Reading {ref} failed", status, payload)
        entries += payload.get("tree") or []
        if not payload.get("truncated"):
            break
    return {"repo": repo, "ref": ref, "cases": cases_from_tree(entries), "recipes": recipes_from_tree(entries)}
