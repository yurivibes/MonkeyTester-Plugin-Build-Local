# Security: MonkeyTester-Plugin-Build-Local

`build-local` **runs the build scripts of the branches it is asked to build**. Anyone
who can call `POST /builds` and point it at a repo the configured token can read
gets code execution on this host as the plugin's user. Treat the port like an SSH
login.

This document covers this plugin only. The family-wide overview (every service, its
port, bind and auth, and the hardening tiers) lives in the MonkeyTester repo's
`SECURITY.md`.

## Threat model

- **Where:** a home LAN (192.168.125.0/24), one user. The plugin runs on the Mac
  (192.168.125.71, LaunchAgent) or a Linux host (systemd user unit, Android only).
  The MonkeyTester viewer on the Linux host (192.168.125.70) calls it over the LAN.
- **Who we defend against:** other devices on the LAN (guests, a compromised IoT
  device, a browser page making requests to LAN addresses) that can reach port 8094.
- **Not in scope:** an attacker who already has a shell as the plugin's user, or who
  can push to a repo you build (its build script runs by design — only add trusted
  repos as projects).

"No known holes" is the goal; this is not a claim of zero vulnerabilities.

## What is protected, and how

| Area | Protection |
|---|---|
| Access | `PLUGIN_TOKEN` pairing token on every request except `GET /health`, as `X-Plugin-Token` or `Authorization: Bearer`; compared with `hmac.compare_digest`. |
| Bind | `PLUGIN_HOST` defaults to `0.0.0.0` (the viewer reaches the Mac over the LAN). **On any non-loopback bind the token is mandatory**: without `PLUGIN_TOKEN` the plugin exits with status 2 and says how to set it. On `127.0.0.1` / `::1` / `localhost` the token is optional. |
| `/health` | Unauthenticated, never contains a secret. Without the token it only answers the contract, plugin name/version, code host kind, platforms, recipes and capabilities (`auth_required: true`); the code host URL/user, viewer URL and queue need the token. It may add `"docs": "/docs"` (README.md is readable); `GET /docs` itself needs the token and is served as HTML under `default-src 'none'` with no scripts, raw HTML in the README escaped. |
| Code host token | Stored in `config.json` (mode 0600, dir 0700); `GET /config` returns only `{configured, last4, saved_at}`. Given to git per command via `GIT_CONFIG_*` environment — never in argv, the remote URL, `.git/config` or a log. |
| Subprocesses | argv lists only, never `shell=True`. Repo `owner/name`, branch/ref, case names, request ids and build refs are validated against allowlist regexes (no leading `-`, no `..`, no control chars). git commands put `--` before positional values; request/log values passed to `submit.py` use `--flag=value` so they can never become another option. |
| git transports | `GIT_ALLOW_PROTOCOL=http:https:ssh` and `protocol.allow=never` (plus per-protocol allows) for the fetch **and submodules**: no `ext::` (runs a command), no `file://`/local paths. The clone URL's scheme is checked before any git command runs. |
| Build environment | The plugin's own secrets (`PLUGIN_TOKEN`, `BUILD_LOCAL_IMPORT`, and any `GIT_CONFIG_*` it was started with) are removed from the environment of git, the build scripts and `submit.py`. |
| Paths | Checkouts, build output and logs are resolved with `realpath` and must stay under their root (`commonpath`); `..`, absolute parts, NUL and symlinks out are refused. Build refs must match `br_<16 hex>`. |
| HTTP | Bodies over 1 MiB → 413 (checked from `Content-Length` before reading), chunked bodies → 411, bad JSON / bad `Content-Length` → 400, 60 s socket timeout per request. Unexpected errors answer `{"error": "internal error: <Type>"}` — the traceback goes to the plugin's log only. |
| Outbound URLs | `urlopen` only ever opens `http://` / `https://` URLs (code host API, viewer `/health`, viewer `submit.py`). |

## Token setup

1. Generate a token on any machine:

   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

2. Give it to the plugin.
   - **macOS LaunchAgent** (`~/Library/LaunchAgents/com.blackjackfruit.monkeytester-build-local.plist`):
     add to the `EnvironmentVariables` dict

     ```xml
     <key>PLUGIN_TOKEN</key><string>the-token-from-step-1</string>
     ```

     then `launchctl unload` + `launchctl load` the plist. Keep the plist readable
     by your user only (`chmod 600`) — it now holds a secret.
   - **Linux systemd user unit:** `systemctl --user edit monkeytester-build-local` and add

     ```ini
     [Service]
     Environment=PLUGIN_TOKEN=the-token-from-step-1
     ```

     or, when the viewer runs on the same Linux host, `Environment=PLUGIN_HOST=127.0.0.1`
     (loopback: no token needed).
3. Enter **the same token** for the plugin in the viewer: **Settings → Build & Test →
   build-local → token**. The viewer sends it as `X-Plugin-Token` on every call.

To rotate: set a new `PLUGIN_TOKEN`, restart the plugin, update the viewer's Settings.

## Not covered

- **No TLS.** Traffic between the viewer and the plugin, including the pairing token,
  is plaintext HTTP on the LAN. For access from outside the LAN use a VPN (WireGuard,
  Tailscale), never a port forward.
- **Plaintext secrets at rest.** The code host token is plaintext in `config.json`
  (0600) and `PLUGIN_TOKEN` is plaintext in the plist / unit. Anyone with your user's
  files has both.
- **Build scripts are trusted code.** A build runs with the plugin user's environment,
  keychain (signing identity) and SDKs. Run the plugin as a dedicated, never-root user.
- Not done yet (tier 3+): TLS/VPN by default, launchd/systemd sandboxing, CI
  dependency/secret scanning.

## Reporting a problem

Open an issue in this repository's issue tracker, or tell the
owner directly for anything that should not be written down first.
