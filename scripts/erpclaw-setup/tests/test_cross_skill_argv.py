"""Unit tests for the erpclaw_lib.cross_skill sales-invoice argv contract.

Pins the exact action names and flags create_invoice/submit_invoice send to
the selling router: create-sales-invoice with only parser-accepted flags, and
submit-sales-invoice with --sales-invoice-id. No subprocess is spawned.
"""
import json
import os
import sys

import pytest

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# M54: bind erpclaw_lib to the tree under test, never the deployed
# ~/.openclaw/erpclaw/lib symlink.
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib import cross_skill


class _Completed:
    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.stderr = ""
        self.returncode = 0


def _capture_run(monkeypatch):
    """Patch out resolve + subprocess; return the captured argv list."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed({"status": "ok", "sales_invoice_id": "SI-1"})

    monkeypatch.setattr(cross_skill, "resolve_skill_script",
                        lambda skill: "/tmp/fake/db_query.py")
    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    return calls


def _flag(cmd, name):
    assert name in cmd, f"{name} missing from argv: {cmd}"
    return cmd[cmd.index(name) + 1]


def test_create_invoice_action_and_flags(monkeypatch):
    calls = _capture_run(monkeypatch)
    items = [{"item_id": "I1", "qty": "1", "rate": "100.00"}]
    cross_skill.create_invoice(
        customer_id="C1", items=items, company_id="CO1",
        posting_date="2026-09-01", due_date="2026-10-01",
        db_path="/tmp/x.sqlite",
    )
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == [cross_skill.child_interpreter(), "/tmp/fake/db_query.py"]
    assert _flag(cmd, "--action") == "create-sales-invoice"
    assert _flag(cmd, "--customer-id") == "C1"
    assert json.loads(_flag(cmd, "--items")) == items
    assert _flag(cmd, "--company-id") == "CO1"
    assert _flag(cmd, "--posting-date") == "2026-09-01"
    assert _flag(cmd, "--due-date") == "2026-10-01"
    assert _flag(cmd, "--db-path") == "/tmp/x.sqlite"
    for banned in ("--project-id", "--remarks", "add-sales-invoice"):
        assert banned not in cmd, f"{banned} must not be sent: {cmd}"


def test_submit_invoice_flag(monkeypatch):
    calls = _capture_run(monkeypatch)
    cross_skill.submit_invoice("SI-9", db_path="/tmp/x.sqlite")
    assert len(calls) == 1
    cmd = calls[0]
    assert _flag(cmd, "--action") == "submit-sales-invoice"
    assert _flag(cmd, "--sales-invoice-id") == "SI-9"
    assert "--user-confirmed" in cmd, f"gated action needs confirmation: {cmd}"
    assert "--invoice-id" not in cmd, f"stale flag sent: {cmd}"


def test_create_invoice_passes_item_id_lines_through_untouched(monkeypatch):
    calls = _capture_run(monkeypatch)
    items = [
        {"item_id": "I1", "description": "Labor", "qty": "2", "rate": "100.00"},
        {"item_id": "I2", "description": "Parts", "qty": "1", "rate": "50.00",
         "uom": "Nos"},
    ]
    result = cross_skill.create_invoice(
        customer_id="C1", items=items, company_id="CO1",
        db_path="/tmp/x.sqlite",
    )
    assert len(calls) == 1
    cmd = calls[0]
    assert _flag(cmd, "--action") == "create-sales-invoice"
    assert json.loads(_flag(cmd, "--items")) == items
    assert result["sales_invoice_id"] == "SI-1"
