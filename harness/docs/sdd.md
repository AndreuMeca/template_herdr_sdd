# Spec-driven development rules

This is the only rules file. `AGENTS.md` is the map, `harness/roles/*.md` tell each agent its job, and
`harness/harness.py check` enforces what a script can enforce. When a role file and this file disagree,
this file wins.

## 1. Lifecycle

One feature = one slug = one folder `harness/features/<slug>/` = one branch = one worktree. The
orchestrator is the agent the human talks to on the base checkout (or a dedicated agent inside the
worktree, with `start --detach`). It normally hands the loop to the driver (`drive <slug> --tab`): a
launcher process in the `driver` tab of the feature's workspace that runs the roles, sends failed checks
and merge conflicts back, runs the gates that are `auto` and stops when a human is needed. Role agents
(spec_author, implementer, reviewer) run one at a time inside the feature's worktree, each in a tab of
its herdr workspace labelled with the role, so there is never more than one writer in a checkout.

| status | set by | means | next |
|---|---|---|---|
| `pending` | `start` | folder exists, intent written | `run spec_author` |
| `spec_ready` | spec_author | requirements, design, tasks written; `check` green | human reads the spec → `approve` |
| `approved` | `approve` (human) | spec frozen; implementation may start | `run implementer` |
| `in_progress` | implementer at start; reviewer on CHANGES_REQUESTED | code and tests being written | `run implementer` |
| `review` | implementer | every task ticked, `check --tests` green | `run reviewer`; with verdict `APPROVED` → `merge` |
| `done` | `merge` (human) | merged into the base branch, worktree removed | — |
| `blocked` | spec_author, implementer, or the launcher | needs a human decision (`## Open questions` or `log.md`) | human answers: `drive <slug> "<answer>" --tab`, or re-run the role with the answer as message |

The only gates are `approve` and `merge`. `gates` in `harness/config.json` sets each one to `human`
(the driver stops, prompts the orchestrator and waits for the human, the default) or `auto` (the driver
or the orchestrator runs the command itself). The board's NEXT column shows which applies. The only
command that writes to the base branch is `merge`; everything else happens on the feature branch.

The launcher blocks a feature itself when `max_review_rounds` reviews ended in `CHANGES_REQUESTED`, or
when a check the driver sent back still fails after three fixes. The reason is appended to `log.md`.

## 2. Feature files

| file | owner | format |
|---|---|---|
| `spec.md` | spec_author | YAML frontmatter with exactly one key, `status`. Sections, in order: `# Title`, `## Intent`, `## Requirements`, `## Design`, `## Verification`, `## Open questions`. |
| `tasks.md` | spec_author writes, implementer ticks | `# Tasks` then one line per task: `- [ ] T<n>: <text> (R1, R2)`. Ordered by dependency. Each task cites at least one R. |
| `log.md` | implementer (append-only) | `# Log` then one entry per working session: `## <YYYY-MM-DD HH:MM> implementer` with `done:`, `decisions:`, `blockers:`, `next:` bullets. Never rewrite earlier entries. |
| `review.md` | reviewer (overwritten each round) | YAML frontmatter with exactly one key, `verdict: APPROVED` or `verdict: CHANGES_REQUESTED`, then `## Findings` (one bullet per finding, citing the R id and `file:line`) and `## Checked` (the checkpoints walked). |

`review.md` is removed by the launcher before a new review round and after an approved review is
reopened, so a verdict on disk always belongs to the current code.

## 3. Requirements (EARS)

- One requirement per line, `- R<n>: <text>`, ids start at R1 and are **stable**: never renumber. A
  dropped requirement stays as `- R<n>: withdrawn`.
- Every active requirement contains exactly one `SHALL` and follows one EARS pattern:
  - Ubiquitous: `THE SYSTEM SHALL <response>.`
  - Event: `WHEN <trigger>, THE SYSTEM SHALL <response>.`
  - State: `WHILE <state>, THE SYSTEM SHALL <response>.`
  - Optional feature: `WHERE <feature is enabled>, THE SYSTEM SHALL <response>.`
  - Unwanted behaviour: `IF <condition>, THEN THE SYSTEM SHALL <response>.`
- Requirements say *what*, with observable behaviour and concrete values (`returns 404`, `within 2 s`),
  never *how*. No soft verbs (`should`, `may`, `try to`).
- `## Design` lists the files to touch, new interfaces or signatures, the data flow, decisions taken, at
  least one alternative discarded and why, and what is out of scope.
- `## Open questions` must be empty at `spec_ready`. If a question cannot be answered from the intent and
  the codebase, list it and set `blocked`.

## 4. Traceability

Every active R has at least one test, and every such test carries the token `<slug>:R<n>` (for example
`login:R3`) in its name, its docstring, or a comment on the line above. `check` verifies this with
`git grep`, so the token is the contract: no token, no coverage. `## Verification` in `spec.md` is the
human-readable map from R to test, filled by spec_author and corrected by implementer.

Tests must exercise real behaviour: no tests that only assert "does not raise", no mocking of the
filesystem when a temporary directory works, no `done` without a green `check --tests`.

## 5. Reviewer checkpoints

The reviewer walks these and records each in `## Checked`:

1. Every active R has a passing test with its token, and the test actually asserts the R's behaviour.
2. No behaviour was added that no R describes (scope creep), and no R was silently reinterpreted.
3. Every task in `tasks.md` is ticked and the diff matches the tasks; nothing undocumented.
4. `log.md` explains the non-obvious decisions and matches the code.
5. Error paths and edge values named in the requirements are handled, not just the happy path.
6. No debug leftovers, no secrets, no TODOs without an owner, no unrelated reformatting.
7. Project conventions from `AGENTS.md` §1 are respected.
8. `uv run harness/harness.py check <slug> --tests` is green on the branch as it stands.

An unchecked box means `CHANGES_REQUESTED`.

## 6. Git and worktree rules

- One branch per feature, named after the slug, created by `start` as a worktree. Never work on the base
  branch except through `approve` and `merge`.
- Commit messages start with the slug: `<slug>: <what>`. Role agents commit their own work before they
  stop; a role that stops with uncommitted changes has not finished.
- Never edit another feature's folder. Never edit `harness/` outside `harness/features/<slug>/`.
- Do not merge or rebase the base branch into the feature branch yourself. The launcher merges it in
  before every review round (so the reviewer sees what will land) and once more at `merge`, and runs the
  tests on the result; a conflict goes back to the implementer with a message.
- Never force-push. Never delete branches or worktrees; `merge` does that.

## 7. What `check` verifies

| # | rule | from status |
|---|---|---|
| 1 | `spec.md` has a frontmatter `status` from the lifecycle table | always |
| 2 | `## Requirements` has at least one active R, ids unique, each active R contains `SHALL`; `## Open questions` empty | `spec_ready` |
| 3 | every task cites at least one existing R; every active R is cited by at least one task | `spec_ready` |
| 4 | every active R has its `<slug>:R<n>` token somewhere outside `harness/` (`git grep --untracked`) | `review` |
| 5 | every task box is ticked and `log.md` exists | `review` |
| 6 | `test_cmd` from `harness/config.json` exits 0 | `review`, or `--tests` |
| 7 | nothing under `harness/` changed outside `harness/features/<slug>/` (branch vs base, plus uncommitted and untracked files) | always, on a feature branch |
| 8 | nothing changed outside `harness/features/<slug>/` | `pending`, `spec_ready`, `approved` |
| 9 | since the reviewer started (`refs/harness/<slug>/review-start`, set by `run reviewer`), nothing changed but `review.md` and the status line of `spec.md` | while a verdict is on disk |
| 10 | an `APPROVED` verdict exists only if the launcher started a reviewer round (the ref above); a verdict the implementer wrote is discarded when it stops | `review` |

`blocked` triggers rules 1 and 7 only. `merge` additionally requires `verdict: APPROVED`. The launcher
runs rules 1–5 and 7–10 when a role stops: a role that leaves them red has not finished (the orchestrator
sees NEEDS_ATTENTION; the driver sends the errors back to the role, three times at most).

## 8. Launcher reference

```
uv run harness/harness.py status                       board across every worktree, grouped: needs you / working / done (anyone)
uv run harness/harness.py start <slug> ["intent"]      worktree + branch + feature folder (orchestrator or human, base branch)
                                   --detach             also start a dedicated orchestrator agent in the worktree
uv run harness/harness.py drive <slug> ["msg"] --tab   run the whole loop in the `driver` tab until a human is needed (orchestrator)
uv run harness/harness.py run <role> <slug> ["msg"]    run one role agent in the feature's worktree (orchestrator, from anywhere)
uv run harness/harness.py wait <role> <slug>           keep waiting for a running role agent (orchestrator)
uv run harness/harness.py approve <slug>               approve a spec; the driver or detached orchestrator continues (human)
uv run harness/harness.py merge <slug>                 merge, mark done, run teardown_cmd, remove the worktree (human, base branch)
uv run harness/harness.py check [slug] [--tests]       rules above (everyone, before stopping)
uv run harness/harness.py doctor                       tools, config, leftover worktrees/branches/workspaces (anyone)
```

`run` and `wait` exit 0 when the role finished (DONE), 3 while it is still working (STILL_WORKING: call
`wait`), 2 when a human or the orchestrator must look (NEEDS_ATTENTION), 1 on error. "Finished" is
decided from the files on disk (status, verdict) and a green structural check, never from the agent's
screen state alone.

`drive <slug> "msg"`: the message goes to the first role the driver runs. At a human gate it goes to
the role that produced what is being judged (feedback on the spec → spec_author; changes before the
merge → implementer); when blocked, to the implementer if the spec was ever approved, else to the
spec_author. `--tab` runs the driver in the feature's workspace and returns at once; when it stops, the
driver prompts the pane it was started from with one line `harness: <slug> ...`. While a driver runs,
`run` refuses to start a role by hand.
