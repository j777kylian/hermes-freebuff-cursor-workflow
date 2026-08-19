# Troubleshooting

## "another Freebuff instance is already running"

Freebuff allows only one instance per machine/account. A workflow task will
fail with this message if an interactive `freebuff` session is open.

Fix:
```bash
# find it
ps aux | grep freebuff | grep -v grep
# close the interactive session (Ctrl-C in its terminal), then retry
```

The bridge deliberately refuses to "take over" another session.

## `/devtask` returns "unknown alias"

```bash
workflow project list
./scripts/register-project.sh <alias> /path/to/project
```

Paths must exist and be directories.

## "task already in progress (state=...)"

A previous task did not reach a terminal state, or a stale session is
lingering.

```bash
workflow status <alias>
workflow stop <alias>           # force STOPPED
workflow freebuff status        # list lingering tmux sessions
tmux kill-session -t hfw-freebuff-<name>
```

## "illegal state transition"

A deterministic verb was invoked out of order (e.g. `workflow cursor run`
while already `CURSOR_RUNNING`). Inspect the authoritative state:

```bash
cat <repo>/.workflow/state.json
```

## Discord commands not appearing

The plugin must be installed *and* enabled, and the gateway restarted:

```bash
./scripts/install-hermes-plugin.sh
hermes plugins enable hermes-freebuff-workflow
hermes gateway restart
hermes plugins show hermes-freebuff-workflow     # expect "Status: enabled"
```

## Doctor reports

Run `workflow doctor` (or `--no-smoke` to skip the Cursor smoke test). Common
FAILs:

- **Python >= 3.11** — `brew install python@3.11` and re-run install.sh.
- **Cursor not authenticated** — `agent login`.
- **Freebuff auth** — `freebuff login`.
- **Hermes Discord target** — verify with `hermes send --list discord` and set
  `[hermes] discord_target` in `~/.config/hermes-freebuff-workflow/config.toml`.

## "workflow: command not found"

```bash
./scripts/install.sh
# ensure ~/.local/bin is on PATH:
export PATH="$HOME/.local/bin:$PATH"
```

## Freebuff session died mid-task ("Freebuff session exited unexpectedly")

This means the supervisor exited before reaching a terminal state. Check the
session log:

```bash
ls -t ~/.config/hermes-freebuff-workflow/runtime/logs/ | head
```

and the archived evidence under `runtime/archive/<task-id>/`.

## Cursor "Workspace Trust Required"

This means `--trust` was not passed. Verify `[cursor] trust = true` in config
(or that the wrapper passes `--trust`). The default wrapper always passes it.
