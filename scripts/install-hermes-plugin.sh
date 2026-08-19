#!/usr/bin/env bash
# Install the versioned Hermes plugin into the user plugin directory.
# Copies only this repository's plugin source; never patches Hermes core.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/integrations/hermes-plugin"
DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-freebuff-workflow"

info() { printf '\033[1;34m[plugin]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[plugin]\033[0m %s\n' "$*" >&2; exit 1; }

command -v hermes >/dev/null 2>&1 || fail "hermes CLI not found on PATH"

mkdir -p "$DEST"
# Idempotent copy of the three plugin files.
cp -f "$SRC/plugin.yaml" "$SRC/__init__.py" "$SRC/commands.py" "$DEST/"

info "Copied plugin to $DEST"
info "Validating with 'hermes plugins doctor'..."
hermes plugins doctor "$DEST" --ci

cat <<'EOF'

Plugin installed. To activate:
  hermes plugins enable hermes-freebuff-workflow
  hermes gateway restart

Then use in Discord:
  /devtask <alias> <task>
  /devstatus <alias>
  /devdecide <task-id> <decision>
  /devstop <alias>
EOF
