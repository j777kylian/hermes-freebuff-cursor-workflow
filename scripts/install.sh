#!/usr/bin/env bash
# Install the hermes-freebuff-workflow package into a local venv and expose
# the `workflow` CLI on PATH. Idempotent: safe to re-run.
#
# This script only creates/modifies:
#   - <repo>/.venv/                     (git-ignored)
#   - ~/.local/bin/workflow             (symlink)
# It never installs secrets and never edits shell config files.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
BIN_DIR="$HOME/.local/bin"

info()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Locate a Python >= 3.11 interpreter (detection only, no modification).
PYTHON_BIN="${WORKFLOW_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for cand in python3.11 python3.12 python3.13 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON_BIN="$(command -v "$cand")"
        break
      fi
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  fail "Python >= 3.11 not found. Install it (e.g. 'brew install python@3.11'), then re-run. \
Or set WORKFLOW_PYTHON=/path/to/python3.11."
fi
info "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# 2. Create the venv if missing (modification, but confined to the repo).
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  info "Creating virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# 3. Install the package (editable). No runtime dependencies.
info "Installing package (editable)"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -e "$REPO_DIR"

# 4. Expose `workflow` on PATH via a symlink.
mkdir -p "$BIN_DIR"
LINK="$BIN_DIR/workflow"
if [[ -L "$LINK" ]]; then
  info "Updating symlink $LINK"
  rm -f "$LINK"
elif [[ -e "$LINK" ]]; then
  warn "$LINK exists and is not a symlink; leaving it untouched. \
Remove it manually if it is not the workflow CLI."
else
  info "Linking $LINK -> $VENV_DIR/bin/workflow"
fi
[[ -L "$LINK" ]] || ln -s "$VENV_DIR/bin/workflow" "$LINK"

info "Done. Ensure $BIN_DIR is on your PATH."
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  warn "$BIN_DIR is NOT on PATH. Add it to your shell rc, e.g.:"
  warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
fi

cat <<'EOF'

Next steps:
  1. freebuff login          # once, interactive
  2. agent login             # once, interactive
  3. ./scripts/doctor.sh
  4. ./scripts/install-hermes-plugin.sh
  5. hermes plugins enable hermes-freebuff-workflow
  6. hermes gateway restart
  7. ./scripts/register-project.sh <alias> /path/to/project
EOF
