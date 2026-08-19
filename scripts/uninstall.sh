#!/usr/bin/env bash
# Remove the workflow CLI symlink and the installed Hermes plugin.
# Does not remove ~/.config/hermes-freebuff-workflow (project registry/runtime)
# or the repo venv by default; pass --purge to also remove those.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_LINK="$HOME/.local/bin/workflow"
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-freebuff-workflow"

info() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }

[[ -L "$BIN_LINK" ]] && { info "Removing $BIN_LINK"; rm -f "$BIN_LINK"; } || true
[[ -d "$PLUGIN_DIR" ]] && { info "Removing $PLUGIN_DIR"; rm -rf "$PLUGIN_DIR"; } || true
info "Removed CLI symlink and Hermes plugin."

if [[ "${1:-}" == "--purge" ]]; then
  [[ -d "$REPO_DIR/.venv" ]] && { info "Removing $REPO_DIR/.venv"; rm -rf "$REPO_DIR/.venv"; } || true
  CFG="$HOME/.config/hermes-freebuff-workflow"
  [[ -d "$CFG" ]] && { info "Removing $CFG"; rm -rf "$CFG"; } || true
  info "Purged venv and workflow config/runtime."
else
  info "Kept $REPO_DIR/.venv and ~/.config/hermes-freebuff-workflow. \
Use --purge to also remove them."
fi
