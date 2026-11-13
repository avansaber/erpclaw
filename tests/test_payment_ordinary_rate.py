"""Payment figures retain ordinary rate recognition outside invoice contexts."""
import pytest

from erpclaw_assistant import resolve_text
from test_payment_resolve import pay, kinds
from test_payment_phrase_misc import receipts


MAIN_RATE = (r"(?:\bpriced\s+at\b|\bat\b|@)\s*[^\w\s]?\s*(\d[\d,]*(?:\.\d+)?)|"
             r"(\d[\d,]*(?:\.\d+)?)\s*(?:\beach\b|\bper\b|\ba\s+piece\b|\bapiece\b)")


def test_payment_with_separate_a_unit_figure(receipts):
    # Oracle: the same scripted turn with ordinary_rate temporarily set to MAIN_RATE.
    events = pay(receipts, "received 300 from Hooli for 2 a unit", customer="Hooli", amount=300)
    assert kinds(events) == ["ask"]
    assert events[0]["code"] == "figure_ambiguous"
    assert events[0]["text"] == "What is the 2 for? I couldn't place it in this request."


@pytest.mark.parametrize("invoice", (False, True))
def test_rate_table_is_scoped_to_invoice_context(invoice):
    token = resolve_text._invoice_rate_context.set(invoice)
    try:
        table = resolve_text.table()
        if invoice:
            assert table["rate"] == table["invoice_rate"]
        else:
            assert table["rate"] == MAIN_RATE
    finally:
        resolve_text._invoice_rate_context.reset(token)
