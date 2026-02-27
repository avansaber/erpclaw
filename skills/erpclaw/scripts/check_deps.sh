#!/bin/bash
# ERPClaw pre-install dependency check.
# Verifies python3 (3.10+) and git are available.
# Outputs JSON for OpenClaw to relay to the user.
set -euo pipefail

MISSING=""
VERSIONS=""

# Python 3.10+
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
        MISSING="$MISSING python3>=3.10(found:$PY_VER)"
    else
        VERSIONS="$VERSIONS python3 $PY_VER,"
    fi
else
    MISSING="$MISSING python3"
fi

# git (needed for v2 GitHub clone support)
if command -v git &>/dev/null; then
    GIT_VER=$(git --version | sed 's/git version //')
    VERSIONS="$VERSIONS git $GIT_VER"
else
    MISSING="$MISSING git"
fi

if [ -n "$MISSING" ]; then
    echo "{\"status\":\"error\",\"message\":\"Missing dependencies:$MISSING. Install with: sudo apt install$MISSING\"}"
    exit 1
fi

echo "{\"status\":\"ok\",\"message\":\"All dependencies found: $VERSIONS\"}"
