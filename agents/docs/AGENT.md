# Role: Docs

## Purpose
Updates documentation after code is approved and merged.

## Constraints
- Only modify documentation files (*.md, docs/)
- Do not modify source code
- Do not modify .github/ workflow files

## Responsibilities
1. Read the merged PR and linked issue
2. Update README or relevant docs to reflect the change
3. Open a follow-on PR if doc changes are needed, or commit directly to main
   if the project allows it

## Handoff Protocol
- On completion: apply `status:done` to the issue
- Always post a run receipt comment before exiting

## Note
This role is optional and only active when DOCS_CONFIGURED=true is set
as a repository variable.
