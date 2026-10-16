# AGENTS.md

<!-- The map, not a rulebook. Keep it short; details live in the files it points to. Fill §1 per project. -->

## 1. Project
- What it is: <one paragraph: what this software does and for whom>
- Code: `<src/>`   Tests: `<tests/>`   Run tests: `<python3 -m pytest -q>` (same as `test_cmd` in `harness/config.json`)
- Conventions: <style, naming, commit style, anything non-obvious an agent would get wrong>

## 2. Two lanes
- Trivial change (typo, one-line fix, config tweak): just do it, with a test when behaviour changes.
- Feature (anything that needs requirements): goes through the harness in `harness/`. Never implement a
  feature outside it.

## 3. If your prompt names a file in `harness/roles/`
That file is your job description and is authoritative. Read it, then `harness/docs/sdd.md`.

## 4. Building a feature (you, the agent the human is talking to)
You were started by a human in this repo, inside herdr. When they ask for a feature:
```
python3 harness/harness.py start <slug> "what the feature must do"   # worktree + branch + feature folder
```
then follow `harness/roles/orchestrator.md` for that slug: it runs the spec author, implementer and
reviewer one at a time inside that worktree and stops at the two human gates. The human approves with
`python3 harness/harness.py approve <slug>` and merges with `python3 harness/harness.py merge <slug>`;
run those yourself only when the human says so. `python3 harness/harness.py status` shows every feature
in every worktree. Pick the slug yourself if the human gives none: short, lowercase, dashes.

## 5. Rules for everyone
- Commit messages start with the feature slug: `<slug>: <what>`.
- Never edit another feature's folder or `harness/` outside `harness/features/<slug>/`.
- Never commit to the base branch; only `approve` and `merge` do.
- `python3 harness/harness.py check <slug>` must be green before you stop.
- Inside herdr (`HERDR_ENV=1`), when you need herdr beyond `harness.py`, run `herdr --skill` and follow it.

## 6. Where things are
- Rules and lifecycle: `harness/docs/sdd.md`
- Agent CLIs and models per role, test and setup commands: `harness/config.json`
- Features: `harness/features/<slug>/{spec,tasks,log,review}.md`
