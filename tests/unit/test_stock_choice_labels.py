"""Stock setup questions accept the owner's warehouse name or account number."""
import pytest

from erpclaw_assistant.resolve_state import Session, match_answer


@pytest.mark.parametrize("field, label, ref", [
    ("add_item#0:warehouse", "Create Main Warehouse", "Main Warehouse"),
    ("add_item#0:inventory_account", "1300 Inventory", "1300"),
])
def test_stock_setup_choice_can_be_answered_by_its_displayed_reference(field, label, ref):
    session = Session()
    session.ask(field, "Choose one", "warehouse_unknown", [label], [ref])
    answer = match_answer(session.pending, ref)
    assert answer == ("choice", "q1-1")
    assert session.answer(answer[1])
    assert session.bindings[field] == ref
    assert not session.answer(answer[1])


def test_unoffered_account_number_does_not_bind():
    session = Session()
    session.ask("add_item#0:inventory_account", "Choose one", "account_ambiguous",
                ["1300 Inventory", "1131 Raw Materials"], ["1300", "1131"])
    assert match_answer(session.pending, "9999") is None
    assert not session.bindings
