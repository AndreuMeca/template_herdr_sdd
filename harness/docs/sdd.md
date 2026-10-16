# Spec-driven development rules

This is the only rules file. `AGENTS.md` is the map, `harness/roles/*.md` tell each agent its job, and
`harness/harness.py check` enforces what a script can enforce. When a role file and this file disagree,
this file wins.

## 1. Lifecycle

One feature = one slug = one folder `harness/features/<slug>/` = one branch = one worktree = one
orchestrator agent. Role agents (spec_author, implementer, reviewer) run one at a time inside that
worktree, so there is never more than one writer in a checkout.

| status | set by | means | next |
|---|---|---|---|
| `pending` | `start` | folder exists, intent written | `run spec_author` |
| `spec_ready` | spec_author | requirements, design, tasks written; `check` green | human reads the spec → `approve` |
| `approved` | `approve` (human) | spec frozen; implementation may start | `run implementer` |
| `in_progress` | implementer at start; reviewer on CHANGES_REQUESTED | code and tests being written | `run implementer` |
| `review` | implementer | every task ticked, `check --tests` green | `run reviewer`; with verdict `APPROVED` → `merge` |
| `done` | `merge` (human) | merged into the base branch, worktree removed | — |
| `blocked` | spec_author or implementer | needs a human decision (`## Open questions` or `log.md`) | human answers; re-run the role with the answer as message |

The only human gates are `approve` and `merge`. The only commands that write to the base branch are
`approve` (a status flip on the feature branch, actually) and `merge`. Everything else happens on the
feature branch.

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
8. `python3 harness/harness.py check <slug> --tests` is green on the branch as it stands.

An unchecked box means `CHANGES_REQUESTED`.

## 6. Git and worktree rules

- One branch per feature, named after the slug, created by `start` as a worktree. Never work on the base
  branch except through `approve` and `merge`.
- Commit messages start with the slug: `<slug>: <what>`. Role agents commit their own work before they
  stop; a role that stops with uncommitted changes has not finished.
- Never edit another feature's folder. Never edit `harness/` outside `harness/features/<slug>/`.
- Do not merge or rebase the base branch into the feature branch unless the human asks (via a `run`
  message). `merge` does it once, right before merging, and runs the tests on the result.
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

`blocked` only triggers rule 1. `merge` additionally requires `verdict: APPROVED`.

## 8. Launcher reference

```
python3 harness/harness.py status                       board across every worktree (anyone)
python3 harness/harness.py start <slug> ["intent"]      worktree + feature + orchestrator (human, base branch)
python3 harness/harness.py run <role> <slug> ["msg"]    run a role agent here (orchestrator, inside its worktree)
python3 harness/harness.py wait <role> <slug>           keep waiting for a running role agent (orchestrator)
python3 harness/harness.py approve <slug>               approve a spec, notify the orchestrator (human)
python3 harness/harness.py merge <slug>                 merge, mark done, remove the worktree (human, base branch)
python3 harness/harness.py check [slug] [--tests]       rules above (everyone, before stopping)
```

`run` and `wait` exit 0 when the role finished (DONE), 3 while it is still working (STILL_WORKING: call
`wait`), 2 when a human or the orchestrator must look (NEEDS_ATTENTION), 1 on error. "Finished" is
decided from the files on disk (status, verdict), never from the agent's screen state alone.
