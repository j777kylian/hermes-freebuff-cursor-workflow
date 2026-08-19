# Install

## Host requirements

The doctor checks all of these. Required:

- **Python >= 3.11** (stdlib only at runtime; `tomllib` is used for config)
- **Git**
- **tmux** (`tmux -V`)
- **Freebuff** CLI (`freebuff --version`) — installed and authenticated
- **Cursor CLI** (`agent --version`, `agent status`, `agent models`) — authenticated
- **Hermes** CLI (`hermes --version`, `hermes doctor`) — configured

Optional (dev/test only): pytest, ruff, mypy.

## Steps

```bash
# 1. Clone
git clone <this-repo>
cd hermes-freebuff-cursor-workflow

# 2. Install the workflow package + CLI
./scripts/install.sh
#   - creates .venv/ (git-ignored)
#   - pip install -e .
#   - symlinks ~/.local/bin/workflow -> .venv/bin/workflow
#   - never edits shell config or installs secrets

# 3. Authenticate external tools (interactive, once each)
freebuff login
agent login

# 4. Verify everything
./scripts/doctor.sh

# 5. Install + enable the Hermes plugin
./scripts/install-hermes-plugin.sh
hermes plugins enable hermes-freebuff-workflow
hermes gateway restart

# 6. Register a project alias
./scripts/register-project.sh demo /path/to/demo-project
```

## What install.sh does (and does not do)

Does:
- Detect Python >= 3.11 (tries `python3.11`, `python3.12`, `python3.13`, `python3`)
- Create `.venv/` inside the repo (idempotent)
- `pip install -e .` (no runtime dependencies)
- Symlink `~/.local/bin/workflow`

Does not:
- Install Homebrew packages (prints instructions instead)
- Edit shell config files (prints a `PATH` hint instead)
- Install or copy credentials

## Configuration

Copy the example to the live (non-secret) location:

```bash
cp config/workflow.example.toml ~/.config/hermes-freebuff-workflow/config.toml
```

All fields are non-secret. Credentials live in the existing stores and are
never copied here.
