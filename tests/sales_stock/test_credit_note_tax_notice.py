"""Full and partial stock-return cards carry the existing no-tax notice."""

from datetime import date

import pytest

from erpclaw_core import approvals
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import NO_TAX_NOTICE

from sales_books import Books


@pytest.mark.parametrize(
    "credit_args, quantity, total, outstanding, cost",
    [
        pytest.param({"everything": True}, "4", "112.00", "0.00", "80.00", id="full"),
        pytest.param({"items": [{"item": "Returned Part", "quantity": 1}]},
                     "1", "28.00", "84.00", "20.00", id="partial"),
    ],
)
def test_credit_note_card_includes_exact_tax_line(
    engine, credit_args, quantity, total, outstanding, cost
):
    books = Books(engine, "Return Cards")
    books.customer("Return Customer")
    books.warehouse("Return Store", default=True)
    books.item("Returned Part", "28.00")
    books.receive("Returned Part", "Return Store", "10", "20.00", date(2026, 10, 25))
    _, invoice = books.run(
        "create_invoice", customer="Return Customer",
        items=[{"item": "Returned Part", "quantity": 4}], submit=True,
    )
    assert invoice.status == "committed", invoice.text
    number = invoice.data["number"]
    with engine.connect() as conn:
        company_name = books.ctx(conn).company_name

    credit = approvals.propose(
        engine, REGISTRY.contracts["create_credit_note"],
        {"invoice": number, **credit_args}, company_id=books.company,
    )

    assert credit.card["lines"] == [
        ["Company", company_name],
        ["Invoice", f"{number}: submitted, 112.00 outstanding, nothing paid"],
        ["Customer", "Return Customer"],
        ["Credit", f"line 1: {quantity} x Returned Part @ 28.00 = {total}"],
        ["Back into stock", f"{quantity} into Return Store against {number} at {cost}"],
        ["Total credit", f"{total} USD"],
        ["Outstanding after", f"{outstanding} USD"],
        ["Date", "26 Oct 2026"],
        ["Tax", NO_TAX_NOTICE],
    ]
