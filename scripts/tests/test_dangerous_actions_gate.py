"""Part A tests for the foundation router's DANGEROUS_ACTIONS gate — the
M36 R-c rider (Wave F sprint 1, S1.6): `cleanup-backups` deletes backup files
via os.remove and must require `--user-confirmed` like its Setup-destructive
sibling `restore-database`.

Loads the router (source/erpclaw/scripts/db_query.py) by file location; the
gate behavior test drives `_gate_dangerous_action` directly with a patched
sys.argv (the gate reads the per-invocation flag from argv by design — env
bypasses are intentionally not honored).
"""
import importlib.util
import json
import os
import sys
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROUTER_PATH = os.path.join(os.path.dirname(_TESTS_DIR), "db_query.py")


def _load_router():
    spec = importlib.util.spec_from_file_location("erpclaw_router", _ROUTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROUTER = _load_router()


def test_cleanup_backups_is_dangerous():
    """M36 R-c: cleanup-backups joins DANGEROUS_ACTIONS (it os.remove's
    backup files — irreversible)."""
    assert "cleanup-backups" in ROUTER.DANGEROUS_ACTIONS
    # Its Setup-destructive sibling stays gated too.
    assert "restore-database" in ROUTER.DANGEROUS_ACTIONS


def test_cleanup_backups_blocked_without_flag(capsys):
    with patch.object(sys, "argv",
                      ["db_query.py", "--action", "cleanup-backups"]):
        with pytest.raises(SystemExit) as exc:
            ROUTER._gate_dangerous_action("cleanup-backups")
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "user_confirmation_required"
    assert payload["action"] == "cleanup-backups"


def test_cleanup_backups_passes_with_flag():
    with patch.object(sys, "argv",
                      ["db_query.py", "--action", "cleanup-backups",
                       "--user-confirmed"]):
        # No exit, no output — the gate lets dispatch proceed.
        ROUTER._gate_dangerous_action("cleanup-backups")


def test_read_only_backup_siblings_stay_ungated():
    """list-backups / verify-backup are read-only and must NOT be gated."""
    assert "list-backups" not in ROUTER.DANGEROUS_ACTIONS
    assert "verify-backup" not in ROUTER.DANGEROUS_ACTIONS
    with patch.object(sys, "argv", ["db_query.py", "--action", "list-backups"]):
        ROUTER._gate_dangerous_action("list-backups")  # no exit
