# Cursor Implementation Worker — Repair Prompt

A previous implementation was reviewed and FAILED. Repair it precisely.

## Original task

{{TASK}}

## Repair instructions (from the supervisor review)

{{REPAIR}}

## Rules

- Work only inside the repository: `{{REPO}}`
- Address the repair instructions exactly. Do not make unrelated changes.
- Re-run the tests/typecheck that failed, plus any tests your change touches.
- When finished, update `{{REPO}}/.workflow/CURSOR_REPORT.md` with what you
  changed and the new verification results.

You may NOT declare a final PASS or write FINAL_REPORT.md.
