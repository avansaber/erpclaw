"""Explicit and unresolved picks for the planned supplier reversal."""

import pytest

from erpclaw_core import catalogue as cat
from erpclaw_core.plugins import REGISTRY


@pytest.mark.parametrize("pick, capability", [
    ("planned.purchasing", "planned.purchasing"),
    ("op.reverse_supplier_payment", "planned.op.reverse_supplier_payment"),
    ("unsure", "planned.purchasing"),
    ("none", "planned.purchasing"),
    ("unrecognized.pick", "planned.purchasing"),
])
def test_planned_reversal_pick(engine, monkeypatch, pick, capability):
    monkeypatch.setattr(REGISTRY.contracts["reverse_supplier_payment"], "planned", "later")
    said = "reverse supplier payment SPAY-2026-00012"
    with engine.connect() as conn:
        decision = cat.decide(cat.find_capability(conn, said), pick,
                              named=cat.named_by_synonym(conn, said))
    assert (decision.outcome, decision.capability) == ("c", capability)
