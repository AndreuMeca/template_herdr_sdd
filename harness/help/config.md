# harness/config.json

The only tool-specific file in the template. JSON cannot hold comments, so this file explains it.
Unknown keys (like `_help`) are ignored by the launcher.

## Keys

| key | required | values | what it does |
|---|---|---|---|
| `base_branch` | yes | a branch name, usually `main` | `start` forks feature branches from it; `merge` merges them back into it. |
| `test_cmd` | yes | a shell command | Run from the repo (or worktree) root by `check` from status `review` on, and by `merge` on the merged tree. Exit 0 means green. Examples: `uv run pytest -q`, `python3 -m unittest discover -s tests -q`, `npm test`. |
| `setup_cmd` | no | a shell command or `""` | Run once by `start` inside the new worktree, before any agent works there. Use it to create the environment: `uv sync`, `npm ci`. Empty means nothing to do. |
| `gates.approve` | no | `human` (default) or `auto` | Who approves a spec. `human`: the orchestrator stops after the spec and waits for you to run `approve <slug>` or say "approved". `auto`: the orchestrator judges the spec and runs `approve` itself. |
| `gates.merge` | no | `human` (default) or `auto` | Who merges. `human`: the orchestrator stops after the reviewer's APPROVED and waits for `merge <slug>`. `auto`: the orchestrator merges itself, unless it is a detached orchestrator inside the worktree (merging would delete its own workspace), in which case it stops and tells you. |
| `agents.<role>` | yes, for all four roles | an object, see below | Which CLI and model run each role: `orchestrator` (only used by `start --detach`), `spec_author`, `implementer`, `reviewer`. |

## `agents.<role>`

| key | required | values | what it does |
|---|---|---|---|
| `kind` | yes | a herdr agent kind: `claude`, `codex`, `pi`, `gemini`, `opencode`, `cursor`, `copilot`, `amp`, `droid`, `grok`, `kimi`, `kiro`, `cline`, `qwen`, `hermes`, `kilo`, `letta`, `devin`, `agy`, `omp`, `mastracode`, `qodercli`, `maki`, `muse` (`herdr agent start --help` lists the ones your herdr knows) | The CLI herdr starts for this role. The CLI must be installed and logged in. This is also what picks the provider: `claude` is Anthropic, `codex` is OpenAI, `gemini` is Google; `pi` and `opencode` take the provider inside the model string. |
| `model` | no | a model id the CLI accepts, or `""` | Passed as `--model <model>` after `--`. Empty means the CLI's default model. |
| `args` | no | a list of strings | Extra CLI flags, appended verbatim after the model flag. Use it for non-interactive permissions: role agents must not stop at approval dialogs. |

Put the model and flags the CLI itself understands; herdr only passes them through. Check with
`<cli> --help` when in doubt. The launcher answers Claude Code's two startup dialogs (trust folder,
bypass-permissions warning) by itself; any other dialog is reported as NEEDS_ATTENTION.

### Ready-to-copy entries per CLI

Claude Code (`claude --help`): models `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`.
```json
{ "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] }
{ "kind": "claude", "model": "claude-opus-5-5",   "args": ["--permission-mode", "acceptEdits"] }
```
`--dangerously-skip-permissions` is what role agents need. `--permission-mode acceptEdits` still asks
before shell commands, which is fine for a detached orchestrator you watch, not for roles.

Codex (`codex --help`): leave `model` empty for the CLI's default, or set one it lists.
```json
{ "kind": "codex", "model": "", "args": ["--ask-for-approval", "never", "--sandbox", "workspace-write"] }
```
`--dangerously-bypass-approvals-and-sandbox` removes the sandbox as well; only in a disposable machine.

pi (`pi --help`): the model string carries the provider, `provider/model`, optionally `:thinking`.
```json
{ "kind": "pi", "model": "anthropic/claude-sonnet-5-5", "args": [] }
{ "kind": "pi", "model": "openai/gpt-5",                "args": [] }
```
pi runs tools without approval prompts, so no permission flag is needed.

Gemini CLI, opencode and the others: not verified with this template. They accept `--model`; check
their help for the non-interactive flag (Gemini CLI calls it `--yolo`) and put it in `args`.

## Complete examples

Everything on Claude, fully automatic once you ask for a feature:
```json
{
  "base_branch": "main",
  "test_cmd": "uv run pytest -q",
  "setup_cmd": "uv sync",
  "gates": { "approve": "auto", "merge": "auto" },
  "agents": {
    "orchestrator": { "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] },
    "spec_author":  { "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] },
    "implementer":  { "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] },
    "reviewer":     { "kind": "claude", "model": "claude-opus-5-5",   "args": ["--dangerously-skip-permissions"] }
  }
}
```

Mixed vendors, you approve specs and merges yourself (the shipped default):
```json
{
  "base_branch": "main",
  "test_cmd": "uv run pytest -q",
  "setup_cmd": "uv sync",
  "gates": { "approve": "human", "merge": "human" },
  "agents": {
    "orchestrator": { "kind": "claude", "model": "claude-opus-5-5",   "args": ["--permission-mode", "acceptEdits"] },
    "spec_author":  { "kind": "claude", "model": "claude-sonnet-5-5", "args": ["--dangerously-skip-permissions"] },
    "implementer":  { "kind": "codex",  "model": "",                  "args": ["--ask-for-approval", "never", "--sandbox", "workspace-write"] },
    "reviewer":     { "kind": "pi",     "model": "anthropic/claude-sonnet-5-5", "args": [] }
  }
}
```

Cheap, for trying the harness out:
```json
{
  "base_branch": "main",
  "test_cmd": "python3 -m unittest discover -s tests -q",
  "setup_cmd": "",
  "gates": { "approve": "auto", "merge": "auto" },
  "agents": {
    "orchestrator": { "kind": "claude", "model": "claude-haiku-5-5", "args": ["--dangerously-skip-permissions"] },
    "spec_author":  { "kind": "claude", "model": "claude-haiku-5-5", "args": ["--dangerously-skip-permissions"] },
    "implementer":  { "kind": "claude", "model": "claude-haiku-5-5", "args": ["--dangerously-skip-permissions"] },
    "reviewer":     { "kind": "claude", "model": "claude-haiku-5-5", "args": ["--dangerously-skip-permissions"] }
  }
}
```

The launcher validates the file on every command: a missing required key, a role without `kind`, or a
gate value other than `human`/`auto` stops with a message naming the problem.
