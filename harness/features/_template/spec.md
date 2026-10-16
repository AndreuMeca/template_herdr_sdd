---
status: pending
---
# {{title}}

## Intent
{{intent}}

## Requirements
<!-- EARS, one requirement per line, stable ids. Never renumber; mark a dropped one as "- R3: withdrawn".
     Patterns:  THE SYSTEM SHALL <response>.
                WHEN <trigger>, THE SYSTEM SHALL <response>.
                WHILE <state>, THE SYSTEM SHALL <response>.
                WHERE <feature is enabled>, THE SYSTEM SHALL <response>.
                IF <unwanted condition>, THEN THE SYSTEM SHALL <response>. -->
- R1: WHEN <trigger>, THE SYSTEM SHALL <response>.
- R2: IF <unwanted condition>, THEN THE SYSTEM SHALL <response>.

## Design
<!-- Files to touch, interfaces and signatures, data flow, decisions taken, one alternative discarded and why, out of scope. -->

## Verification
<!-- One row per R. Every test that covers R<n> carries the token {{slug}}:R<n> in its name, docstring or a comment. -->
| R  | Test |
|----|------|
| R1 | `tests/test_{{slug}}.py::test_...`  (`{{slug}}:R1`) |
| R2 | `tests/test_{{slug}}.py::test_...`  (`{{slug}}:R2`) |

## Open questions
<!-- Must be empty when status is spec_ready. If anything is listed here, status is blocked. -->
