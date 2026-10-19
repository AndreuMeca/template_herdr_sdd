# Spec-driven development rules

The only rules file. `AGENTS.md` is the map, `harness/roles/*.md` give each agent its job, and
`harness.py check` enforces what a script can enforce. When a role file and this file disagree, this
file wins. Commands: `uv run harness/harness.py --help`.

## 1. Lifecycle

One feature = one slug = one folder `harness/features/<slug>/` = one branch = one worktree. The
orchestrator (the agent the human talks to, on the base checkout) starts a feature and hands it to the
driver: a launcher process that runs the roles one at a time in the feature's worktree, sends failed
checks and conflicts back, runs the gates set to `auto`, and stops when a human is needed. One writer
per checkout, always.

| status | set by | means | next |
|---|---|---|---|
| `pending` | `start` | folder exists, intent written | spec_author |
| `spec_ready` | spec_author | requirements, design, tasks written; `check` green | `approve` |
| `approved` | `approve` | spec frozen | implementer |
| `in_progress` | implementer at start; reviewer on CHANGES_REQUESTED | code and tests being written | implementer |
| `review` | implementer | every task ticked, `check --tests` green | reviewer; with `APPROVED` → `merge` |
| `done` | `merge` | merged into the base branch, worktree removed | — |
| `blocked` | spec_author, implementer, or the launcher | needs a human decision (`## Open questions` or `log.md`) | human answers: `drive <slug> "<answer>" --tab` |

The gates are `approve` and `merge`; `gates` in `harness/config.json` sets each to `human` (the driver
stops and prompts the orchestrator; what a missing key means) or `auto` (the driver runs it; the shipped
config). Only `merge` writes to
the base branch. The launcher blocks a feature itself after `max_review_rounds` reviews ending in
`CHANGES_REQUESTED`, or when a check it sent back still fails after three fixes; the reason goes to
`log.md`.

## 2. Feature files

| file | owner | format |
|---|---|---|
| `spec.md` | spec_author | frontmatter with one key, `status`. Sections in order: `# Title`, `## Intent`, `## Requirements`, `## Design`, `## Verification`, `## Open questions`. |
| `tasks.md` | spec_author writes, implementer ticks | `# Tasks`, then `- [ ] T<n>: <text> (R1, R2)` per task, ordered by dependency, each citing at least one R. |
| `log.md` | implementer, append-only | `# Log`, then per session `## <YYYY-MM-DD HH:MM> implementer` with `done:`, `decisions:`, `blockers:`, `next:`. |
| `review.md` | reviewer, one per round | frontmatter with one key, `verdict: APPROVED` or `verdict: CHANGES_REQUESTED`; `## Findings` (one actionable bullet per finding with the R id and `file:line`; "none" if approved); `## Checked` (the §5 checkpoints, `[x]`/`[ ]`). |

`## Design` lists the files to touch, interfaces and signatures, data flow, decisions, one discarded
alternative and why, and what is out of scope. `## Verification` maps each R to the test that covers
it. The launcher removes `review.md` before each review round, so a verdict on disk always belongs to
the current code.

## 3. Requirements (EARS)

- One per line, `- R<n>: <text>`, ids from R1 and **stable**: never renumber; a dropped one stays as
  `- R<n>: withdrawn`. After approval the requirements are frozen (rule 11); a change needs a new spec round.
- Each active R has exactly one `SHALL` and one pattern:
  `THE SYSTEM SHALL <response>.` · `WHEN <trigger>, THE SYSTEM SHALL ...` · `WHILE <state>, THE SYSTEM SHALL ...` ·
  `WHERE <feature is enabled>, THE SYSTEM SHALL ...` · `IF <condition>, THEN THE SYSTEM SHALL ...`
- Observable behaviour with concrete values (`returns 404`, `within 2 s`), *what* not *how*, no soft
  verbs (`should`, `may`, `try to`).
- `## Open questions` is empty at `spec_ready`; a question the intent and the code cannot answer is
  listed there with status `blocked`.

## 4. Traceability

Every active R has a test carrying the token `<slug>:R<n>` (for example `login:R3`) in its name, its
docstring, or a comment on the line above; `check` verifies this with `git grep`, so the token is the
contract. Tests exercise real behaviour: none that only assert "does not raise", no mocking of the
filesystem when a temporary directory works.

## 5. Reviewer checkpoints

Recorded one by one in `## Checked`; any unchecked box means `CHANGES_REQUESTED`.

1. Every active R has a passing test with its token that actually asserts the R's behaviour.
2. No behaviour that no R describes (scope creep); no R silently reinterpreted.
3. Every task ticked and the diff matches the tasks; nothing undocumented.
4. `log.md` explains the non-obvious decisions and matches the code.
5. Error paths and edge values named in the requirements are handled.
6. No debug leftovers, secrets, ownerless TODOs or unrelated reformatting.
7. Conventions from `AGENTS.md` §1 respected.
8. `check <slug> --tests` green on the branch as it stands.

## 6. Git and worktree rules

- Commit messages: `<slug>: <what>`. A role commits before it stops; uncommitted changes mean it has
  not finished.
- Never edit another feature's folder, nor `harness/` outside `harness/features/<slug>/`.
- Never merge or rebase the base branch yourself: the launcher merges it in before every review and at
  `merge`, runs the tests, and sends a conflict back to the implementer.
- Never force-push, delete branches or remove worktrees; `merge` does that.

## 7. What `check` verifies

| # | rule | from status |
|---|---|---|
| 1 | `status` in `spec.md` is one of the lifecycle table | always |
| 2 | at least one active R, ids unique, each with `SHALL`; `## Open questions` empty | `spec_ready` |
| 3 | every task cites an existing R; every active R is cited by a task | `spec_ready` |
| 4 | every active R has its `<slug>:R<n>` token outside `harness/` (`git grep --untracked`) | `review` |
| 5 | every task ticked and `log.md` exists | `review` |
| 6 | `test_cmd` exits 0 | `review`, or `--tests` |
| 7 | nothing under `harness/` changed outside the feature folder (branch vs base, plus uncommitted and untracked files) | always, on a feature branch |
| 8 | nothing changed outside the feature folder | `pending`, `spec_ready`, `approved` |
| 9 | since the reviewer started (`refs/harness/<slug>/review-start`), only `review.md` and the status line of `spec.md` changed | while a verdict is on disk |
| 10 | an `APPROVED` verdict exists only if the launcher started a reviewer round; one the implementer wrote is discarded | `review` |
| 11 | `## Requirements` equals the one in the last `<slug>: spec approved` commit | `approved`, `in_progress`, `review` |

`blocked` triggers rules 1 and 7 only; `merge` also requires `verdict: APPROVED`. When a role stops, the
launcher runs rules 1–5 and 7–11 and requires a clean worktree; a role that leaves any of them red has
not finished (NEEDS_ATTENTION, or sent back by the driver, three times at most). Commands and checks
come from the main checkout's `config.json`, so a branch cannot change what it is tested with.

## 8. Launcher semantics not in `--help`

- `run`/`wait` exit 0 DONE, 3 STILL_WORKING (call `wait`), 2 NEEDS_ATTENTION, 1 error. "Done" is
  decided from the files on disk and a green check, never from the agent's screen alone.
- `drive <slug> "msg"`: the message goes to the first role run. At a gate, to the role that produced
  what is judged (spec → spec_author, merge → implementer); when blocked, to the implementer if the spec
  was ever approved, else the spec_author. `--tab` runs the driver in the feature's workspace and
  returns; when it stops, it prompts the pane it was started from with `harness: <slug> ...`.
- While a driver runs a feature, `run` and `merge` refuse it; while a role agent is still working,
  `run` refuses to prompt it and the driver waits.
