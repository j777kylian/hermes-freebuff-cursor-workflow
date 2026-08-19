# hermes-freebuff-cursor-workflow

A portable, Git-versioned workflow that turns a Discord slash command into a
supervised software-engineering task:

```
Discord
  └─ /devtask <alias> <task>          (Hermes plugin, deterministic — no LLM)
       └─ workflow bridge             (plain Python, no LLM)
            └─ Freebuff               (primary engineering supervisor)
                 └─ Cursor `agent`    (implementation worker, --model auto)
                      └─ Freebuff review → repair / decision / PASS
                           └─ hermes send → Discord report
```

The overriding goal: **normal software development must not require a
Hermes/GPT turn.** Hermes is transport/control only; Freebuff is the
supervisor; Cursor is the worker; the bridge is deterministic Python.

## Components

| Piece | Role | LLM? |
|---|---|---|
| Hermes plugin (`integrations/hermes-plugin/`) | `/devtask`, `/devstatus`, `/devdecide`, `/devstop` slash commands | No |
| `workflow` bridge (`src/workflow/`) | task lifecycle, state machine, locking, reporting | No |
| Freebuff (`src/workflow/freebuff.py`) | supervisor, driven through tmux | Yes (supervisor) |
| Cursor (`src/workflow/cursor.py`) | deterministic `agent` wrapper | Yes (worker) |
| `hermes send` (`src/workflow/hermes.py`) | report/decision delivery | No |

## Quick start

```bash
git clone <this-repo> && cd hermes-freebuff-cursor-workflow
./scripts/install.sh
freebuff login        # once
agent login           # once
./scripts/doctor.sh
./scripts/install-hermes-plugin.sh
hermes plugins enable hermes-freebuff-workflow
hermes gateway restart
./scripts/register-project.sh demo /path/to/demo-project
```

Then, in Discord:

```
/devtask demo Fix the failing parser tests
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Install](docs/INSTALL.md)
- [Reproduce from a new Mac](docs/REPRODUCE.md)
- [Operations](docs/OPERATIONS.md)
- [Security](docs/SECURITY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## License

MIT. See [LICENSE](LICENSE).
