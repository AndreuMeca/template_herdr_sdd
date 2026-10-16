# Project template: spec-driven development on herdr

A starting point for a coding project where features are specified before they are built and the work
is done by agent CLIs (Claude Code, Codex, pi, ...) running in parallel, one git worktree per feature,
under [herdr](https://herdr.dev). Everything harness-related lives in `harness/`; `AGENTS.md` at the
root is the map every agent reads. The rest of the repo is your project.

## Prerequisites
- git, python3 (3.9+, stdlib only)
- [herdr](https://herdr.dev/docs/install/) (`curl -fsSL https://herdr.dev/install.sh | sh`)
- The agent CLIs you name in `harness/config.json` (for example `claude`, `codex`, `pi`), logged in

## New project
1. Copy this template and start fresh history:
   `git clone <this repo> myproject && cd myproject && rm -rf .git && git init -b main`
2. Edit `AGENTS.md` §1 (what the project is, where code and tests live, how to run tests).
3. Edit `harness/config.json`: `test_cmd`, optional `setup_cmd`, the two `gates` (`approve` and `merge`,
   each `human` to stop and wait for you, the default, or `auto` to let the orchestrator run them), and
   for each role the agent CLI
   (`kind`, one of herdr's agent kinds), the `model`, and extra CLI `args`. Role agents need
   non-interactive permission flags or they stop at the first approval dialog. The launcher answers
   Claude Code's two startup dialogs by itself (trust this folder, bypass-permissions warning); any other
   dialog is reported as NEEDS_ATTENTION for the orchestrator or you to answer. The `orchestrator` entry
   is only used by `start --detach`.
4. Add your project code anywhere outside `harness/`. Commit.
5. Open herdr, open a workspace in the repo, start your agent (`claude`, `codex`, `pi`, ...) and ask for
   a feature. It reads `AGENTS.md` and takes it from there.

## Daily flow
You, to the agent you started on `main`: "Build email+password login with a cookie session."
The agent runs `python3 harness/harness.py start login "..."`, which creates a worktree
(`~/.herdr/worktrees/<repo>/login`), the branch `login`, and `harness/features/login/`. It then runs the
spec author, implementer and reviewer one after another, each in its own tab of that worktree's
workspace (labelled with the role), and stops
at the two human gates:
```
python3 harness/harness.py status          # spec_ready: read harness/features/login/spec.md, then
python3 harness/harness.py approve login   # ... the agent continues with implementer and reviewer
python3 harness/harness.py status          # review + APPROVED:
python3 harness/harness.py merge login
```
You can type those yourself, tell the agent "approved" / "merge it", or set the gates to `auto` in
`harness/config.json` so the agent goes from request to merged feature without stopping. Several features can run in
parallel, each in its own worktree. `start --detach` instead starts a dedicated orchestrator agent inside
the worktree (in a tab labelled `orchestrator`), for work you want to leave running on its own; talk to
it by focusing its tab or
`herdr agent prompt <slug> "..."`. If the repo itself is not open in herdr yet, herdr opens a workspace
for it when the first worktree is created.

## Layout
```
AGENTS.md                      the map (read natively by Claude Code, Codex, pi)
harness/
  config.json                  agent kind + model per role, test/setup commands (the only tool-specific file)
  harness.py                   status | start | run | wait | approve | merge | check
  docs/sdd.md                  all the rules: lifecycle, file formats, EARS, traceability, checkpoints
  roles/*.md                   job description of orchestrator, spec_author, implementer, reviewer
  features/_template/          copied by start
  features/<slug>/             spec.md, tasks.md, log.md, review.md
```
State lives in markdown: `status` in the frontmatter of `spec.md`, `verdict` in `review.md`. There is no
database and no central list; `status` scans every worktree.

## Credits
Adapted from [betta-tech/harness-sdd](https://github.com/betta-tech/harness-sdd) and built on
[herdr](https://herdr.dev).
