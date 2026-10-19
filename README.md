# Project template: spec-driven development on herdr

A starting point for a coding project where features are specified before they are built, by agent
CLIs (Claude Code, Codex, pi, ...) working in parallel, one git worktree per feature, under
[herdr](https://herdr.dev). Everything harness-related lives in `harness/`; `AGENTS.md` is the map
every agent reads; the rest of the repo is your project.

## Prerequisites
- git and [uv](https://docs.astral.sh/uv/) (the launcher is stdlib-only Python; `python3 harness/harness.py` also works)
- [herdr](https://herdr.dev/docs/install/): `curl -fsSL https://herdr.dev/install.sh | sh`
- The agent CLIs named in `harness/config.json`, logged in

## New project
1. `git clone <this repo> myproject && cd myproject && rm -rf .git && git init -b main`
2. Fill `AGENTS.md` §1: what the project is, where code and tests live, how to run tests.
3. Edit `harness/config.json`: agent per role, gates, commands, files to copy into worktrees. Every
   key is explained in `harness/help/config.md`.
4. Add your code outside `harness/`, commit, open the repo in herdr, start your agent and ask for a
   feature. `uv run harness/harness.py doctor` checks tools and config.

## Daily flow
You, to the agent on `main`: "Build email+password login with a cookie session." It runs `start login`
(worktree, branch, `harness/features/login/`, nested workspace) and `drive login --tab`, and is free
again. The driver runs the spec author, implementer and reviewer in their own tabs, merges `main` in and
tests before every review, sends failures back, and stops at the two human gates, prompting your agent
with `harness: login ...`:
```
uv run harness/harness.py status          # needs you: spec_ready → read spec.md, then
uv run harness/harness.py approve login   # the driver continues
uv run harness/harness.py status          # needs you: review + APPROVED
uv run harness/harness.py merge login
```
Type those, tell the agent "approved" / "merge it", or set the gates to `auto`. Feedback at a gate:
`drive login "<feedback>" --tab`. Several features run at once, one driver each. Each feature's state
shows in herdr's sidebar once you add one line to its config (`harness/help/config.md`).

## Layout
```
AGENTS.md                      the map (read natively by Claude Code, Codex, pi)
harness/
  harness.py                   the launcher: --help lists every command
  selftest.py                  the launcher's own tests: python3 harness/selftest.py (ruff.toml: its lint settings)
  config.json                  agent per role, gates, commands, files copied into worktrees
  help/config.md               every key of config.json
  docs/sdd.md                  the rules: lifecycle, file formats, EARS, traceability, what check enforces
  roles/*.md                   job of orchestrator, spec_author, implementer, reviewer
  features/_template/          copied by start
  features/<slug>/             spec.md, tasks.md, log.md, review.md
```
State is markdown: `status` in `spec.md`, `verdict` in `review.md`; `status` scans every worktree.

## Credits
Adapted from [betta-tech/harness-sdd](https://github.com/betta-tech/harness-sdd), built on
[herdr](https://herdr.dev).
