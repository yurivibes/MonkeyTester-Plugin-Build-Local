"""build-local: MonkeyTester build plugin that builds on its own host
(BUILD_PLUGIN_API.md, contract_version 1).

The viewer's Build & Test page asks this plugin for a repo's branches and test
cases and to build a branch. The plugin reads the repo from Gitea or GitHub with
a read-only token, fetches the branch with git, runs the app repo's
`.monkeytester/build-<platform>.sh` here (Android and iOS on a Mac, Android
only on Linux) and uploads the apps to the viewer with the viewer's submit.py.

This file: HTTP routing, the pairing token, JSON errors, the config file
(code host + token + viewer URL, mode 0600), platform detection. The build
queue is runner.py; code host access is sources/.

Entry point: python3 build_plugin.py

Environment:
  PLUGIN_HOST              listen address (default 0.0.0.0: the viewer on the
                           Linux host reaches the Mac over the LAN)
  PLUGIN_PORT              listen port (default 8094)
  PLUGIN_TOKEN             pairing token, required on every request except
                           GET /health. MANDATORY when PLUGIN_HOST is not a
                           loopback address (127.0.0.1, ::1, localhost): the
                           plugin refuses to start without it (SECURITY.md)
  BUILD_PLUGIN_STATE_DIR   config.json, builds.json, checkouts/, out/
                           (default ~/.config/monkeytester-build-local/)
  BUILD_LOCAL_PLATFORMS    override the detected platforms, e.g. "android"
  BUILD_LOCAL_TIMEOUT_ANDROID / BUILD_LOCAL_TIMEOUT_IOS
                           seconds per platform build (default 3600)
  BUILD_LOCAL_IMPORT       first start only: import url + token + user from
                           this gitea_config.json (the viewer's old Gitea file)

Standard library only, Python 3.9+, Linux and macOS.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner as runner_mod  # noqa: E402
import sources  # noqa: E402
from sources import PLATFORMS, RECIPES, SourceError  # noqa: E402

PLUGIN_NAME = "build-local"
PLUGIN_VERSION = "0.1.0"
DEFAULT_PORT = 8094
CONTRACT = "monkeytester-build-plugin"
CONTRACT_VERSION = 1
CAPABILITIES = ["logs", "cancel"]
DEFAULT_KIND = "gitea"
MAX_BODY = 1024 * 1024
REQUEST_TIMEOUT = 60              # seconds a client may take per socket read
LOOPBACK_NAMES = ("localhost", "localhost.", "ip6-localhost")
VIEWER_SERVICE = "monkeytester"   # the viewer's GET /health "service"
TEMPLATES = Path(__file__).resolve().parent / "templates"

# owner/repo; neither part may be only dots.
REPO_PART_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9_.-]{1,100}$")
# Git refs: no spaces, control chars, "..", "//", leading "/", trailing "/" or ".".
BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!-)(?!.*//)(?!.*@\{)[^\s~^:?*\[\\\x00-\x1f\x7f]{1,250}(?<![/.])$")
CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

STATUS_FOR_CODE = {
    "bad_request": 400, "unauthorized": 401, "not_found": 404, "not_configured": 409,
    "forge_refused": 400, "forge_unreachable": 502, "internal": 500, "too_large": 413,
}


class PluginError(Exception):
    def __init__(self, message, code="bad_request", status=None, forge_status=None):
        super().__init__(message)
        self.code = code
        self.status = status or STATUS_FOR_CODE.get(code, 400)
        self.forge_status = forge_status


def log(message):
    print(f"{PLUGIN_NAME}: {message}", file=sys.stderr, flush=True)


def is_loopback(host):
    """True when `host` (a PLUGIN_HOST value) only listens on this machine."""
    host = (host or "").strip().strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_error(host, token):
    """The reason this bind is refused, or None. A non-loopback bind (LAN,
    0.0.0.0, ::) runs builds for anyone who can reach the port, so it needs a
    pairing token; loopback may run without one."""
    if (token or "").strip() or is_loopback(host):
        return None
    return (f"refusing to listen on {host or 'all interfaces'} without a pairing token: "
            "anyone on the network could run builds (code execution) on this host. "
            "Set PLUGIN_TOKEN to a long random secret (e.g. `python3 -c "
            "'import secrets; print(secrets.token_urlsafe(32))'`) in the service "
            "environment (launchd plist EnvironmentVariables / systemd Environment=) "
            "and enter the same token for the plugin in the viewer's Settings, or set "
            "PLUGIN_HOST=127.0.0.1 to listen on this machine only. See SECURITY.md.")


def state_dir():
    d = os.environ.get("BUILD_PLUGIN_STATE_DIR") or f"~/.config/monkeytester-{PLUGIN_NAME}"
    return Path(d).expanduser()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ─── Platform detection ───

def detect_platforms(env=None, system=None, home=None, run=subprocess.run):
    """What this host can build: ios when `xcodebuild -version` works on macOS,
    android when an Android SDK exists. BUILD_LOCAL_PLATFORMS overrides."""
    env = os.environ if env is None else env
    override = (env.get("BUILD_LOCAL_PLATFORMS") or "").strip()
    if override:
        wanted = {p.strip().lower() for p in override.split(",") if p.strip()}
        unknown = sorted(wanted - set(PLATFORMS))
        if unknown:
            log(f"BUILD_LOCAL_PLATFORMS: ignoring unknown platform(s) {', '.join(unknown)}")
        return [p for p in PLATFORMS if p in wanted]
    system = sys.platform if system is None else system
    home = Path.home() if home is None else Path(home)
    found = []
    sdks = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"),
            str(home / "Library" / "Android" / "sdk"), str(home / "Android" / "Sdk")]
    if any(s and Path(s).expanduser().is_dir() for s in sdks):
        found.append("android")
    if system == "darwin":
        try:
            if run(["xcodebuild", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=60).returncode == 0:
                found.append("ios")
        except (OSError, subprocess.SubprocessError):
            pass
    return [p for p in PLATFORMS if p in found]


# ─── Config ───

class ConfigStore:
    """config.json: {forge_kind, forge_url, token, user, saved_at, viewer_url}."""

    FIELDS = ("forge_kind", "forge_url", "token", "user", "saved_at", "viewer_url")

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self):
        cfg = {f: "" for f in self.FIELDS}
        cfg["forge_kind"] = DEFAULT_KIND
        stored = runner_mod.read_json(self.path, {})
        cfg.update({k: v for k, v in stored.items() if k in cfg and isinstance(v, str)})
        if cfg["forge_kind"] not in sources.KINDS:
            cfg["forge_kind"] = DEFAULT_KIND
        return cfg

    def save(self, cfg):
        with self.lock:
            runner_mod.write_private_json(self.path, {f: cfg.get(f) or "" for f in self.FIELDS})

    def bootstrap(self, env=None):
        """First start with no config.json: import the viewer's old
        gitea_config.json from $BUILD_LOCAL_IMPORT (url + token + user) as
        forge_kind gitea. The source file is never changed."""
        env = os.environ if env is None else env
        source = (env.get("BUILD_LOCAL_IMPORT") or "").strip()
        with self.lock:
            if self.path.exists() or not source:
                return False
            path = Path(source).expanduser()
            try:
                with open(path) as f:
                    stored = json.load(f)
            except (OSError, ValueError) as e:
                log(f"not importing {path}: {e.__class__.__name__}")
                return False
            if not isinstance(stored, dict):
                return False
            from sources import gitea
            cfg = self.load()
            cfg["forge_kind"] = "gitea"
            try:
                if isinstance(stored.get("url"), str) and stored["url"].strip():
                    cfg["forge_url"] = gitea.normalize_url(stored["url"])
            except SourceError:
                pass
            token = stored.get("token")
            if isinstance(token, str) and token.strip():
                cfg["token"] = token.strip()
                cfg["user"] = stored["user"] if isinstance(stored.get("user"), str) else ""
                cfg["saved_at"] = now_iso()
            if not cfg["forge_url"] and not cfg["token"]:
                return False
            self.save(cfg)
            what = " and ".join(k for k, ok in (("URL", cfg["forge_url"]), ("token", cfg["token"])) if ok)
            log(f"imported the Gitea {what} from {path}")
            return True


def public_config(cfg):
    token = cfg.get("token") or ""
    return {
        "forge_kind": cfg.get("forge_kind") or DEFAULT_KIND,
        "forge_url": cfg.get("forge_url") or "",
        "token": {"configured": bool(token), "last4": token[-4:] if len(token) >= 8 else "",
                  "saved_at": (cfg.get("saved_at") or "") if token else ""},
        "user": cfg.get("user") or "",
        "viewer_url": cfg.get("viewer_url") or "",
    }


def normalize_viewer_url(url):
    url = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PluginError("viewer_url must look like http://host:8089")
    return url


def check_viewer(url, timeout=10):
    """Raise PluginError unless url answers GET /health as a MonkeyTester viewer."""
    try:
        sources.require_http(url)
    except SourceError as e:
        raise PluginError(f"viewer_url: {e}") from None
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:  # nosec B310 - http(s) checked above
            payload = json.loads(resp.read().decode() or "null")
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as e:
        raise PluginError(f"viewer_url {url} did not answer GET /health: "
                          f"{getattr(e, 'reason', e.__class__.__name__)}") from None
    if not isinstance(payload, dict) or payload.get("service") != VIEWER_SERVICE:
        raise PluginError(f"viewer_url {url} is not a MonkeyTester viewer (GET /health)")


# ─── Request handling ───

def source_call(fn, *args):
    """Run a source call, turning its SourceError into a PluginError."""
    try:
        return fn(*args)
    except SourceError as e:
        raise PluginError(str(e), e.code, e.status, e.forge_status) from None


def check_repo_name(owner, repo):
    if not (REPO_PART_RE.match(owner) and REPO_PART_RE.match(repo)):
        raise PluginError(f"{owner}/{repo!s} is not owner/repo")
    return f"{owner}/{repo}"


def check_branch(branch, what="branch"):
    if not isinstance(branch, str) or not BRANCH_RE.match(branch):
        raise PluginError(f"invalid {what} name")
    return branch


class Plugin:
    def __init__(self, directory=None, pairing_token=None, platforms=None):
        directory = Path(directory) if directory else state_dir()
        self.dir = directory
        self.config = ConfigStore(directory / "config.json")
        self.pairing_token = pairing_token or ""
        self.config.bootstrap()
        self.platforms = detect_platforms() if platforms is None else [p for p in PLATFORMS if p in platforms]
        self.runner = runner_mod.Runner(directory, self.config.load, self.platforms, log)

    def source(self, cfg):
        return sources.get(cfg.get("forge_kind")) or sources.get(DEFAULT_KIND)

    # GET /health
    def health(self, full=True):
        """The contract body. Without the pairing token (full=False, only when a
        token is set) the code host URL/user, viewer URL and queue are left
        out: an unauthenticated caller learns the contract and platforms only.
        No secret is ever part of it."""
        cfg = self.config.load()
        src = self.source(cfg)
        error = None
        if not self.platforms:
            error = "this host can build no platform (no Android SDK, no Xcode found)"
        body = {
            "ok": True,
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
            "forge": {"kind": src.KIND, "label": src.LABEL, "url": cfg.get("forge_url") or "",
                      "configured": bool(cfg.get("token")), "user": cfg.get("user") or ""},
            "platforms": list(self.platforms),
            "recipes": dict(RECIPES),
            "viewer_url": cfg.get("viewer_url") or "",
            "queue": self.runner.queue_info(),
            "capabilities": list(CAPABILITIES),
            "error": error,
        }
        if not full:
            body["forge"] = {"kind": src.KIND, "label": src.LABEL}
            body["auth_required"] = True
            for key in ("viewer_url", "queue"):
                body.pop(key)
        return body

    def configured(self, need_viewer=False):
        cfg = self.config.load()
        if not cfg.get("token"):
            raise PluginError(f"No {self.source(cfg).LABEL} token saved. Configure the plugin first.",
                              "not_configured")
        if need_viewer and not cfg.get("viewer_url"):
            raise PluginError("No viewer_url saved. Configure the plugin first.", "not_configured")
        return cfg

    # POST /config
    def save_config(self, data):
        if not isinstance(data, dict):
            raise PluginError("expected a JSON object")
        for field in ("forge_kind", "forge_url", "viewer_url"):
            if data.get(field) is not None and not isinstance(data[field], str):
                raise PluginError(f"{field} must be a string")
        with self.config.lock:
            cfg = self.config.load()
            kind = (data.get("forge_kind") or "").strip() or cfg["forge_kind"]
            src = sources.get(kind)
            if src is None:
                raise PluginError("forge_kind must be one of: " + ", ".join(sources.KINDS))
            if (data.get("forge_url") or "").strip():
                url = source_call(src.normalize_url, data["forge_url"])
            elif kind != cfg["forge_kind"]:
                url = src.DEFAULT_URL
            else:
                url = cfg.get("forge_url") or src.DEFAULT_URL
            forge_changed = kind != cfg["forge_kind"] or url != (cfg.get("forge_url") or "")

            remove = "token" in data and data["token"] is None
            new_token = data.get("token")
            if new_token is not None and not isinstance(new_token, str):
                raise PluginError("token must be a string or null")
            new_token = (new_token or "").strip()
            if forge_changed and not new_token and not remove and cfg.get("token"):
                raise PluginError("Changing the code host or its URL needs the token again")

            viewer = cfg.get("viewer_url") or ""
            if "viewer_url" in data and data["viewer_url"] is None:
                viewer = ""
            elif (data.get("viewer_url") or "").strip():
                viewer = normalize_viewer_url(data["viewer_url"])

            # Check everything before storing anything.
            user = cfg.get("user") or ""
            if new_token:
                if not url:
                    raise PluginError(f"Enter the {src.LABEL} URL")
                try:
                    user = src.check_token(url, new_token) or ""
                except SourceError as e:
                    status = 400 if e.code == "forge_refused" else e.status
                    raise PluginError(str(e), e.code, status, e.forge_status) from None
            if viewer and viewer != (cfg.get("viewer_url") or ""):
                check_viewer(viewer)

            cfg.update(forge_kind=kind, forge_url=url, viewer_url=viewer)
            if remove:
                cfg.update(token="", user="", saved_at="")
            elif new_token:
                cfg.update(token=new_token, user=user, saved_at=now_iso())
            self.config.save(cfg)
            return {"ok": True, "config": public_config(cfg)}

    # POST /builds
    def start_build(self, data):
        if not isinstance(data, dict):
            raise PluginError("expected a JSON object")
        repo = data.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise PluginError("repo must be owner/repo")
        repo = check_repo_name(*repo.split("/"))
        branch = check_branch(data.get("branch"))
        platforms = data.get("platforms")
        if not isinstance(platforms, list) or not platforms or any(p not in PLATFORMS for p in platforms):
            raise PluginError("platforms must be a non-empty list of: " + ", ".join(PLATFORMS))
        platforms = [p for p in PLATFORMS if p in platforms]
        cases = data.get("cases", [])
        if cases is None:
            cases = []
        if not isinstance(cases, list) or any(not isinstance(c, str) or not CASE_RE.match(c) for c in cases):
            raise PluginError("cases must be a list of case names")
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
            raise PluginError("request_id is missing or invalid")
        cant = [p for p in platforms if p not in self.platforms]
        if cant:
            builds = ", ".join(self.platforms) or "nothing"
            raise PluginError(f"this host can't build {', '.join(cant)} (it builds: {builds})")
        cfg = self.configured(need_viewer=True)
        src = self.source(cfg)
        info = source_call(src.list_cases, cfg, repo, branch)
        missing = [p for p in platforms if (info.get("recipes") or {}).get(p) is False]
        if missing:
            raise PluginError(f"{branch} has no {', '.join(RECIPES[p] for p in missing)} "
                              "(GET /template?platform= has an example)")
        ref = self.runner.submit(repo, branch, platforms, sorted(set(cases)), request_id, src.KIND)
        log(f"build {ref} queued: {repo} {branch} {','.join(platforms)} ({request_id})")
        return {"build_ref": ref, "url": ""}

    def build_status(self, ref):
        body = self.runner.body(ref)
        if body is None:
            raise PluginError("unknown build_ref", "not_found")
        return body

    def cancel_build(self, ref):
        body = self.runner.cancel(ref)
        if body is None:
            raise PluginError("unknown build_ref", "not_found")
        return body


def template_text(platform):
    if platform not in PLATFORMS:
        raise PluginError("platform must be one of: " + ", ".join(PLATFORMS))
    return (TEMPLATES / f"build-{platform}.sh").read_text()


class Handler(BaseHTTPRequestHandler):
    server_version = f"{PLUGIN_NAME}/{PLUGIN_VERSION}"
    plugin: "Plugin | None" = None  # set by make_server
    timeout = REQUEST_TIMEOUT         # socket timeout: a stalled client can't pin a thread

    def log_message(self, format, *args):  # noqa: A002 - base class name
        if os.environ.get("BUILD_PLUGIN_ACCESS_LOG"):
            super().log_message(format, *args)

    # ─ responses ─
    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, status, data, headers=None):
        if isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, err):
        payload = {"error": str(err), "code": err.code}
        if err.forge_status is not None:
            payload["forge_status"] = err.forge_status
        self.send_json(err.status, payload)

    # ─ plumbing ─
    def authorized(self):
        expected = self.plugin.pairing_token
        if not expected:
            return True
        given = self.headers.get("X-Plugin-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if not given and auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return hmac.compare_digest(given.encode(), expected.encode())

    def read_body(self):
        """The JSON request body ({} when empty). 400 for a bad Content-Length
        or JSON, 413 above MAX_BODY (checked before reading), 411 for a
        chunked body (no length to check)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            raise PluginError("send the body with a Content-Length", status=411)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise PluginError("bad Content-Length") from None
        if length < 0:
            raise PluginError("bad Content-Length")
        if length > MAX_BODY:
            self.close_connection = True   # the unread body must not be parsed as a request
            raise PluginError(f"request body too large (max {MAX_BODY} bytes)", "too_large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise PluginError("request body shorter than Content-Length")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise PluginError("request body is not JSON") from None

    def dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path == "/health":
                return self.send_json(200, self.plugin.health(full=self.authorized()))
            if not self.authorized():
                raise PluginError("missing or wrong pairing token", "unauthorized")
            parts = [unquote(p) for p in path.strip("/").split("/")]
            return self.route(method, parts, query)
        except PluginError as e:
            return self.send_error_json(e)
        except Exception as e:  # noqa: BLE001 - last resort, keep serving
            traceback.print_exc()   # server log only; the client gets the type name, never a traceback
            return self.send_error_json(PluginError(f"internal error: {type(e).__name__}", "internal"))

    def route(self, method, parts, query):
        p = self.plugin
        if parts == ["config"]:
            if method == "GET":
                return self.send_json(200, public_config(p.config.load()))
            if method == "POST":
                return self.send_json(200, p.save_config(self.read_body()))
        elif parts == ["template"] and method == "GET":
            return self.send_text(200, template_text(query.get("platform") or ""))
        elif parts == ["builds"] and method == "POST":
            return self.send_json(201, p.start_build(self.read_body()))
        elif len(parts) == 2 and parts[0] == "builds" and method == "GET":
            return self.send_json(200, p.build_status(parts[1]))
        elif len(parts) == 3 and parts[0] == "builds" and parts[2] == "log" and method == "GET":
            try:
                offset = int(query.get("offset") or 0)
            except ValueError:
                raise PluginError("offset must be a number of bytes") from None
            if offset < 0:
                raise PluginError("offset must be a number of bytes")
            result = p.runner.read_log(parts[1], offset)
            if result is None:
                raise PluginError("unknown build_ref", "not_found")
            data, size, complete = result
            headers = {"X-Log-Size": str(size)}
            if complete:
                headers["X-Log-Complete"] = "1"
            return self.send_text(200, data, headers)
        elif len(parts) == 3 and parts[0] == "builds" and parts[2] == "cancel" and method == "POST":
            return self.send_json(200, p.cancel_build(parts[1]))
        elif len(parts) in (3, 4) and parts[0] == "repos" and method == "GET":
            repo = check_repo_name(parts[1], parts[2])
            sub = parts[3] if len(parts) == 4 else None
            if sub is None:
                branch = query.get("branch") or ""
                if branch:
                    check_branch(branch)
                cfg = p.configured()
                return self.send_json(200, source_call(p.source(cfg).check_repo, cfg, repo, branch))
            if sub == "branches":
                cfg = p.configured()
                return self.send_json(200, source_call(p.source(cfg).list_branches, cfg, repo))
            if sub == "cases":
                ref = check_branch(query.get("ref") or "", "ref")
                cfg = p.configured()
                return self.send_json(200, source_call(p.source(cfg).list_cases, cfg, repo, ref))
        raise PluginError(f"no route {method} /{'/'.join(parts)}", "not_found")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


def make_server(host="127.0.0.1", port=0, directory=None, pairing_token=None, platforms=None):
    """A server with its build runner started. Stop with stop_server().
    Raises ValueError for a non-loopback host without a pairing token."""
    error = bind_error(host, pairing_token)
    if error:
        raise ValueError(error)
    plugin = Plugin(directory, pairing_token, platforms)
    handler = type("BoundHandler", (Handler,), {"plugin": plugin})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.plugin = plugin
    plugin.runner.start()
    return server


def stop_server(server):
    server.shutdown()
    server.server_close()
    server.plugin.runner.stop()


def main():
    host = (os.environ.get("PLUGIN_HOST") or "").strip() or "0.0.0.0"  # nosec B104 - LAN by design; needs PLUGIN_TOKEN (bind_error)
    token = (os.environ.get("PLUGIN_TOKEN") or "").strip()
    error = bind_error(host, token)
    if error:
        log(error)
        sys.exit(2)
    try:
        port = int(os.environ.get("PLUGIN_PORT") or DEFAULT_PORT)
    except ValueError:
        log("PLUGIN_PORT must be a port number")
        sys.exit(2)
    if not token:
        log(f"no PLUGIN_TOKEN: listening on loopback {host} only, without a pairing token")
    if not shutil.which("git"):
        log("warning: git not found on PATH; builds will fail to fetch")
    server = make_server(host, port, pairing_token=token)
    plugin = server.plugin
    log(f"listening on {host}:{server.server_address[1]} (state in {plugin.dir}); "
        f"platforms: {', '.join(plugin.platforms) or 'none'}")

    def on_term(signum, frame):
        # launchd/systemd stop: end the running build's process group too.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        plugin.runner.stop()


if __name__ == "__main__":
    main()
