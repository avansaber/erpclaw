#!/bin/bash
# ERPClaw meta-package post-install script.
# Detects installed sibling skills, installs the shared library,
# initializes the database, and reports installation status.
#
# No sudo required. No network access. Pure Python + SQLite.
#
# Run by OpenClaw after copying skill files to ~/clawd/skills/erpclaw/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
USER_HOME=$(eval echo "~$CURRENT_USER")
DB_DIR="$USER_HOME/.openclaw/erpclaw"
DB_PATH="$DB_DIR/data.sqlite"
LIB_TARGET="$DB_DIR/lib/erpclaw_lib"

log() { echo "[erpclaw] $1"; }
err() { echo "{\"status\":\"error\",\"message\":\"$1\"}"; exit 1; }

# ── Phase 0: Detect installed skills ──────────────────────────────────────

log "Phase 0: Detecting installed ERPClaw skills..."

FOUND=0
MISSING=""
SETUP_FOUND=false

ALL_SKILLS=(
    erpclaw-setup erpclaw-gl erpclaw-journals erpclaw-payments erpclaw-tax
    erpclaw-reports erpclaw-inventory erpclaw-selling erpclaw-buying
    erpclaw-manufacturing erpclaw-hr erpclaw-payroll erpclaw-projects
    erpclaw-assets erpclaw-quality erpclaw-crm erpclaw-support erpclaw-billing
    erpclaw-ai-engine erpclaw-analytics erpclaw-region-ca erpclaw-region-eu
    erpclaw-region-in erpclaw-region-uk erpclaw-integrations webclaw
)

for skill in "${ALL_SKILLS[@]}"; do
    if [ -f "$SKILLS_DIR/$skill/SKILL.md" ]; then
        FOUND=$((FOUND + 1))
        if [ "$skill" = "erpclaw-setup" ]; then
            SETUP_FOUND=true
        fi
    else
        MISSING="$MISSING $skill"
    fi
done

log "Found $FOUND/29 skills in $SKILLS_DIR"

if [ "$SETUP_FOUND" = false ]; then
    err "erpclaw-setup not found in $SKILLS_DIR. Install it first: clawhub install erpclaw-setup"
fi

# ── Phase 1: Install shared library ──────────────────────────────────────

log "Phase 1: Installing shared library..."

SETUP_LIB="$SKILLS_DIR/erpclaw-setup/lib/erpclaw_lib"

if [ ! -d "$SETUP_LIB" ]; then
    err "Shared library not found at $SETUP_LIB. Re-install erpclaw-setup."
fi

mkdir -p "$LIB_TARGET"

LIB_COPIED=0
for pyfile in "$SETUP_LIB"/*.py; do
    [ -f "$pyfile" ] || continue
    fname=$(basename "$pyfile")
    # Copy if target missing or source is newer
    if [ ! -f "$LIB_TARGET/$fname" ] || [ "$pyfile" -nt "$LIB_TARGET/$fname" ]; then
        cp "$pyfile" "$LIB_TARGET/$fname"
        LIB_COPIED=$((LIB_COPIED + 1))
    fi
done

LIB_TOTAL=$(ls -1 "$LIB_TARGET"/*.py 2>/dev/null | wc -l | tr -d ' ')
log "Shared library: $LIB_COPIED files updated, $LIB_TOTAL total at $LIB_TARGET"

# ── Phase 2: Initialize database ─────────────────────────────────────────

log "Phase 2: Initializing database..."

mkdir -p "$DB_DIR"

# Run erpclaw-setup's initialize-database action (creates 173 tables, 264 indexes)
SETUP_SCRIPT="$SKILLS_DIR/erpclaw-setup/scripts/db_query.py"

if [ ! -f "$SETUP_SCRIPT" ]; then
    err "erpclaw-setup db_query.py not found at $SETUP_SCRIPT"
fi

INIT_OUTPUT=$(python3 "$SETUP_SCRIPT" --action initialize-database --db-path "$DB_PATH" 2>&1) || {
    log "Database initialization output: $INIT_OUTPUT"
    err "Database initialization failed. Check erpclaw-setup installation."
}

# Parse table/index counts from JSON output (init_db prints text before JSON)
read TABLE_COUNT INDEX_COUNT <<< $(echo "$INIT_OUTPUT" | python3 -c "
import sys, json
text = sys.stdin.read()
# Find the JSON block: starts at first '{' on its own line
start = text.rfind('{')
if start >= 0:
    d = json.loads(text[start:])
    print(d.get('tables', '?'), d.get('indexes', '?'))
else:
    print('? ?')
" 2>/dev/null || echo "? ?")

log "Database ready: $TABLE_COUNT tables, $INDEX_COUNT indexes at $DB_PATH"

# Fix ownership if run via sudo
if [ "$CURRENT_USER" != "$(whoami)" ]; then
    chown -R "$CURRENT_USER:$CURRENT_USER" "$DB_DIR" 2>/dev/null || true
fi

# ── Phase 3: Report ──────────────────────────────────────────────────────

log "Phase 3: Verifying installation..."

CHECK_OUTPUT=$(python3 "$INSTALL_DIR/scripts/db_query.py" --action check-installation --db-path "$DB_PATH" 2>&1) || true

# Build next-steps guidance
NEXT_STEPS="ERPClaw is ready!"
if [ "$FOUND" -eq 29 ]; then
    NEXT_STEPS="$NEXT_STEPS All 29 skills installed."
else
    NEXT_STEPS="$NEXT_STEPS $FOUND/29 skills found. Missing:$MISSING"
fi

# Check if a company exists
HAS_COMPANY=$(python3 -c "
import sqlite3, os
db = '$DB_PATH'
if not os.path.exists(db):
    print('no')
else:
    conn = sqlite3.connect(db)
    c = conn.execute('SELECT COUNT(*) FROM company').fetchone()[0]
    conn.close()
    print('yes' if c > 0 else 'no')
" 2>/dev/null || echo "no")

if [ "$HAS_COMPANY" = "no" ]; then
    NEXT_STEPS="$NEXT_STEPS\n\nNext: Set up your company. Say 'get started' in Telegram, or install webclaw for a browser UI."
else
    NEXT_STEPS="$NEXT_STEPS\n\nCompany already configured. You're ready to go!"
fi

cat <<EOF
{"status":"ok","message":"$NEXT_STEPS","skills_found":$FOUND,"tables":$TABLE_COUNT,"indexes":$INDEX_COUNT,"shared_lib_files":$LIB_TOTAL}
EOF
