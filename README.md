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
3. Edit `harness/config.json`: `test_cmd`, optional `setup_cmd`, and for each role the agent CLI
   (`kind`, one of herdr's agent kinds), the `model`, and extra CLI `args`. Role agents need
   non-interactive permission flags or they stop at the first approval dialog. The launcher answers
   Claude Code's two startup dialogs by itself (trust this folder, bypass-permissions warning); any other
   dialog is reported as NEEDS_ATTENTION for you or the orchestrator to answer.
4. Add your project code anywhere outside `harness/`. Commit.
5. Open herdr in the repo (`herdr`), then from a shell pane on `main`:
   `python3 harness/harness.py start <slug> "what the feature must do"`

## Daily flow
```
python3 harness/harness.py start login "Email+password login with a cookie session"
python3 harness/harness.py status                      # when it says spec_ready:
python3 harness/harness.py approve login               # read harness/features/login/spec.md first
python3 harness/harness.py status                      # when it says review + APPROVED:
python3 harness/harness.py merge login
```
Each `start` creates a worktree (`~/.herdr/worktrees/<repo>/<slug>`), a branch named after the slug and
an orchestrator agent inside it (if the repo itself is not open in herdr yet, herdr opens a workspace for
it too). The orchestrator runs the spec author, implementer and reviewer one
after another in sibling panes of its worktree and stops at the two human gates: approving the spec and
merging. Several features run in parallel, each in its own worktree. Talk to an orchestrator by focusing
its pane or `herdr agent prompt <slug> "..."`.

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
