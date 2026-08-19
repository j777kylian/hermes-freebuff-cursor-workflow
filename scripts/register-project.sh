#!/usr/bin/env bash
# Register a project alias -> path. Usage: register-project.sh <alias> <path>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <alias> <path>" >&2
  exit 2
fi

ALIAS="$1"
PROJECT_PATH="$2"

if [[ ! -d "$PROJECT_PATH" ]]; then
  echo "error: path does not exist or is not a directory: $PROJECT_PATH" >&2
  exit 1
fi

if command -v workflow >/dev/null 2>&1; then
  exec workflow project add "$ALIAS" "$PROJECT_PATH"
elif [[ -x "$REPO_DIR/.venv/bin/workflow" ]]; then
  exec "$REPO_DIR/.venv/bin/workflow" project add "$ALIAS" "$PROJECT_PATH"
else
  echo "error: workflow CLI not installed. Run ./scripts/install.sh first." >&2
  exit 1
fi
