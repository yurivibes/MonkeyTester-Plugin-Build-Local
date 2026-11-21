"""Build runner for build-local: one build at a time, FIFO queue.

Per build (BUILD_PLUGIN_API.md, POST /builds):
  1. fetch the branch into <state>/checkouts/<forge_kind>/<owner>/<repo>
     (shallow, credentials per command through GIT_CONFIG_* env: never in
     .git/config, the remote URL, argv or the log),
  2. run each chosen platform's recipe (`bash .monkeytester/build-<p>.sh`) in its
     own process group with VT_PLATFORM / VT_OUTPUT / VT_BRANCH / VT_CASES /
     VT_REQUEST_ID, under a per-platform timeout,
  3. upload through the viewer's submit.py (`--no-wait`): built apps, and the
     error + log of the platforms that failed.

Builds are kept in <state>/builds.json; a build that was queued or running when
the plugin stopped is marked failed ("interrupted") at the next start. Each
build's files live in <state>/out/<build_ref>/ (build.log, <platform>-build.log,
the app files, submit.py); only the newest KEEP_OUT builds keep them.

Standard library only.
"""
from __future__ import annotations

import collections
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import sources
from sources import PLATFORMS, RECIPES

MAX_BUILDS = 500          # records kept in builds.json
KEEP_OUT = 50             # builds whose out dir (logs, apps) is kept
DEFAULT_TIMEOUT = 3600    # per platform, seconds (BUILD_LOCAL_TIMEOUT_<PLATFORM>)
FETCH_TIMEOUT = 1800
UPLOAD_TIMEOUT = 1800
SUBMIT_WAIT = 3600        # submit.py --timeout (unused with --no-wait, kept for older viewers)
KILL_GRACE = 10.0         # SIGTERM -> SIGKILL
MAX_ERROR_LINES = 5
MAX_ERROR_CHARS = 1500
APP_NAMES = {"android": "app.apk", "ios": "app.ipa"}
JOB_NAMES = {"android": "Android", "ios": "iOS", "upload": "Upload"}
BUILD_REF_RE = re.compile(r"^br_[0-9a-f]{16}$")
# Never handed to a build script or git.
PRIVATE_ENV = ("PLUGIN_TOKEN", "BUILD_LOCAL_IMPORT")
# Transports git may use for the fetch and for submodules (GIT_ALLOW_PROTOCOL):
# never ext:: (runs a command), file:// or other local paths. Tests that fetch
# file:// fixtures add "file" through a source's CLONE_URL_OVERRIDE.
GIT_PROTOCOLS = ("http", "https", "ssh")

ERROR_LINE_RE = re.compile(r"(^|: )error: ")
ERROR_STRIP_RE = re.compile(r"^([^:]*: )?error: ")


def write_private_json(path, data):
    """Write JSON readable only by this user (0600), atomically."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def platform_timeout(platform, env=None):
    """Seconds a platform's recipe may run: BUILD_LOCAL_TIMEOUT_<PLATFORM>, default 3600."""
    env = os.environ if env is None else env
    raw = (env.get(f"BUILD_LOCAL_TIMEOUT_{platform.upper()}") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        value = DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _seconds(value):
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def build_error_message(log_path):
    """The log's last distinct `error:` lines, as the old CI template's
    "Collect the iOS build error" step did (grep '(^|: )error: ' | sed strip |
    awk dedupe): up to 5 lines joined with spaces, at most 1500 chars. "" when
    the log has none."""
    try:
        text = Path(log_path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    seen, lines = set(), []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not ERROR_LINE_RE.search(line):
            continue
        msg = ERROR_STRIP_RE.sub("", line, count=1)
        if msg in seen:
            continue
        seen.add(msg)
        lines.append(msg)
    return " ".join(lines[-MAX_ERROR_LINES:])[:MAX_ERROR_CHARS]


def child_env(extra=None):
    """The plugin's environment without its own secrets (PRIVATE_ENV and any
    GIT_CONFIG_* the plugin itself was started with), plus `extra`."""
    env = {k: v for k, v in os.environ.items()
           if k not in PRIVATE_ENV and not k.startswith("GIT_CONFIG_")}
    env.update(extra or {})
    return env


def git_env(auth_pairs, protocols=GIT_PROTOCOLS):
    """Environment for a git command: the credentials as GIT_CONFIG_* entries
    (git >= 2.31), credential helpers off, never a prompt, only `protocols` as
    transports (also for submodules), no `ext::`/local transports."""
    pairs = [("credential.helper", ""), ("protocol.allow", "never")]
    pairs += [(f"protocol.{p}.allow", "always") for p in protocols]
    pairs += list(auth_pairs)
    env = child_env({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(len(pairs)),
                     "GIT_ALLOW_PROTOCOL": ":".join(protocols), "GIT_PROTOCOL_FROM_USER": "0"})
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def git_protocols(source):
    """Allowed transports for a source: GIT_PROTOCOLS, plus file for the tests'
    CLONE_URL_OVERRIDE fixtures (never set in production)."""
    if getattr(source, "CLONE_URL_OVERRIDE", None):
        return GIT_PROTOCOLS + ("file",)
    return GIT_PROTOCOLS


def contained(root, *parts):
    """root/parts, resolved, or ValueError when it would leave root ("..",
    absolute parts, NUL, symlinks pointing outside)."""
    for part in parts:
        if not isinstance(part, str) or not part or "\x00" in part or part in (".", "..") \
                or "/" in part or "\\" in part:
            raise ValueError(f"unsafe path part {part!r}")
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath([base, path]) != base:
        raise ValueError("path leaves its root")
    return Path(path)


class Cancelled(Exception):
    pass


class Stopping(Exception):
    pass


class Runner:
    def __init__(self, directory, load_config, platforms, log=None):
        self.dir = Path(directory)
        self.path = self.dir / "builds.json"
        self.load_config = load_config
        self.platforms = list(platforms)
        self.log = log or (lambda msg: None)
        self.kill_grace = KILL_GRACE
        self.keep_out = KEEP_OUT
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.builds = read_json(self.path, {})
        self.queue = collections.deque()
        self.running = None
        self.proc = None
        self.cancel_event = threading.Event()
        self.stopping = False
        self.thread = None
        self._mark_interrupted()

    # ─── store ───

    def _save(self):
        if len(self.builds) > MAX_BUILDS:
            keep = sorted(self.builds, key=lambda r: self.builds[r].get("created") or 0, reverse=True)[:MAX_BUILDS]
            self.builds = {r: self.builds[r] for r in keep}
        write_private_json(self.path, self.builds)

    def out_dir(self, ref):
        if not BUILD_REF_RE.match(ref or ""):
            raise ValueError("not a build_ref")
        return contained(self.dir / "out", ref)

    def log_path(self, ref):
        return self.out_dir(ref) / "build.log"

    def _mark_interrupted(self):
        changed = False
        for ref, rec in self.builds.items():
            if not isinstance(rec, dict) or rec.get("state") == "completed":
                continue
            rec.update(state="completed", conclusion="failure", finished=time.time(),
                       error="interrupted: build-local stopped before this build finished")
            for job in (rec.get("jobs") or {}).values():
                if job.get("status") != "completed":
                    job.update(status="completed", conclusion="failure")
            self._append_log(ref, "==> interrupted: build-local stopped before this build finished\n")
            changed = True
        if changed:
            self._save()

    # ─── public API ───

    def start(self):
        self.thread = threading.Thread(target=self._worker, name="build-local-runner", daemon=True)
        self.thread.start()

    def stop(self, timeout=15):
        """Stop the worker; a running build's process group is killed and the
        build stays unfinished (the next start marks it interrupted)."""
        with self.lock:
            self.stopping = True
            self.cond.notify_all()
            proc = self.proc
        self.cancel_event.set()
        if proc is not None:
            self._kill_group(proc)
        if self.thread:
            self.thread.join(timeout)

    def submit(self, repo, branch, platforms, cases, request_id, forge_kind):
        with self.lock:
            ref = "br_" + secrets.token_hex(8)
            while ref in self.builds:
                ref = "br_" + secrets.token_hex(8)
            jobs = {p: {"status": "queued", "conclusion": None} for p in platforms}
            jobs["upload"] = {"status": "queued", "conclusion": None}
            self.builds[ref] = {
                "repo": repo, "branch": branch, "platforms": list(platforms), "cases": list(cases),
                "request_id": request_id, "forge_kind": forge_kind, "created": time.time(),
                "state": "queued", "conclusion": None, "error": None, "jobs": jobs,
            }
            self.queue.append(ref)
            self._save()
            self.cond.notify_all()
        return ref

    def get(self, ref):
        if not BUILD_REF_RE.match(ref or ""):
            return None
        with self.lock:
            rec = self.builds.get(ref)
            return json.loads(json.dumps(rec)) if isinstance(rec, dict) else None

    def body(self, ref):
        """Contract GET /builds/<ref> body, or None."""
        rec = self.get(ref)
        if rec is None:
            return None
        jobs = rec.get("jobs") or {}
        order = [p for p in PLATFORMS if p in jobs] + ["upload"]
        return {
            "state": rec.get("state") or "pending",
            "conclusion": rec.get("conclusion") if rec.get("state") == "completed" else None,
            "url": "",
            "jobs": [{"name": JOB_NAMES[k], "status": jobs.get(k, {}).get("status") or "queued",
                      "conclusion": jobs.get(k, {}).get("conclusion"), "url": ""} for k in order],
            "error": rec.get("error"),
        }

    def queue_info(self):
        with self.lock:
            return {"running": self.running, "waiting": len(self.queue)}

    def cancel(self, ref):
        """Cancel a queued or running build. Returns its body, or None."""
        proc = None
        with self.lock:
            rec = self.builds.get(ref) if BUILD_REF_RE.match(ref or "") else None
            if rec is None:
                return None
            if ref in self.queue:
                self.queue.remove(ref)
                self._finish_cancelled(rec)
                self._append_log(ref, "==> cancelled while queued\n")
                self._save()
                return self.body(ref)
            running = self.running == ref
            if running:
                self.cancel_event.set()
                proc = self.proc
        if running:
            if proc is not None:
                threading.Thread(target=self._kill_group, args=(proc,), daemon=True).start()
            deadline = time.monotonic() + self.kill_grace + 10
            while time.monotonic() < deadline:
                with self.lock:
                    if self.builds.get(ref, {}).get("state") == "completed":
                        break
                time.sleep(0.05)
        return self.body(ref)

    def read_log(self, ref, offset=0, limit=1024 * 1024):
        """(bytes from offset, total size, complete) or None for an unknown build."""
        rec = self.get(ref)
        if rec is None:
            return None
        path = self.log_path(ref)
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(min(offset, size))
                data = f.read(limit)
        except OSError:
            size, data = 0, b""
        return data, size, rec.get("state") == "completed"

    # ─── worker ───

    def _worker(self):
        while True:
            with self.lock:
                while not self.queue and not self.stopping:
                    self.cond.wait()
                if self.stopping:
                    return
                ref = self.queue.popleft()
                self.running = ref
                self.cancel_event.clear()
            try:
                self._execute(ref)
            except Stopping:
                return
            except Exception as e:  # noqa: BLE001 - a bug must not kill the worker
                self.log(f"build {ref}: internal error {type(e).__name__}: {e}")
                with self.lock:
                    rec = self.builds.get(ref)
                    if rec and rec.get("state") != "completed":
                        self._finish(rec, "failure", f"internal error: {type(e).__name__}")
                        self._save()
            finally:
                with self.lock:
                    self.running = None
                    self.proc = None
            try:
                self._prune_out()
            except OSError:
                pass

    def _update(self, ref, **fields):
        with self.lock:
            rec = self.builds[ref]
            rec.update(fields)
            self._save()
            return rec

    def _job(self, ref, key, status, conclusion=None):
        with self.lock:
            self.builds[ref]["jobs"][key] = {"status": status, "conclusion": conclusion}
            self._save()

    def _finish(self, rec, conclusion, error=None, job_conclusion="failure"):
        for job in rec["jobs"].values():
            if job.get("status") != "completed":
                job.update(status="completed", conclusion=job_conclusion)
        rec.update(state="completed", conclusion=conclusion, error=error, finished=time.time())

    def _finish_cancelled(self, rec):
        self._finish(rec, "cancelled", "cancelled", job_conclusion="cancelled")

    def _append_log(self, ref, text):
        try:
            path = self.log_path(ref)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as f:
                f.write(text.encode())
        except (OSError, ValueError):
            pass

    def _check(self):
        if self.stopping:
            raise Stopping()
        if self.cancel_event.is_set():
            raise Cancelled()

    def _execute(self, ref):
        rec = self.get(ref)
        out = self.out_dir(ref)
        out.mkdir(parents=True, exist_ok=True)
        self._update(ref, state="in_progress", started=time.time())
        with open(self.log_path(ref), "ab") as logf:
            def note(text):
                logf.write(f"==> [{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n".encode())
                logf.flush()
            try:
                self._build(ref, rec, out, logf, note)
            except Cancelled:
                note("cancelled")
                with self.lock:
                    self._finish_cancelled(self.builds[ref])
                    self._save()

    def _build(self, ref, rec, out, logf, note):
        repo, branch = rec["repo"], rec["branch"]
        platforms = rec["platforms"]
        # 1. fetch
        note(f"fetch {repo} {branch}")
        self._check()
        checkout, error = self._fetch(rec, logf, note)
        if error:
            note(f"fetch failed: {error}")
            with self.lock:
                self._finish(self.builds[ref], "failure", f"fetch failed: {error}")
                self._save()
            return
        # 2. platforms
        built, failed = {}, {}
        for p in PLATFORMS:
            if p not in platforms:
                continue
            self._check()
            self._job(ref, p, "in_progress")
            app = out / APP_NAMES[p]
            plog = out / f"{p}-build.log"
            note(f"{JOB_NAMES[p]}: bash {RECIPES[p]} (VT_OUTPUT={app})")
            timeout = platform_timeout(p)
            env = child_env({"VT_PLATFORM": p, "VT_OUTPUT": str(app), "VT_BRANCH": branch,
                             "VT_CASES": ",".join(rec.get("cases") or []),
                             "VT_REQUEST_ID": rec["request_id"]})
            with open(plog, "ab") as pf:
                rc, outcome = self._run(["bash", RECIPES[p]], checkout, env, [logf, pf], timeout)
            if outcome == "timeout":
                msg = f"timed out after {_seconds(timeout)} s"
            elif rc == 0 and app.is_file() and app.stat().st_size > 0:
                msg = ""
            else:
                msg = build_error_message(plog)
                if not msg:
                    msg = (f"build script exited with status {rc}" if rc != 0
                           else "build script exited with status 0 but wrote no app at VT_OUTPUT")
            if msg:
                failed[p] = (msg, plog)
                note(f"{JOB_NAMES[p]}: failed: {msg}")
                self._job(ref, p, "completed", "failure")
            else:
                built[p] = app
                note(f"{JOB_NAMES[p]}: built {app.name} ({app.stat().st_size} bytes)")
                self._job(ref, p, "completed", "success")
        # 3. upload
        self._check()
        self._job(ref, "upload", "in_progress")
        ok, error = self._upload(rec, checkout, out, built, failed, logf, note)
        self._job(ref, "upload", "completed", "success" if ok else "failure")
        with self.lock:
            target = self.builds[ref]
            if not built:
                errors = "; ".join(f"{JOB_NAMES[p]}: {m}" for p, (m, _) in failed.items())
                self._finish(target, "failure", f"every platform failed to build ({errors})")
            elif not ok:
                self._finish(target, "failure", f"upload failed: {error}")
            else:
                self._finish(target, "success", None)
            self._save()
            note(f"done: {target['conclusion']}")

    def _fetch(self, rec, logf, note):
        """(checkout path, None) or (None, error message)."""
        cfg = self.load_config()
        kind = rec.get("forge_kind") or cfg.get("forge_kind")
        source = sources.get(kind)
        if source is None or not cfg.get("token"):
            return None, "build-local is not configured (no code host token)"
        if kind != cfg.get("forge_kind"):
            return None, f"the code host changed from {kind} to {cfg.get('forge_kind')}"
        owner, name = rec["repo"].split("/", 1)
        branch = rec["branch"]
        if not isinstance(branch, str) or not branch or branch.startswith("-") or ".." in branch:
            return None, "invalid branch name"
        try:
            checkout = contained(self.dir / "checkouts", kind, owner, name)
        except ValueError:
            return None, "invalid repo name"
        url = source.clone_url(cfg, rec["repo"])
        protocols = git_protocols(source)
        if urllib.parse.urlsplit(url).scheme not in protocols:
            return None, "the clone URL uses a transport build-local does not allow"
        env = git_env(source.git_auth_config(cfg, rec["repo"]), protocols)
        steps = []
        if not (checkout / ".git").is_dir():
            checkout.mkdir(parents=True, exist_ok=True)
            steps += [["git", "init", "-q"], ["git", "remote", "add", "--", "origin", url]]
        else:
            steps.append(["git", "remote", "set-url", "--", "origin", url])
        # `--` ends the options: a request value can never become a git option.
        steps += [["git", "fetch", "--depth", "1", "--", "origin", f"refs/heads/{branch}"],
                  ["git", "checkout", "--force", "FETCH_HEAD", "--"],
                  ["git", "clean", "-ffdx"]]
        for argv in steps:
            self._check()
            rc, outcome = self._git(argv, checkout, env, logf)
            if outcome == "timeout":
                return None, f"`git {argv[1]}` timed out after {FETCH_TIMEOUT} s"
            if rc != 0:
                return None, f"`git {argv[1]}` exited with status {rc} (see the log)"
        if (checkout / ".gitmodules").is_file():
            argv = ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"]
            rc, outcome = self._git(argv, checkout, env, logf)
            if rc != 0 or outcome != "exited":
                return None, f"`git submodule update` failed (status {rc})"
        return checkout, None

    def _git(self, argv, cwd, env, logf):
        logf.write(f"$ {' '.join(argv)}\n".encode())
        logf.flush()
        return self._run(argv, cwd, env, [logf], FETCH_TIMEOUT)

    def _upload(self, rec, checkout, out, built, failed, logf, note):
        """(ok, error): download the viewer's submit.py and run it with --no-wait."""
        cfg = self.load_config()
        viewer = (cfg.get("viewer_url") or "").rstrip("/")
        if not viewer:
            return False, "no viewer_url configured"
        submit = out / "submit.py"
        note(f"Upload: GET {viewer}/ci/submit.py")
        try:
            sources.require_http(viewer)
            with urllib.request.urlopen(f"{viewer}/ci/submit.py", timeout=30) as resp:  # nosec B310 - http(s) checked
                submit.write_bytes(resp.read())
        except sources.SourceError as e:
            note(f"Upload: viewer_url: {e}")
            return False, f"viewer_url: {e}"
        except (urllib.error.URLError, OSError) as e:
            msg = f"could not download submit.py from the viewer: {getattr(e, 'reason', e)}"
            note(f"Upload: {msg}")
            return False, msg
        argv = [sys.executable, str(submit), "--url", viewer, "--no-wait"]
        for p, app in built.items():
            argv += [f"--{p}-app", str(app)]
        # Request- and log-derived values ride as --flag=value, so a value
        # starting with "-" (an error line) can't be taken for another option.
        for p, (msg, plog) in failed.items():
            argv += [f"--{p}-build-error={msg}", f"--{p}-build-log", str(plog)]
        argv += ["--tests-dir", "visual-tests", f"--branch={rec['branch']}", f"--repo={rec['repo']}",
                 f"--cases={','.join(rec.get('cases') or [])}", f"--build-request={rec['request_id']}",
                 "--timeout", str(SUBMIT_WAIT)]
        note("Upload: python3 submit.py " + " ".join(argv[3:]))
        rc, outcome = self._run(argv, checkout, child_env(), [logf], UPLOAD_TIMEOUT)
        if outcome == "timeout":
            return False, f"submit.py timed out after {UPLOAD_TIMEOUT} s"
        if rc != 0:
            note(f"Upload: submit.py exited with status {rc}")
            return False, f"the viewer refused the upload (submit.py exited with status {rc})"
        return True, None

    # ─── processes ───

    def _run(self, argv, cwd, env, sinks, timeout):
        """Run argv in its own process group, copying stdout+stderr to sinks.

        Returns (exit status or None, "exited" | "timeout"). Raises Cancelled /
        Stopping after killing the group when the build is cancelled or the
        plugin stops."""
        self._check()
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            for sink in sinks:
                sink.write(f"cannot start {argv[0]}: {e}\n".encode())
                sink.flush()
            return 127, "exited"
        with self.lock:
            self.proc = proc
        reader = threading.Thread(target=self._copy, args=(proc.stdout, sinks), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        outcome, rc = "exited", None
        try:
            while True:
                try:
                    rc = proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.stopping or self.cancel_event.is_set():
                    self._kill_group(proc)
                    outcome = "cancelled"
                    break
                if time.monotonic() > deadline:
                    self._kill_group(proc)
                    outcome = "timeout"
                    break
        finally:
            # Anything the script left running in its group ends with the step.
            self._signal_group(proc, signal.SIGKILL)
            reader.join(5)
            try:
                proc.stdout.close()
            except OSError:
                pass
            with self.lock:
                self.proc = None
        if outcome == "exited" and rc is not None and rc < 0 and (
                self.stopping or self.cancel_event.is_set()):
            # The kill from cancel()/stop() beat the wait loop: the process
            # died because we killed its group, not because the step ended.
            outcome = "cancelled"
        if outcome == "cancelled":
            self._check()
            raise Cancelled()
        if outcome == "timeout":
            for sink in sinks:
                sink.write(f"\n==> killed: timed out after {_seconds(timeout)} s\n".encode())
                sink.flush()
        return rc, outcome

    @staticmethod
    def _copy(stream, sinks):
        try:
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    return
                for sink in sinks:
                    try:
                        sink.write(chunk)
                        sink.flush()
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError):
            return

    @staticmethod
    def _signal_group(proc, sig):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _kill_group(self, proc):
        """SIGTERM the process group, SIGKILL after kill_grace seconds."""
        self._signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=self.kill_grace)
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _prune_out(self):
        root = self.dir / "out"
        if not root.is_dir():
            return
        with self.lock:
            active = set(self.queue) | ({self.running} if self.running else set())
            created = {r: (self.builds.get(r) or {}).get("created") for r in self.builds}
        dirs = [d for d in root.iterdir() if d.is_dir() and d.name not in active]

        def key(d):
            return created.get(d.name) or d.stat().st_mtime
        dirs.sort(key=key, reverse=True)
        for d in dirs[self.keep_out:]:
            shutil.rmtree(d, ignore_errors=True)
