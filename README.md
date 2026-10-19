# Project template: spec-driven development on herdr

A starting point for a coding project where features are specified before they are built and the work
is done by agent CLIs (Claude Code, Codex, pi, ...) running in parallel, one git worktree per feature,
under [herdr](https://herdr.dev). Everything harness-related lives in `harness/`; `AGENTS.md` at the
root is the map every agent reads. The rest of the repo is your project.

## Prerequisites
- git and [uv](https://docs.astral.sh/uv/). The launcher is stdlib-only Python, so plain
  `python3 harness/harness.py ...` also works where uv is not installed.
- [herdr](https://herdr.dev/docs/install/) (`curl -fsSL https://herdr.dev/install.sh | sh`)
- The agent CLIs you name in `harness/config.json` (for example `claude`, `codex`, `pi`), logged in

## New project
1. Copy this template and start fresh history:
   `git clone <this repo> myproject && cd myproject && rm -rf .git && git init -b main`
2. Edit `AGENTS.md` §1 (what the project is, where code and tests live, how to run tests).
3. Edit `harness/config.json` (every key and value is explained in `harness/help/config.md`): `test_cmd` and
   optional `setup_cmd` (the defaults assume a uv project:
   `uv run pytest -q` and `uv sync`), `copy_files` (gitignored files such as `.env` that each new
   worktree needs, copied from the main checkout; a worktree only gets what git tracks; a
   `.worktreeinclude` file in the repo lists more), optional `teardown_cmd` (run in the worktree before
   `merge` removes it) and `max_review_rounds` (after that many `CHANGES_REQUESTED` the feature waits
   for you), the two `gates` (`approve` and `merge`,
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
The agent runs `uv run harness/harness.py start login "..."`, which creates a worktree
(`~/.herdr/worktrees/<repo>/login`), the branch `login`, and `harness/features/login/`, then
`uv run harness/harness.py drive login --tab`, which starts the driver: a launcher process in a `driver`
tab of that worktree's workspace. The driver runs the spec author, implementer and reviewer one after
another, each in its own tab (labelled with the role), merges `main` in and runs the tests before every
review, sends failed checks and conflicts back to the role, and stops at the two human gates. When it
stops it prompts your agent with a line `harness: login ...`, and the board says what is needed:
```
uv run harness/harness.py status          # "needs you": spec_ready → read harness/features/login/spec.md, then
uv run harness/harness.py approve login   # ... the driver continues with implementer and reviewer
uv run harness/harness.py status          # "needs you": review + APPROVED:
uv run harness/harness.py merge login
```
You can type those yourself, tell the agent "approved" / "merge it", or set the gates to `auto` in
`harness/config.json` so the driver goes from request to merged feature without stopping. Your agent is
free the moment the driver starts, so several features can run in parallel, each with its own worktree,
workspace and driver. Feedback at a gate goes through the driver too:
`uv run harness/harness.py drive login "<feedback>" --tab`.

`start --detach` instead starts a dedicated orchestrator agent inside the worktree (in a tab labelled
`orchestrator`); talk to it by focusing its tab or `herdr agent prompt <slug> "..."`. Each feature's
workspace nests under the workspace you asked from, as with the sidebar's "new worktree". If `start`
runs outside herdr, or from a workspace that is not on the main checkout, herdr finds or opens a
workspace for the repo instead.

To see each feature's state in herdr's sidebar (`spec ready · needs you: approve`, `implementer ·
working`, ...), add the `$harness` token to the Space rows in `~/.config/herdr/config.toml` and reload:
```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]
```
`uv run harness/harness.py doctor` checks the tools and the config and lists leftovers (worktrees,
branches, herdr workspaces) with the command that removes each.

## Layout
```
AGENTS.md                      the map (read natively by Claude Code, Codex, pi)
harness/
  config.json                  agent kind + model per role, gates, test/setup/teardown commands, files copied into worktrees (the only tool-specific file)
  help/config.md               what every key in config.json means and which values it accepts
  harness.py                   status | start | drive | run | wait | approve | merge | check | doctor
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
