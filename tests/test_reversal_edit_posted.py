"""Specific refusals survive supplier-payment routing and unresolved picks."""
import pytest

from erpclaw_core import catalogue as cat


@pytest.mark.parametrize("said,pick,outcome,capability", [
    ("Reverse the posted supplier payment to correct an error", "op.reverse_supplier_payment", "a", None),
    ("reverse the supplier payment SPAY-2026-00012", "unsure", "e", None),
    ("reverse the supplier payment SPAY-2026-00012", "planned.purchasing", "c", "planned.purchasing"),
    ("reverse the supplier payment SPAY-2026-00012", "op.reverse_supplier_payment", "a", None),
    ("undo the payment to Stark", "unsure", "e", None),
    ("undo the payment to Stark", "planned.purchasing", "e", None),
    ("undo the payment to Stark", "op.reverse_supplier_payment", "a", None),
])
def test_reversal_routing_remains_planned(engine, said, pick, outcome, capability):
    with engine.connect() as conn:
        candidates = cat.find_capability(conn, said)
        named = cat.named_by_synonym(conn, said)
        decision = cat.decide(candidates, pick, named=named)
    assert decision.outcome == outcome
    assert decision.capability == capability


def test_none_h035_gives_named_refusal(engine, as_released):
    said = "Change the posted invoice INV-2026-00007 from 2 training days to 3. Leave it posted and just edit it."
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        decision = cat.decide(cat.find_capability(conn, said), "none", named=named)
    scores = {entry.id: score for entry, score in named}
    assert scores["unsupported.edit_posted"] == 8 and scores["op.create_invoice"] == 2
    assert (decision.outcome, decision.capability) == ("d", "unsupported.edit_posted")
