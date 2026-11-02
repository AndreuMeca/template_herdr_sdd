# harness/config.json

The only tool-specific file. JSON cannot hold comments, so this file explains it; the shipped
`config.json` is the safe default (Claude roles, a Codex reviewer, CLI default models, `guarded`, both gates
`human`); "Fully automatic" below shows the other end. Unknown keys such as `_help` are
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
| `max_role_minutes` | no | integer, default `180`, `0` = no limit | When one role run (prompt to settled) takes longer, the driver stops and prompts the orchestrator ("still working after N min"). The agent keeps running; you decide whether to wait (`drive <slug> --tab` again) or stop it. |
| `gates.approve`, `gates.merge` | no | `human` or `auto`; a missing key means `human` | `human`: the driver stops and prompts the orchestrator. `auto`: the driver runs the command itself. The shipped file sets both to `human`. |
| `agents.<role>` | yes, all three | object, below | Which agent harness and model run `spec_author`, `implementer` and `reviewer`. Not the orchestrator: that is whichever agent you start in the repo (it reads `AGENTS.md`), with the model and permissions you start it with. |

## `agents.<role>`

You never write a provider's flag for effort or permissions: the file has its own words, and the launcher
translates them for each CLI. `config` shows both.

| key | required | values | what it does |
|---|---|---|---|
| `harness` | yes | the agent CLI, by its herdr agent kind (`herdr agent start --help` lists them): `claude`, `codex`, `pi`, `gemini`, `opencode`, ... | The CLI herdr starts, installed and logged in. It picks the provider; `pi` and `opencode` take it in the model string. |
| `model` | no | the provider's model id, or `""` | Passed as `--model`; empty = the CLI's default. Kept as the provider names it, so you always see what you pay for; `doctor --models` checks it. |
| `effort` | no | `low`, `medium`, `high`, `extra`, `max`, or `""` | How hard the model thinks, translated per CLI (table below). Empty = the CLI's default. |
| `permissions` | yes | `guarded`, `full` | What the agent may do without asking (table below). Roles run unattended, so there is no level that asks. |
| `args` | no | list of strings | Any other flag, verbatim (`{repo}` becomes the main checkout's absolute path, so the file stays the same on every machine). Not a permission flag when `permissions` is set: `config` rejects that. |

**effort**, by meaning; where a CLI has no such level it gets its nearest one:

| ours | claude (`--effort`) | codex (`-c model_reasoning_effort=`) | pi (`--thinking`) |
|---|---|---|---|
| `low` | low | low | low |
| `medium` | medium | medium | medium |
| `high` | high | high | high |
| `extra` | xhigh | xhigh | xhigh |
| `max` | max | xhigh (its highest) | max |

**permissions**, by what the agent does:

| ours | the agent | claude | codex | pi |
|---|---|---|---|---|
| `guarded` | never asks; the CLI blocks risky actions | `--permission-mode auto` (a classifier reviews each action) | `--sandbox workspace-write --ask-for-approval never --add-dir {repo}/.git` (writes only in its worktree, plus the git dir it needs to commit) | — |
| `full` | never asks, no limits | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | (pi never asks) |

A level a CLI does not have (—) is a config error that names the one it does have. `guarded` means the
same promise through different mechanisms; `full` is only for a machine you would not mind an agent
breaking. What either means for the harness: `harness/docs/sdd.md` §9. For a CLI not in these tables
(`gemini`, `opencode`, ...), `effort` and `permissions` are not translated: `config` says so, and its own
flags go in `args` (Gemini CLI: `--yolo`). To translate for another CLI, add one `Cli(...)` entry to `CLIS`
in `harness/harness.py` (its flags, the `doctor --models` probe, the startup dialogs it may show) and one
column to the tables above.

herdr only passes model and flags through. The launcher answers Claude Code's two startup dialogs (trust
folder, bypass-permissions warning) itself; any other dialog is reported.

Ready-to-copy entries:
```json
{ "harness": "claude", "model": "claude-sonnet-5-5", "effort": "high",  "permissions": "full" }
{ "harness": "claude", "model": "claude-opus-5-5",   "effort": "extra", "permissions": "guarded" }
{ "harness": "codex",  "model": "",                  "effort": "extra", "permissions": "guarded" }
{ "harness": "pi",     "model": "anthropic/claude-sonnet-5-5", "effort": "medium", "permissions": "full" }
```

## Fully automatic
Nothing stops for you: the driver approves the spec and merges by itself, and the Claude roles never ask.
Only on a machine you would not mind an agent breaking, and nobody reads the spec or the code before it
lands on the base branch (`harness/docs/sdd.md` §9). Replace `gates` and `agents` with:
```json
"gates": { "approve": "auto", "merge": "auto" },
"agents": {
  "spec_author": { "harness": "claude", "model": "claude-fable-5-1", "effort": "extra", "permissions": "full" },
  "implementer": { "harness": "claude", "model": "claude-opus-5-5",  "effort": "high",  "permissions": "full" },
  "reviewer":    { "harness": "codex",  "model": "gpt-6-astra",      "effort": "extra", "permissions": "guarded" }
}
```

## herdr's sidebar

The launcher reports each feature's state (`spec ready · needs you: approve`, `implementer · working`,
...) as the Space token `$harness` of its workspace. herdr shows custom tokens only when the layout names
them: add once to `~/.config/herdr/config.toml` and run `herdr server reload-config` (`doctor` reminds you):
```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]
```
