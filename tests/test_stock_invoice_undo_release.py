"""Invoice cancellation stays planned; measurement belongs to the external dispatcher."""
import json
import os
from pathlib import Path

import pytest

from erpclaw_core import catalogue
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import PLANNED
from conftest import RELEASED


def test_cancel_invoice_stays_planned_until_the_payment_gate_passes(as_released):
    assert REGISTRY.contracts["cancel_invoice"].planned is None
    assert REGISTRY.contracts["cancel_stock_movement"].planned == "Core"


def test_cancel_invoice_catalogue_stays_planned(engine, as_released):
    with engine.connect() as conn:
        entries = {e.id: e for e in catalogue.catalogue(conn)}
        remedy = next(e for e in entries.values() if "edit posted" in e.synonyms)
    assert entries["op.cancel_invoice"].state == "installed"
    assert RELEASED[0]["cancel_invoice"] is None
    assert "cancel" in remedy.reply.lower()
    assert "not in ERPClaw yet" not in remedy.reply
    assert {n: v for n, v in RELEASED[0].items() if n in {"create_sales_order",
        "deliver_order", "create_credit_note", "cancel_stock_movement"}} == {
        n: "Core" for n in ("create_sales_order", "deliver_order",
                           "create_credit_note", "cancel_stock_movement")}


def test_the_log_line_carries_the_before_and_after_lines():
    """Consume dispatcher evidence when supplied; never call a model or invent a receipt."""
    path = os.environ.get("ERPCLAW_MEASUREMENT_LOG")
    if not path:
        pytest.skip("dispatcher supplies before/after measurement receipts")
    evidence = json.loads(Path(path).read_text())
    assert set(evidence) == {"eval", "honesty"}
    for runner in evidence.values():
        assert set(runner) == {"before", "after"}
        for result in runner.values():
            assert result["k"] == 1 and result["provider"] and result["quantization"]
            assert result["run_id"] and result["line"]
        assert runner["before"]["run_id"] != runner["after"]["run_id"]
