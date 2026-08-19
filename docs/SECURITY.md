# Security

## Credential boundary

This repository must never contain credentials. Credentials live only in the
existing stores:

| System | Store |
|---|---|
| Hermes | `~/.hermes/` |
| Freebuff | `~/.config/manicode/` |
| Cursor | Cursor CLI's own auth store |

The committed `config/workflow.example.toml` contains only non-secret values.
The doctor never prints credentials — it reads at most the Freebuff selected
**model name** (not the credential file).

## Command injection resistance

- All external commands use `subprocess` **argument arrays** with
  `shell=False`. Nothing is ever interpolated into a shell string.
- Discord input (`/devtask`, `/devdecide`) is passed as a single argv element
  to the workflow CLI; it is never concatenated into a command.
- Project aliases are validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
  and are registry keys only — never used to build filesystem paths.
- Registered paths are canonicalized (`resolve()`) and must exist.

## Least privilege / blast radius

- `workflow cursor run` runs `agent -p --force --trust --model auto` inside
  the managed repo. `--force` means Cursor may run commands unless explicitly
  denied. See "Cursor `--force`" below. Only point this at repositories you
  trust, in an environment you control.
- The bridge only writes inside the managed repo's `.workflow/` and the
  per-user `~/.config/hermes-freebuff-workflow/` runtime dir.

## Cursor `--force` / `--trust`

- `--trust` skips the workspace-trust prompt (required for headless use).
- `--force` allows Cursor to run shell commands without per-command approval.
  It is the default because the loop is unattended, but it is configurable:
  set `[cursor] force = false` to require approval (which will block headless
  automation). Do not lower this to "false" and expect unattended operation.

## Freebuff single-instance

Freebuff enforces a single instance per machine/account. The bridge detects
the "already running" condition and fails cleanly instead of taking over a
user's interactive session. See `docs/TROUBLESHOOTING.md`.

## Locking and races

- A per-tree `fcntl` lock (`runtime/locks/<hash>.lock`) prevents two
  `/devtask` commands from running against the same tree.
- `state.json` writes are atomic (`os.replace`) and guarded by a per-repo
  transition lock, so the monitor, `/devdecide`, and `/devstop` cannot race a
  transition.

## Git hygiene

`git status --short`, `git diff`, and `git diff --cached` are part of the
pre-commit check. The `.gitignore` excludes `.venv/`, logs, pid/lock files,
machine-local config, and secrets.
