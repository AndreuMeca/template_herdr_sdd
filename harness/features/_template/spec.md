---
status: pending
---
# {{title}}

## Intent
{{intent}}

## Requirements
<!-- EARS lines per harness/docs/sdd.md §3; stable ids, never renumber. -->
- R1: WHEN <trigger>, THE SYSTEM SHALL <response>.
- R2: IF <unwanted condition>, THEN THE SYSTEM SHALL <response>.

## Design
<!-- Files to touch, interfaces, data flow, decisions, one discarded alternative, out of scope (sdd §2). -->

## Verification
<!-- One row per R; the test carries the token {{slug}}:R<n> (sdd §4). -->
| R  | Test |
|----|------|
| R1 | `tests/test_{{slug}}.py::test_...`  (`{{slug}}:R1`) |
| R2 | `tests/test_{{slug}}.py::test_...`  (`{{slug}}:R2`) |

## Open questions
<!-- Empty at spec_ready; anything here means status blocked. -->
