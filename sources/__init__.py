"""Read-only code host access for build-local (BUILD_PLUGIN_API.md).

One module per code host kind (`gitea`, `github`). Every module offers the
same functions, all READ-ONLY (the token only needs to read the repo):

  KIND, LABEL, DEFAULT_URL
  normalize_url(url)                  -> the stored forge_url (raises SourceError)
  check_token(forge_url, token)       -> the token's user login
  check_repo(cfg, repo, branch)       -> contract GET /repos/<o>/<r> body
  list_branches(cfg, repo)            -> contract GET /repos/<o>/<r>/branches body
  list_cases(cfg, repo, ref)          -> contract GET /repos/<o>/<r>/cases body
  clone_url(cfg, repo)                -> the git URL to fetch from
  git_auth_config(cfg, repo)          -> [(git config key, value)] carrying the token,
                                         passed per command via GIT_CONFIG_* env,
                                         never written to .git/config or the URL
  CLONE_URL_OVERRIDE                  -> tests only: "file:///.../{owner}/{name}.git"

`cfg` is the plugin's config dict ({"forge_url", "token", ...}).
Standard library only.
"""
from __future__ import annotations

import re

PLATFORMS = ("android", "ios")
RECIPES = {"android": ".monkeytester/build-android.sh", "ios": ".monkeytester/build-ios.sh"}
CASES_DIR = "mt-tests"


class SourceError(Exception):
    """A request the plugin answers with {"error", "code"} and HTTP `status`.

    forge_status is the code host's HTTP status when it answered with an error.
    """

    def __init__(self, message, code="forge_refused", status=502, forge_status=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.forge_status = forge_status


CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def cases_from_tree(entries):
    """Case names: mt-tests/<case>/INSTRUCTION.txt blobs in a git tree listing."""
    cases = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        parts = (entry.get("path") or "").split("/")
        if len(parts) == 3 and parts[0] == CASES_DIR and parts[2] == "INSTRUCTION.txt" \
                and entry.get("type") == "blob" and CASE_RE.match(parts[1]):
            cases.add(parts[1])
    return sorted(cases)


def recipes_from_tree(entries):
    """{platform: bool}: which build scripts a git tree listing holds."""
    blobs = {e.get("path") for e in entries or [] if isinstance(e, dict) and e.get("type") == "blob"}
    return {p: RECIPES[p] in blobs for p in PLATFORMS}


def require_http(url):
    """`url` when it is http(s)://, else SourceError: urlopen must never open
    file:// or a custom scheme, even from a hand-edited config.json."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise SourceError("only http:// and https:// URLs are allowed", "bad_request", 400)
    return url


def override_url(template, repo):
    """Tests only: CLONE_URL_OVERRIDE formatted for owner/name."""
    owner, name = repo.split("/", 1)
    return template.format(owner=owner, name=name, repo=repo)


def get(kind):
    """The source module for a forge_kind, or None."""
    if kind == "gitea":
        from sources import gitea
        return gitea
    if kind == "github":
        from sources import github
        return github
    return None


KINDS = ("gitea", "github")
