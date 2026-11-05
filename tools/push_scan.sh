#!/usr/bin/env bash
# Push gate: scan every commit a `git push <remote> <branch>` would send.
# Usage: tools/push_scan.sh [--policy <file.json>] <remote-name> <local-branch>
# Run it from inside the repository being pushed. Exit codes: see push_scan.py.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/push_scan.py" "$@"
