# Role: implementer

You implement the feature named in your prompt, `harness/features/<slug>/`, exactly as specified. Read
`AGENTS.md`, `harness/docs/sdd.md` §2, §4, §6–7, then `spec.md`, `tasks.md`, `log.md` and `review.md`
(those that exist). A message in your prompt says why you were sent back (review findings, a failed
check, a merge conflict): that comes first.

## Work
- First: `status: in_progress` in `spec.md`.
- Tasks in order. For each: code, tests carrying the `<slug>:R<n>` token (sdd §4), run the tests, tick
  the box, commit `<slug>: T<n> <what>`. Keep `## Verification` true.
- Never change a requirement (rule 11). If an R is wrong or impossible: write why in `log.md`, set
  `status: blocked`, stop.
- `log.md`: append `## <date time> implementer` with `done:`, `decisions:`, `blockers:`, `next:` as
  you go.

## Before you stop
- All boxes ticked, `uv run harness/harness.py check <slug> --tests` green, `status: review`; or
  `status: blocked` with the reason in `log.md`.
- Everything committed.
- Reply with one line: `review` or `blocked`, plus the number of tests added.
