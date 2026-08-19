# Workflow agent policy (hermes-freebuff-cursor)

This repository is managed by the deterministic hermes-freebuff-cursor
workflow. Read this file together with `AGENTS.md` (if present) before
starting any task.

## Roles

- **Freebuff** is the primary software-engineering supervisor.
- **Cursor (`agent`)** is the implementation worker.
- **Hermes** is transport/control only (Discord in, reports out). It performs
  no engineering reasoning.
- The deterministic **workflow bridge** (`workflow` CLI) owns task lifecycle,
  state transitions, and validation.

## Source of truth

Agent conversation history is never authoritative. The on-disk files in
`.workflow/` are authoritative:

- `PROJECT_STATE.md` — durable project policy and context
- `CURRENT_TASK.md` — the current task and acceptance criteria
- `CURSOR_REPORT.md` — the implementation worker's report
- `FREEBUFF_REVIEW.md` — the supervisor's independent review
- `FINAL_REPORT.md` — the final report (written only on completion)
- `DECISION_REQUIRED.md` — a pending human decision
- `state.json` — the deterministic lifecycle state machine

## Rules

- Normal software development must not require a Hermes/GPT turn.
- Cursor may inspect/edit/run checks, but may not declare PASS or write the
  final report.
- Do not interpolate untrusted input into shell commands.
- No credentials may ever be committed to this repository.
