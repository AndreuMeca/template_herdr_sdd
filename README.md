# Project template: spec-driven development on herdr

A starting point for a coding project where features are specified before they are built, by agent
CLIs (Claude Code, Codex, pi, ...) working in parallel, one git worktree per feature, under
[herdr](https://herdr.dev). Everything harness-related lives in `harness/`; `AGENTS.md` is the map
every agent reads; the rest of the repo is your project.

## Prerequisites
- git and [uv](https://docs.astral.sh/uv/) (the launcher is stdlib-only Python; `python3 harness/harness.py` also works)
- [herdr](https://herdr.dev/docs/install/): `curl -fsSL https://herdr.dev/install.sh | sh`
- The agent CLIs named in `harness/config.json`, logged in
- A read of the trust model, `harness/docs/sdd.md` §9: role agents run with the permissions you give them
  in `config.json`, and the harness's checks catch mistakes, not a hostile agent

## New project
1. GitHub's "Use this template", or `git clone <this repo> myproject && cd myproject && rm -rf .git &&
   git init -b main && git add -A && git commit -m "start from template"`
2. Edit `harness/config.json`: agent per role, gates, commands, files to copy into worktrees. Every
   key is explained in `harness/help/config.md`.
3. Open the repo in herdr, start your agent and ask for a feature. Before the first one it runs `doctor`
   (the setup, tools, config, and `test_cmd` on the main checkout, so a red baseline is not blamed on
   the first implementer), fills in `AGENTS.md` §1 with you, and waits for your yes. A repo with no code
   yet is fine: the first feature sets the project up. `doctor --models` also sends each Claude Code and Codex role
   one tiny prompt with its model, so a wrong model id fails there (a few tokens). Effort and permissions
   use the file's own words for every CLI (`low`…`max`, `guarded`/`full`); `harness/help/config.md` shows
   what each CLI is given. The orchestrator is not in `config.json`: it is the agent you start in the repo,
   with whatever model and permissions you start it with (change them there: Claude Code `/model`,
   `/permissions`).

## Daily flow
You, to the agent on `main`: "Build email+password login with a cookie session." It runs `start login`
(worktree, branch, `harness/features/login/`, nested workspace, and the driver in its own tab), and is
free again. The driver runs the spec author, implementer and reviewer in their own tabs, merges `main` in and
tests before every review, and sends failures back. With the shipped config (`gates` set to `human`) it
stops twice, for you to read the spec and to merge, and prompts your agent with `harness: login ...`:
```
uv run harness/harness.py status          # needs you: spec_ready → read spec.md, then
uv run harness/harness.py approve login   # the driver continues
uv run harness/harness.py status          # needs you: review + APPROVED
uv run harness/harness.py merge login
```
Type those, or tell the agent "approved" / "merge it". Feedback at a gate: `drive login "<feedback>" --tab`.
Set a gate to `auto` and the driver runs it itself (`harness/help/config.md`, "Fully automatic").
Several features run at once, one driver each; `merge` is local, so a push is always yours. Each
feature's state shows in herdr's sidebar once you add one line to its config (`harness/help/config.md`).

## Layout
```
AGENTS.md                      the map (read natively by Claude Code, Codex, pi)
harness/
  harness.py                   the launcher: --help lists every command
  selftest.py                  the launcher's own tests: python3 harness/selftest.py (ruff.toml: its lint settings);
                               .github/workflows/harness.yml runs both when harness/ changes
  config.json                  agent per role, gates, commands, files copied into worktrees
  help/config.md               every key of config.json
  docs/sdd.md                  the rules: lifecycle, file formats, EARS, traceability, what check enforces
  roles/*.md                   job of orchestrator, spec_author, implementer, reviewer
  features/_template/          copied by start
  features/<slug>/             spec.md, tasks.md, log.md, review.md
```
State is markdown: `status` in `spec.md`, `verdict` in `review.md`; `status` scans every worktree.
