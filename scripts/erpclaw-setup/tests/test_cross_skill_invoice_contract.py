"""Contract tests for the cross-skill sales-invoice bridge.

Pins the selling module's flat response shape (sales_invoice_id at the top
level), the refusal of arguments create-sales-invoice cannot carry, and the
generic service-item resolution for description-only lines. No subprocess is
spawned: resolve + subprocess.run are patched out and argv is captured.
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
from erpclaw_lib.cross_skill import CrossSkillError


class _Completed:
    def __init__(self, payload, returncode=0, stderr=""):
        self.stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.stderr = stderr
        self.returncode = returncode


def _patch_resolve(monkeypatch):
    monkeypatch.setattr(cross_skill, "resolve_skill_script",
                        lambda skill: "/tmp/fake/db_query.py")


def _action_of(cmd):
    return cmd[cmd.index("--action") + 1]


def test_create_invoice_refuses_project_id(monkeypatch):
    _patch_resolve(monkeypatch)
    calls = []

    def _must_not_spawn(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("subprocess must not run when the call is refused")

    monkeypatch.setattr(cross_skill.subprocess, "run", _must_not_spawn)
    with pytest.raises(CrossSkillError) as exc_info:
        cross_skill.create_invoice(
            customer_id="C1",
            items=[{"description": "Labor", "qty": "1", "rate": "100.00"}],
            company_id="CO1",
            project_id="P1",
        )
    assert "project_id" in str(exc_info.value)
    assert "create-sales-invoice" in str(exc_info.value)
    assert calls == []


def test_create_invoice_refuses_remarks(monkeypatch):
    _patch_resolve(monkeypatch)
    calls = []

    def _must_not_spawn(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("subprocess must not run when the call is refused")

    monkeypatch.setattr(cross_skill.subprocess, "run", _must_not_spawn)
    with pytest.raises(CrossSkillError) as exc_info:
        cross_skill.create_invoice(
            customer_id="C1",
            items=[{"description": "Labor", "qty": "1", "rate": "100.00"}],
            company_id="CO1",
            remarks="hello",
        )
    assert "remarks" in str(exc_info.value)
    assert "create-sales-invoice" in str(exc_info.value)
    assert calls == []


def test_create_invoice_resolves_service_item_for_description_lines(monkeypatch):
    _patch_resolve(monkeypatch)
    cross_skill._SERVICE_ITEM_CACHE.clear()
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if _action_of(cmd) == "add-item":
            return _Completed({"status": "ok", "item_id": "ITEM-SVC"})
        assert _action_of(cmd) == "create-sales-invoice"
        return _Completed({"status": "ok", "sales_invoice_id": "SI-1",
                           "total_amount": "500.00", "tax_amount": "0",
                           "grand_total": "500.00"})

    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    result = cross_skill.create_invoice(
        customer_id="C1",
        items=[{"description": "Labor", "qty": "1", "rate": "250.00"},
               {"description": "Travel", "qty": "1", "rate": "250.00"}],
        company_id="CO1",
    )
    add_calls = [c for c in calls if _action_of(c) == "add-item"]
    assert len(add_calls) == 1
    invoice_calls = [c for c in calls if _action_of(c) == "create-sales-invoice"]
    assert len(invoice_calls) == 1
    sent = json.loads(invoice_calls[0][invoice_calls[0].index("--items") + 1])
    assert len(sent) == 2
    for line in sent:
        assert line["item_id"] == "ITEM-SVC"
        assert line["rate"] == "250.00"
    assert result["sales_invoice_id"] == "SI-1"
    assert "sales_invoice" not in result


def test_ensure_service_item_reuses_existing_code(monkeypatch):
    _patch_resolve(monkeypatch)
    cross_skill._SERVICE_ITEM_CACHE.clear()

    def _fake_run(cmd, **kwargs):
        if _action_of(cmd) == "add-item":
            return _Completed(
                json.dumps({"status": "error",
                            "message": "Item creation failed — check for duplicates"}),
                returncode=1,
            )
        assert _action_of(cmd) == "list-items"
        return _Completed({"status": "ok",
                           "items": [{"id": "ITEM-OLD", "item_code": "SVC-CO1"}],
                           "total_count": 1})

    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    assert cross_skill.ensure_service_item("CO1") == "ITEM-OLD"


def test_create_purchase_invoice_refuses_project_id(monkeypatch):
    _patch_resolve(monkeypatch)
    calls = []

    def _must_not_spawn(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("subprocess must not run when the call is refused")

    monkeypatch.setattr(cross_skill.subprocess, "run", _must_not_spawn)
    with pytest.raises(CrossSkillError) as exc_info:
        cross_skill.create_purchase_invoice(
            supplier_id="S1",
            items=[{"description": "Freight", "qty": "1", "rate": "100.00"}],
            company_id="CO1",
            project_id="P1",
        )
    assert "project_id" in str(exc_info.value)
    assert "create-purchase-invoice" in str(exc_info.value)
    assert calls == []


def test_create_purchase_invoice_refuses_remarks(monkeypatch):
    _patch_resolve(monkeypatch)
    calls = []

    def _must_not_spawn(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("subprocess must not run when the call is refused")

    monkeypatch.setattr(cross_skill.subprocess, "run", _must_not_spawn)
    with pytest.raises(CrossSkillError) as exc_info:
        cross_skill.create_purchase_invoice(
            supplier_id="S1",
            items=[{"description": "Freight", "qty": "1", "rate": "100.00"}],
            company_id="CO1",
            remarks="hello",
        )
    assert "remarks" in str(exc_info.value)
    assert "create-purchase-invoice" in str(exc_info.value)
    assert calls == []


def test_create_purchase_invoice_action_and_flags(monkeypatch):
    skills = []

    def _fake_resolve(skill):
        skills.append(skill)
        return "/tmp/fake/db_query.py"

    monkeypatch.setattr(cross_skill, "resolve_skill_script", _fake_resolve)
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert _action_of(cmd) == "create-purchase-invoice"
        return _Completed({"status": "ok", "purchase_invoice_id": "PI-1",
                           "total_amount": "500.00", "tax_amount": "0",
                           "grand_total": "500.00"})

    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    items = [{"item_id": "I1", "qty": "1", "rate": "500.00"}]
    result = cross_skill.create_purchase_invoice(
        supplier_id="S1",
        items=items,
        company_id="CO1",
        posting_date="2026-09-01",
        due_date="2026-10-01",
    )
    assert skills == ["erpclaw"]
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == [cross_skill.child_interpreter(), "/tmp/fake/db_query.py"]
    assert _action_of(cmd) == "create-purchase-invoice"
    sent_flags = {cmd[i] for i in range(2, len(cmd), 2)}
    assert sent_flags == {"--action", "--supplier-id", "--items",
                          "--company-id", "--posting-date", "--due-date"}, cmd
    assert cmd[cmd.index("--supplier-id") + 1] == "S1"
    assert json.loads(cmd[cmd.index("--items") + 1]) == items
    assert cmd[cmd.index("--company-id") + 1] == "CO1"
    assert cmd[cmd.index("--posting-date") + 1] == "2026-09-01"
    assert cmd[cmd.index("--due-date") + 1] == "2026-10-01"
    for banned in ("--project-id", "--remarks", "add-purchase-invoice"):
        assert banned not in cmd, f"{banned} must not be sent: {cmd}"
    assert result["purchase_invoice_id"] == "PI-1"
    assert "purchase_invoice" not in result


def test_create_purchase_invoice_resolves_service_item_for_description_lines(monkeypatch):
    _patch_resolve(monkeypatch)
    cross_skill._SERVICE_ITEM_CACHE.clear()
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if _action_of(cmd) == "add-item":
            return _Completed({"status": "ok", "item_id": "ITEM-SVC"})
        assert _action_of(cmd) == "create-purchase-invoice"
        return _Completed({"status": "ok", "purchase_invoice_id": "PI-1",
                           "total_amount": "500.00", "tax_amount": "0",
                           "grand_total": "500.00"})

    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    passthrough = {"item_id": "I9", "description": "Parts", "qty": "2",
                   "rate": "100.00"}
    result = cross_skill.create_purchase_invoice(
        supplier_id="S1",
        items=[{"description": "Freight", "qty": "1", "rate": "300.00"},
               dict(passthrough)],
        company_id="CO1",
    )
    add_calls = [c for c in calls if _action_of(c) == "add-item"]
    assert len(add_calls) == 1
    invoice_calls = [c for c in calls if _action_of(c) == "create-purchase-invoice"]
    assert len(invoice_calls) == 1
    sent = json.loads(invoice_calls[0][invoice_calls[0].index("--items") + 1])
    assert len(sent) == 2
    assert sent[0]["item_id"] == "ITEM-SVC"
    assert sent[0]["rate"] == "300.00"
    assert sent[1] == passthrough
    assert result["purchase_invoice_id"] == "PI-1"
    assert "purchase_invoice" not in result
