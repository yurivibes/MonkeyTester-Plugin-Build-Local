# MonkeyTester-Build-Local

`build-local` is a MonkeyTester build plugin: it implements
[BUILD_PLUGIN_API.md](BUILD_PLUGIN_API.md) (contract `monkeytester-build-plugin`,
version 1, kept in the MonkeyTester repo) with builds that run **on the plugin's
own host**. The viewer's Build & Test page asks it for a repo's branches and test
cases and to build a branch; the plugin fetches the code from Gitea or GitHub with
a read-only token, runs the app repo's `.monkeytester/build-<platform>.sh` scripts
here (Android and iOS on a Mac, Android only on Linux) and uploads the built apps
to the viewer with the viewer's `submit.py`, so the run attaches to the build.

The code-host URL and token live only in this plugin; the viewer never sees the
token. A build that fails is not an HTTP error: `GET /builds/<ref>` is `200` with
a failed conclusion. A platform that failed to build while another one built still
ends the build as `success`: the failed platform is uploaded as a build error with
its log, so its devices show every test as Unavailable with View logs. Builds run
one at a time (FIFO queue); queued or running builds can be cancelled
(`POST /builds/<ref>/cancel` terminates the script's process group), and the build
log is tail-able (`GET /builds/<ref>/log?offset=` with `X-Log-Size` /
`X-Log-Complete`).

- Default port: **8094**
- Python 3.10+, standard library only. Runs on Linux and macOS.

## Requirements per host

- **Mac (builds Android and iOS):** macOS with Xcode (the plugin detects iOS with
  `xcodebuild -version`), the Android SDK (detected via `ANDROID_HOME` /
  `ANDROID_SDK_ROOT` / `~/Library/Android/sdk` / `~/Android/Sdk`), a JDK for the
  Android build, and — for the signed `.ipa` — the code-signing identity in the
  **login keychain of the user the plugin runs as** (the launchd agent runs as an
  ordinary user; keychain access needs the `Aqua` session type, see the plist).
  The provisioning profile must cover each test iPhone's UDID.
- **Linux (Android only):** the Android SDK plus a JDK; iOS is not offered.

## Install

### With Nix (Linux or macOS)

```sh
git clone <the repo url>            # ssh://git@192.168.125.70:2222/yurivibes/MonkeyTester-Build-Local.git
cd MonkeyTester-Build-Local
nix run .            # plugin on :8094
nix run .#tests      # unit tests against the fake Gitea/GitHub
```

The flake builds from git-tracked files only, so `git add` new files before
`nix run`.

To keep it running:

- Linux: `packaging/systemd/monkeytester-build-local.service` (user unit; the
  install steps are in the file's header; Android only).
- macOS: `packaging/launchd/com.blackjackfruit.monkeytester-build-local.plist`
  (LaunchAgent; replace `CHECKOUT_DIR` and `PYTHON`).

### Without Nix

Any `python3` 3.10 or newer works, with no extra packages:

```sh
python3 build_plugin.py
python3 -m unittest discover -s tests -v
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PLUGIN_HOST` | `0.0.0.0` | listen address. The default is the LAN, because the viewer on the Linux host reaches the Mac over it — so the default **needs `PLUGIN_TOKEN`**. `127.0.0.1` listens on this machine only |
| `PLUGIN_PORT` | `8094` | listen port |
| `PLUGIN_TOKEN` | unset | pairing token: every request except `GET /health` must send it as `X-Plugin-Token` or `Authorization: Bearer`. **Mandatory when `PLUGIN_HOST` is not loopback** (`127.0.0.1`, `::1`, `localhost`): without it the plugin refuses to start (exit status 2). Generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `BUILD_PLUGIN_STATE_DIR` | `~/.config/monkeytester-build-local/` | `config.json` (code host + token + viewer URL, mode 0600), `builds.json`, `checkouts/`, `out/` (newest 50 builds keep their files) |
| `BUILD_LOCAL_PLATFORMS` | auto-detect | e.g. `android` to force Android only |
| `BUILD_LOCAL_TIMEOUT_ANDROID` / `BUILD_LOCAL_TIMEOUT_IOS` | `3600` | seconds a platform's build script may run |
| `BUILD_LOCAL_IMPORT` | unset | first start only: import `url` + `token` + `user` from this `gitea_config.json` (the viewer's old Gitea settings file) as a Gitea code host |

The token is plaintext at rest in `config.json` (mode 0600): the plugin reads it
without a person present, so encrypting it on the same disk would not add real
protection. It is never returned by any endpoint, never written to a build log or
to any repo's `.git/config`, and passed to git only per command via environment.

## Register the plugin in the viewer

1. Set `PLUGIN_TOKEN` (see [SECURITY.md](SECURITY.md#token-setup)) and start the plugin.
2. In the viewer: **Settings → Build & Test → Add build plugin**. Name `build-local`,
   address `http://<host>:8094`, and the same pairing token as `PLUGIN_TOKEN`.
3. Click **Configure** on the plugin and enter:
   - the **code host**: Gitea or GitHub,
   - its **URL**: the Gitea URL (e.g. `http://192.168.125.70:3000`) or the GitHub
     API base (`https://api.github.com`, or `https://<host>/api/v3` for GitHub
     Enterprise Server),
   - a **read-only token** — it only reads the repo and fetches it with git:
     - Gitea: a token with `read:repository` (plus `read:user`) under
       Settings → Applications → Access tokens,
     - GitHub: a fine-grained token with Contents: read + Metadata: read, or a
       classic token with the `repo` scope for private repos,
   - the **viewer URL** (prefilled with the address the browser is using) — the
     plugin uploads to it.
   The plugin checks the token against the code host (who it belongs to) before
   it saves anything.
4. Add projects (one repo each) and pick `build-local` as their build plugin.

## The app repo side

Commit one script per platform at the paths the plugin reports in `/health`
`recipes`: `.monkeytester/build-android.sh` and `.monkeytester/build-ios.sh`
(`GET /template?platform=` returns the example scripts — download them from
Settings and commit them to your repo). The plugin runs each with `bash` from the
root of a clean checkout of the branch, with:

| Variable | Value |
|---|---|
| `VT_PLATFORM` | `android` or `ios` |
| `VT_OUTPUT` | where the script must write the app: a `.apk` (Android) or a signed `.ipa` (iOS) |
| `VT_BRANCH` | the branch being built |
| `VT_CASES` | comma-separated case names |
| `VT_REQUEST_ID` | the viewer's build id |

Exit status `0` with a non-empty file at `VT_OUTPUT` is a successful build;
anything else is a build error, reported with the script's last `error:` lines
and its log.

## Security

See [SECURITY.md](SECURITY.md) for the threat model, token setup (launchd
`EnvironmentVariables`, systemd `Environment=`) and what is not covered (no TLS,
plaintext secrets at rest). In short: on a LAN bind the plugin will not start
without `PLUGIN_TOKEN`; `/health` is the only unauthenticated route and carries no
secret or configuration.

The plugin **runs the scripts of the branch it builds** — run it as a dedicated,
never-root user, and only add trusted repos as projects: a repo's build script
runs with that user's environment, keychain and SDKs. The forge token is read-only
and stays out of every log and git config. Builds are one-at-a-time; use
`POST /builds/<ref>/cancel` to stop a queued or running build (the script's whole
process group is terminated), and `GET /builds/<ref>/log` to watch what it is
doing.

## Tests

`python3 -m unittest discover -s tests -v` — fake Gitea, fake GitHub and a fake
viewer, all in-process on 127.0.0.1; a real forge or viewer is never contacted.
CI (`.gitea/workflows/test.yml`) runs the same command.
