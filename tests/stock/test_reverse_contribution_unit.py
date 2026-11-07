"""Pure reversal contribution checks: original-entry data in memory, with SQL/network forbidden by the runner."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from erpclaw_core.common import Ctx, Refusal
from erpclaw_stock import service


@pytest.fixture
def context(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No database access in reversal contribution units")
    ctx = Ctx(SimpleNamespace(execute=forbidden), "company", "Test Company", "USD", date(2026, 11, 4))
    monkeypatch.setattr(service, "role_account", lambda ctx, role: {
        "cost_of_goods_sold": "cogs", "stock_received_not_billed": "srnb"}[role])
    monkeypatch.setattr(service, "stock_inventory", lambda ctx: {"inv"})
    return ctx


def move(direction, value, *, original_direction, against=None, account="inv"):
    pair = SimpleNamespace(account=account, item={"item_id": "item"}, wh={"id": "wh"}, head={"id": "head"})
    return SimpleNamespace(pair=pair, direction=direction, value=Decimal(value), qty=Decimal(30),
                           qty_after=Decimal(0), value_after=Decimal(0),
                           original={"direction": original_direction, "against_entry_id": against})


@pytest.mark.parametrize("voucher_type", ["stock_receipt", "stock_opening"])
def test_plain_receipt_reversal_is_inventory_only(context, monkeypatch, voucher_type):
    moves = [move("out", "345", original_direction="in")]
    monkeypatch.setattr(service, "_reverse_moves", lambda *args, **kwargs: moves)
    result = service.preview_reverse(context, voucher_type, "voucher", context.today)
    assert result["contribution"] == [{"account": "inv", "net": "-345.00"}]


def test_goods_received_requires_delivery_reversal_before_contribution(context, monkeypatch):
    queries = []

    def execute(query):
        queries.append(query)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: None))

    monkeypatch.setattr(context.conn, "execute", execute)
    monkeypatch.setattr(service, "_reverse_moves", lambda *a, **k: pytest.fail("generic goods-received reversal"))
    with pytest.raises(Refusal) as exc:
        service.preview_reverse(context, "stock_goods_received", "voucher", context.today)
    assert exc.value.code == "goods_received_reversal_required"
    assert exc.value.message == "Goods received must be reversed as a delivery."
    assert exc.value.data == {"points_to": "reverse_goods_received"}
    assert len(queries) == 1
    assert queries[0].compile().params == {"company_id_1": "company", "voucher_id_1": "voucher"}


def test_mixed_receipt_issue_reversal_has_only_the_original_cogs(context, monkeypatch):
    moves = [move("out", "345", original_direction="in"), move("in", "100", original_direction="out")]
    monkeypatch.setattr(service, "_reverse_moves", lambda *args, **kwargs: moves)
    result = service.preview_reverse(context, "stock_test_mixed", "voucher", context.today)
    assert result["contribution"] == [{"account": "cogs", "net": "-100.00"}, {"account": "inv", "net": "-245.00"}]


def test_return_reversal_retains_its_cogs_side(context, monkeypatch):
    moves = [move("out", "40", original_direction="in", against="original-issue")]
    monkeypatch.setattr(service, "_reverse_moves", lambda *args, **kwargs: moves)
    result = service.preview_reverse(context, "sales_credit_note", "voucher", context.today)
    assert result["contribution"] == [{"account": "cogs", "net": "40.00"}, {"account": "inv", "net": "-40.00"}]


def test_issue_reversal_retains_its_cogs_side(context, monkeypatch):
    moves = [move("in", "100", original_direction="out")]
    monkeypatch.setattr(service, "_reverse_moves", lambda *args, **kwargs: moves)
    result = service.preview_reverse(context, "sales_invoice", "voucher", context.today)
    assert result["contribution"] == [{"account": "cogs", "net": "-100.00"}, {"account": "inv", "net": "100.00"}]
