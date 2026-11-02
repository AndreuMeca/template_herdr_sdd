# AGENTS.md

<!-- The map, not a rulebook. §1 is per project; everything else points to its one source. -->

## 1. Project
<!-- Filled in by the orchestrator with the human before the first feature. -->
- What it is: <one paragraph: what this software does and for whom>
- Stack and layout: <language, framework; where code and tests live>
- Conventions: <style, naming, commit style, anything non-obvious an agent would get wrong>

Tests run with `test_cmd` in `harness/config.json`.

## 2. Two lanes
- Trivial change (typo, one-line fix, config tweak): just do it, with a test when behaviour changes.
- Feature (anything that needs requirements): through the harness in `harness/`, never outside it.

## 3. Your role
- Your prompt names a file in `harness/roles/`: that file is your job. Read it, then `harness/docs/sdd.md`.
- Otherwise you are the orchestrator, the agent the human talks to: follow `harness/roles/orchestrator.md`.
  Before your first `start` or `drive` in a session (not for questions, reviews or trivial changes), once:
  run `uv run harness/harness.py config` and `uv run harness/harness.py doctor` (both read-only, no
  tokens; `doctor` also runs `test_cmd`), show the setup together with the model and permission mode you
  are running with, plus any `doctor` problem with its fix, ask the human whether this is the setup they
  want, and wait for the answer (they may ask you to change it: `harness/roles/orchestrator.md` says how).
  When `doctor` says §1 above is not filled in, fill it in with the human in the same step (same file).
- Your own model and permissions are not in `harness/config.json`: the human chose them by starting you.
  To change them they use your CLI's own commands (Claude Code: `/model`, `/permissions`) or start a new
  session; never change them yourself.

## 4. Where things are
- Rules, lifecycle, what `check` enforces: `harness/docs/sdd.md`
- Commands: `uv run harness/harness.py --help`
- Config (agent per role, gates, commands, files copied into worktrees): `harness/config.json`, explained in `harness/help/config.md`
- Features: `harness/features/<slug>/{spec,tasks,log,review}.md`
- herdr beyond `harness.py` (inside herdr, `HERDR_ENV=1`): `herdr --skill`
