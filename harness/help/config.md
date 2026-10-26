# harness/config.json

The only tool-specific file. JSON cannot hold comments, so this file explains it; the shipped
`config.json` is the default example (mixed vendors, automatic gates). Unknown keys such as `_help` are
ignored. The launcher validates the file on every command and names the problem;
`uv run harness/harness.py config` prints the setup in a few lines, which the orchestrator shows you
before the first feature of a session. The driver (`drive`) is the launcher itself, not an agent: it
has no model and no cost.

## Keys

| key | required | values | what it does |
|---|---|---|---|
| `base_branch` | yes | branch name | `start` forks from it; `merge` merges back into it. |
| `test_cmd` | yes | shell command | Run at the worktree root by `check` from status `review` on and by `merge`. Exit 0 = green. `uv run pytest -q`, `npm test`. |
| `setup_cmd` | no | shell command or `""` | Run once by `start` in the new worktree: `uv sync`, `npm ci`. |
| `copy_files` | no | paths or globs relative to the repo root, default `[]` | Gitignored files `start` copies from the main checkout into the worktree (a worktree only gets what git tracks): `[".env"]`. A `.worktreeinclude` file at the repo root (one entry per line, `#` comments) adds to the list. Tracked paths are skipped; copies, not links. Not `.venv` or `node_modules`: `setup_cmd` rebuilds them. |
| `teardown_cmd` | no | shell command or `""` | Run by `merge` in the worktree before removing it: `docker compose down`. |
| `max_review_rounds` | no | integer, default `3`, `0` = no limit | After this many `CHANGES_REQUESTED` reviews the feature is set `blocked` for the human. |
| `gates.approve`, `gates.merge` | no | `human` or `auto`; a missing key means `human` | `human`: the driver stops and prompts the orchestrator. `auto`: the driver runs the command itself. The shipped file sets both to `auto`. |
| `agents.<role>` | yes, all four | object, below | Which CLI and model run `spec_author`, `implementer`, `reviewer`, and `orchestrator`. The orchestrator entry starts the agent of `start --detach`, and when its kind is `claude` it also sets what the Claude you open in this repo starts with: `config` and `start` write its `model` and the permission mode its `args` imply (`--dangerously-skip-permissions` → bypass, `--permission-mode X` → X, else default) to `.claude/settings.local.json`, read at the next session start. |

## `agents.<role>`

| key | required | values | what it does |
|---|---|---|---|
| `kind` | yes | a herdr agent kind (`herdr agent start --help` lists them): `claude`, `codex`, `pi`, `gemini`, `opencode`, ... | The CLI herdr starts, installed and logged in. It picks the provider; `pi` and `opencode` take it in the model string. |
| `model` | no | a model id the CLI accepts, or `""` | Passed as `--model`; empty = the CLI's default. |
| `effort` | no | `claude`: `low`, `medium`, `high`, `xhigh`, `max`. `codex`: `minimal`, `low`, `medium`, `high`, `xhigh`. Or `""` | The reasoning effort, translated per CLI (`--effort` for Claude Code, `-c model_reasoning_effort=` for Codex). Empty = the CLI's default. Ignored for other CLIs; `config` says so. |
| `args` | no | list of strings | Extra flags, verbatim. Role agents need the non-interactive permission flag or they stop at the first dialog. |

herdr only passes model and flags through; check `<cli> --help`. The launcher answers Claude Code's
two startup dialogs (trust folder, bypass-permissions warning) itself; any other dialog is reported.

Ready-to-copy entries:
```json
{ "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] }
{ "kind": "claude", "model": "claude-opus-5-5",   "args": ["--permission-mode", "acceptEdits"] }
{ "kind": "codex",  "model": "",                  "args": ["--ask-for-approval", "never", "--sandbox", "workspace-write"] }
{ "kind": "pi",     "model": "anthropic/claude-sonnet-5-5", "args": [] }
```
`--permission-mode acceptEdits` still asks before shell commands: fine for a detached orchestrator you
watch, not for roles. Codex's `--dangerously-bypass-approvals-and-sandbox` also drops the sandbox; only
on a disposable machine. With `--sandbox workspace-write` instead, Codex cannot commit inside a feature
worktree (the worktree's git dir lives in the main checkout): add `--add-dir <repo>/.git` to its `args`,
or the driver stops at the merge gate with "uncommitted changes left behind". pi never asks. Other CLIs are unverified; put their non-interactive flag
(Gemini CLI: `--yolo`) in `args`.

## herdr's sidebar

The launcher reports each feature's state (`spec ready · needs you: approve`, `implementer · working`,
...) as the Space token `$harness` of its workspace. herdr shows custom tokens only when the layout names
them: add once to `~/.config/herdr/config.toml` and run `herdr server reload-config` (`doctor` reminds you):
```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]
```
