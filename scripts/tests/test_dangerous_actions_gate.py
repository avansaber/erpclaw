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


# ── Wave G F17: bad-debt write-off joins the gated transaction class ─────────
#
# QA condition 2. Submitting an invoice is gated and cancelling it — the only
# undo for a write-off — is gated, while permanently forgiving the same
# receivable was not. Both write-off actions post GL, so they are gated; neither
# is in ADR-0018's five-member destructive list (that list is Nik-ratified and
# closed), so both are transaction-class: the agent may pass the flag on a clear
# request without re-asking.

@pytest.mark.parametrize("action", ["write-off-invoice", "legal-write-off-invoice"])
def test_write_off_actions_are_gated(action):
    assert action in ROUTER.DANGEROUS_ACTIONS
    # The asymmetry that made this necessary: both neighbours were already gated.
    assert "submit-sales-invoice" in ROUTER.DANGEROUS_ACTIONS
    assert "cancel-sales-invoice" in ROUTER.DANGEROUS_ACTIONS


@pytest.mark.parametrize("action", ["write-off-invoice", "legal-write-off-invoice"])
def test_write_off_blocked_without_flag(action, capsys):
    with patch.object(sys, "argv", ["db_query.py", "--action", action]):
        with pytest.raises(SystemExit) as exc:
            ROUTER._gate_dangerous_action(action)
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "user_confirmation_required"
    assert payload["action"] == action


@pytest.mark.parametrize("action", ["write-off-invoice", "legal-write-off-invoice"])
def test_write_off_passes_with_flag(action):
    with patch.object(sys, "argv",
                      ["db_query.py", "--action", action, "--user-confirmed"]):
        ROUTER._gate_dangerous_action(action)  # no exit


def test_write_off_is_transaction_class_not_destructive():
    """ADR-0018 dec. 1: the destructive list is closed at five members.

    A write-off is reversible by cancelling the invoice, so it must NOT be
    smuggled into the destructive class — that list is Nik-ratified and any
    addition needs its own ratification, not a lane's judgement.
    """
    destructive = {"close-fiscal-year", "restore-database", "install-module",
                   "rollback-foundation", "generate-nacha-file"}
    assert "write-off-invoice" not in destructive
    assert "legal-write-off-invoice" not in destructive
    assert destructive <= ROUTER.DANGEROUS_ACTIONS


def test_the_mcp_layer_sees_the_gate_too():
    """The MCP confirm map AST-reads the router's frozenset (ADR-0024 sub-dec 2),
    so gating here must reach the protocol surface with no second edit."""
    import importlib.util as _il
    mcp_dir = os.path.join(os.path.dirname(os.path.dirname(_TESTS_DIR)), "mcp")
    spec = _il.spec_from_file_location("_f17_skill_reader",
                                       os.path.join(mcp_dir, "skill_reader.py"))
    reader = _il.module_from_spec(spec)
    spec.loader.exec_module(reader)
    seen = reader.dangerous_actions()
    assert "write-off-invoice" in seen
    assert "legal-write-off-invoice" in seen
