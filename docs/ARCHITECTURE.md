# Architecture

## Overview

The system is a deterministic pipeline. Every step that does not require
engineering judgment is plain Python; the only LLM turns are the Freebuff
supervisor and the Cursor implementation worker.

```
Discord slash command
        │  (Hermes plugin: ctx.register_command, deterministic handler)
        ▼
workflow devtask <alias> <task>          # spawned detached, returns ack
        │
        ▼
Bridge._run()
  ├─ validate alias → registered path
  ├─ acquire per-tree lock (fcntl)
  ├─ write .workflow/CURRENT_TASK.md, reset state → TASK_READY
  ├─ render prompts/freebuff_supervisor.md
  ├─ start Freebuff in tmux:  freebuff --cwd <repo>
  ├─ state → FREEBUFF_STARTING
  ├─ classify startup screen; select configured model (no premium fallback)
  ├─ wait for the true READY composer, then inject supervisor prompt
  ├─ wait for durable ack:  workflow state supervisor-started --task-id <id>
  ├─ state → FREEBUFF_RUNNING (only after the ack is on disk)
  └─ monitor state.json until a terminal state
        │
        ▼
Freebuff (supervisor, in TUI)
  ├─ read AGENTS.workflow.md / AGENTS.md / .workflow/*
  ├─ git status/diff inspection
  ├─ decompose → .workflow/PLAN.md
  ├─ run:  workflow cursor run --repo <repo> --prompt-file ...
  │         └─ CursorRunner: agent -p --trust --force --model auto ...
  ├─ independently review (git status/diff, rerun tests)
  ├─ repair loop via: workflow cursor repair --repo <repo> ...
  ├─ decision:  workflow report decision --repo <repo> --question ...
  └─ finish:    workflow report final ... && workflow state set ... PASS
        │
        ▼
Bridge monitor
  ├─ terminal state → snapshot, stop session, archive, hermes send report
  └─ DECISION_REQUIRED → hermes send decision request, wait for /devdecide
```

## Deterministic boundary

The bridge never reasons about code. Freebuff is told (via
`prompts/freebuff_supervisor.md`) to drive the loop through a small set of
deterministic CLI verbs. Each verb validates the state machine and writes
durable evidence, so correctness never depends on Freebuff remembering
anything:

- `workflow cursor run` — CURSOR_RUNNING → run agent → CURSOR_REPORT.md → REVIEWING
- `workflow cursor repair` — increment counter, enforce max, then run agent
- `workflow state set <repo> <STATE>` — validated transition
- `workflow report final` — write FINAL_REPORT.md
- `workflow report decision` — write DECISION_REQUIRED.md + state

## State machine

```
IDLE → TASK_READY → FREEBUFF_STARTING → FREEBUFF_RUNNING → CURSOR_RUNNING → REVIEWING
                                                                        ├─ REPAIR_REQUIRED → CURSOR_RUNNING
                                                                        ├─ PASS
                                                                        ├─ BLOCKED
                                                                        ├─ DECISION_REQUIRED → FREEBUFF_STARTING → (ack) → FREEBUFF_RUNNING
                                                                        └─ FAILED
any non-terminal → STOPPED
```

`FREEBUFF_STARTING` means the tmux/Freebuff process may exist and the model
picker may still be visible; the supervisor prompt has not yet been accepted.
`FREEBUFF_RUNNING` is reached ONLY via the durable
`workflow state supervisor-started --task-id <id>` acknowledgement.

Terminal states: `PASS`, `BLOCKED`, `STOPPED`, `FAILED`. `DECISION_REQUIRED`
is *not* terminal; it resumes via `/devdecide`. A user-requested `STOPPED` is
recorded in `terminal_reason`, never `last_error`.

## Source of truth

`.workflow/state.json` in each managed repo is authoritative. TUI capture and
chat history are diagnostic only. Context files separate concerns:

- `AGENTS.workflow.md` — durable policy
- `.workflow/PROJECT_STATE.md` — durable project context
- `.workflow/CURRENT_TASK.md` — current task + acceptance criteria
- `.workflow/CURSOR_REPORT.md` — worker evidence
- `.workflow/FREEBUFF_REVIEW.md` — supervisor evidence
- `.workflow/FINAL_REPORT.md` — completion report
- `.workflow/DECISION_REQUIRED.md` — pending decision
- `.workflow/state.json` — runtime state machine

Task archives go to `~/.config/hermes-freebuff-workflow/runtime/archive/`
(outside Git, safe by default).

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | TOML config (tomllib), defaults, config/runtime dirs |
| `state.py` | state machine, validated transitions, per-tree lock, atomic writes |
| `projects.py` | alias registry, path validation, repo-side template install |
| `tmux.py` | tmux session/paste helpers (robust buffer paste) |
| `freebuff.py` | Freebuff tmux controller (start/ready/inject/stop/conflict) |
| `cursor.py` | Cursor `agent` wrapper (argv, capture, smoke test) |
| `hermes.py` | `hermes send` wrapper + target detection |
| `reports.py` | report rendering + Discord summarization |
| `runner.py` | deterministic verbs used by Freebuff |
| `registry.py` | durable task_id → project lookup |
| `bridge.py` | orchestrator (task lifecycle, monitor, decide, stop) |
| `cli.py` | argparse entry point (`workflow`) |
| `doctor.py` | host/workflow diagnostics |

## Security posture

- No shell interpolation anywhere: `subprocess.run([...], shell=False)`.
- Discord input is never concatenated into a command string.
- No credentials are stored in this repo (see `docs/SECURITY.md`).
- A per-tree `fcntl` lock prevents concurrent tasks on one working tree.
