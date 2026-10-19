# AGENTS.md

<!-- The map, not a rulebook. Fill §1 per project; everything else points to its one source. -->

## 1. Project
- What it is: <one paragraph: what this software does and for whom>
- Code: `<src/>`   Tests: `<tests/>`   Run tests: `<uv run pytest -q>` (same as `test_cmd` in `harness/config.json`)
- Conventions: <style, naming, commit style, anything non-obvious an agent would get wrong>

## 2. Two lanes
- Trivial change (typo, one-line fix, config tweak): just do it, with a test when behaviour changes.
- Feature (anything that needs requirements): through the harness in `harness/`, never outside it.

## 3. Your role
- Your prompt names a file in `harness/roles/`: that file is your job. Read it, then `harness/docs/sdd.md`.
- Otherwise you are the orchestrator, the agent the human talks to. Before anything else in a session,
  once: run `uv run harness/harness.py config`, show its output together with the model you are running
  as, ask the human whether this is the setup they want, and wait for the answer (they may ask you to
  change it: `harness/roles/orchestrator.md` says how). Then follow `harness/roles/orchestrator.md`.

## 4. Where things are
- Rules, lifecycle, what `check` enforces: `harness/docs/sdd.md`
- Commands: `uv run harness/harness.py --help`
- Config (agent per role, gates, commands, files copied into worktrees): `harness/config.json`, explained in `harness/help/config.md`
- Features: `harness/features/<slug>/{spec,tasks,log,review}.md`
- herdr beyond `harness.py` (inside herdr, `HERDR_ENV=1`): `herdr --skill`
