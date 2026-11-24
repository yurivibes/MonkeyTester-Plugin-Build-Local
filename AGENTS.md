# AGENTS.md — MonkeyTester-Plugin-Build-Local

## Purpose
`build-local`, a MonkeyTester build plugin: it implements `BUILD_PLUGIN_API.md` (contract
`monkeytester-build-plugin`, kept in the MonkeyTester repo) and builds an app repo's branch on
its own host (Android and iOS on a Mac, Android only on Linux), then uploads the result to the
viewer. See README.md (setup, configuration, API) and SECURITY.md (threat model).

## Setup (do this first)
README.md → **Install** is the single source for dependencies. The code is Python 3.10+,
standard library only. With Nix, `nix run .` starts the plugin on :8094 and `nix run .#tests`
runs the tests.

## Tests
`nix run .#tests`, or `python3 -m unittest discover -s tests -v`. They use a fake Gitea, a fake
GitHub and a fake viewer in-process, so no real forge or viewer is contacted. Gitea and GitHub
here are product features (the code hosts the plugin builds from), not where this repo is hosted.

## Hosting and review workflow
This repo isn't tied to one code host: it can live on GitHub, Gitea/Forgejo, GitLab, Bitbucket or a
mirror. Find the host from the remote you were asked to work against (`git remote -v`, usually
`origin`) and use that host's tools; the docs never assume one.

| Remote host | Review is called | CLI |
|---|---|---|
| github.com or GitHub Enterprise | pull request | `gh pr create` |
| Gitea / Forgejo | pull request | `tea pr create` |
| GitLab | merge request | `glab mr create` |
| Bitbucket | pull request | web UI or REST API |

- `master` changes only through a reviewed PR/MR that a maintainer merges. Never push to `master`
  and never merge your own PR/MR.
- Work on a feature branch in its own git worktree, based on the remote's `master`.
- CI is in `.github/workflows/`, which both GitHub Actions and Gitea Actions run. Its test job runs the same command as `nix run .#tests`; on a host without Actions, run that locally before opening the PR/MR.
- In docs, name sibling repos (e.g. MonkeyTester-Plugin-AI) instead of linking a host URL, link
  files in this repo with relative paths, and stick to CommonMark plus GFM tables and code
  fences (no host-only syntax such as `> [!NOTE]` callouts or `#123` autolinks).
