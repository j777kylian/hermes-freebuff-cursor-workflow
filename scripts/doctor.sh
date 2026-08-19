#!/usr/bin/env bash
# Run the workflow doctor. Uses the installed `workflow` CLI if available,
# otherwise falls back to the repo-local venv, otherwise prints setup help.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v workflow >/dev/null 2>&1; then
  exec workflow doctor "$@"
elif [[ -x "$REPO_DIR/.venv/bin/workflow" ]]; then
  exec "$REPO_DIR/.venv/bin/workflow" doctor "$@"
else
  echo "workflow CLI not installed. Run ./scripts/install.sh first." >&2
  exit 1
fi
