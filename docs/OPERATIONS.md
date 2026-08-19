# Operations

## Daily commands

```bash
workflow project list                        # registered aliases
workflow project add <alias> <path>          # register
workflow project remove <alias>              # unregister
workflow project init --repo <path>          # install repo-side context files
workflow status <alias-or-task-id>           # current task state
workflow stop <alias-or-task-id>             # stop a task (→ STOPPED)
workflow devdecide <task-id> <decision>      # resolve a pending decision
workflow doctor                              # full diagnostics (incl. Cursor smoke)
workflow doctor --no-smoke                   # fast diagnostics
workflow freebuff status                     # running Freebuff tmux sessions
```

## Discord commands

```
/devtask <alias> <task description>          # start a task (backgrounded)
/devstatus <alias-or-task-id>                # show state
/devdecide <task-id> <decision text>         # answer a decision request
/devstop <alias-or-task-id>                  # stop
```

`/devtask` returns an immediate acknowledgement and runs the task detached.

## State machine reference

| State | Meaning | Next |
|---|---|---|
| `IDLE` | no task | `TASK_READY` |
| `TASK_READY` | task written | `FREEBUFF_STARTING` |
| `FREEBUFF_STARTING` | process may exist; picker/loading; prompt not yet accepted | `FREEBUFF_RUNNING` (ack), `FAILED`, `STOPPED` |
| `FREEBUFF_RUNNING` | supervisor durably acknowledged the task | `CURSOR_RUNNING`, `DECISION_REQUIRED`, `FAILED`, `STOPPED` |
| `CURSOR_RUNNING` | worker running | `REVIEWING` |
| `REVIEWING` | supervisor reviewing | `PASS`, `REPAIR_REQUIRED`, `BLOCKED`, `DECISION_REQUIRED`, `FAILED` |
| `REPAIR_REQUIRED` | repair needed | `CURSOR_RUNNING` |
| `DECISION_REQUIRED` | awaiting human | `FREEBUFF_STARTING` (on `/devdecide`) |
| `PASS` / `BLOCKED` / `STOPPED` / `FAILED` | terminal | — |

`FREEBUFF_RUNNING` is reached only after Freebuff's first workflow action —
`workflow state supervisor-started --repo <repo> --task-id <task-id>` — writes
the durable acknowledgement. `status` reconciles a stale `FREEBUFF_RUNNING`/
`FREEBUFF_STARTING` against the live tmux session before reporting.

Transitions are validated in `workflow/state.py`. A per-tree `fcntl` lock
prevents two tasks on the same working tree.

## The repair loop

Freebuff reviews, and on failure calls `workflow cursor repair` with precise
instructions. Each repair increments a counter; the default maximum is 3
(configurable via `[workflow] max_repair_iterations`). Exceeding the maximum
transitions to `BLOCKED`.

## Reporting

On a terminal state the bridge:

1. snapshots the Freebuff pane into a log,
2. stops the tmux session,
3. archives `.workflow/*` into `runtime/archive/<task-id>/`,
4. sends a ≤1800-char Discord summary via `hermes send`.

The full `FINAL_REPORT.md` stays on disk in the managed repo.

## Decision flow

1. Freebuff writes `DECISION_REQUIRED.md` and sets `DECISION_REQUIRED`.
2. The bridge sends a concise Discord decision request (`hermes send`).
3. User answers `/devdecide <task-id> <decision>`.
4. The handler writes `.workflow/decision.json`, appends to
   `DECISION_REQUIRED.md`, transitions the state to `FREEBUFF_STARTING`, and
   injects a self-contained resume prompt into the running Freebuff session
   (or starts a fresh resume session if the old one is gone). The resume
   prompt's first action is the same durable `supervisor-started`
   acknowledgement, which moves the state back to `FREEBUFF_RUNNING`.

## Cleanup

Terminal task cleanup is automatic: when a task reaches `PASS`, `BLOCKED`,
`STOPPED`, or `FAILED` (including startup/prompt failure), the bridge stops
the Freebuff tmux session and removes the project lock file, so a new task
against the same repo is immediately allowed.

For a stuck tmux session or a lock left behind by a crashed bridge process:

```bash
workflow freebuff status     # list sessions
tmux kill-session -t hfw-freebuff-<name>   # manual cleanup of a stuck session
workflow doctor --repair     # remove proven-stale lock/PID files
```

`workflow doctor` is read-only by default. It reports stale lock/PID files
only when their recorded owner PID is dead or was reused by a different
process — a lock held by a live workflow owner is reported as active.
`workflow doctor --repair` removes those proven-stale artifacts and never
touches a lock owned by a live process.
