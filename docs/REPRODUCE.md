# Reproduce from a new Mac

This is the exact procedure verified on macOS (Apple Silicon) on 2026-08-18.
It does not depend on this chat conversation.

## 0. Prerequisites installed once

```bash
# Homebrew (if not present): https://brew.sh
brew install python@3.11 git tmux node

# Freebuff CLI (Node)
npm install -g freebuff

# Cursor CLI ("agent") — install from Cursor, ensure `agent` is on PATH
agent --version

# Hermes — install per Hermes docs, ensure `hermes` is on PATH
hermes --version
```

## 1. Clone and install

```bash
git clone <this-repo>
cd hermes-freebuff-cursor-workflow
./scripts/install.sh
```

## 2. Log in (interactive, once)

```bash
freebuff login
agent login
```

`freebuff login` stores credentials under `~/.config/manicode/`.
`agent login` stores credentials in the Cursor CLI's own store.

## 3. Verify with the doctor

```bash
./scripts/doctor.sh
```

Expected: every line `PASS` (a `WARN` for "no projects registered" is fine
before step 5). The doctor runs a bounded headless Cursor `auto` smoke test.

## 4. Install and enable the Hermes plugin

```bash
./scripts/install-hermes-plugin.sh
hermes plugins enable hermes-freebuff-workflow
hermes gateway restart
```

## 5. Register a project

```bash
./scripts/register-project.sh demo /path/to/demo-project
```

## 6. Run a task from Discord

```
/devtask demo Fix the failing parser tests
/devstatus demo
/devdecide <task-id> <decision>     # only when a decision is requested
/devstop demo
```

## 7. Run a task without Discord (E2E / headless)

```bash
cd /path/to/demo-project
workflow project init --repo .
# put the task into .workflow/CURRENT_TASK.md
workflow start --repo .
```

`workflow start` runs the full Freebuff → Cursor → review → report loop.

## Important constraint: Freebuff is single-instance

Freebuff allows **one** instance per machine/account. A task will not start
while an interactive `freebuff` session is open. Close any open Freebuff
session (or run `workflow` from a context with no Freebuff running) before
starting a task. The bridge detects the conflict and fails with a clear
message rather than hanging.

## Notes

- All runtime state is outside Git: `~/.config/hermes-freebuff-workflow/`.
- Project aliases map to paths in `~/.config/hermes-freebuff-workflow/projects.json`.
- Task archives: `~/.config/hermes-freebuff-workflow/runtime/archive/`.
