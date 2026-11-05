"""The stock services inside an approved card's commit, through a planted consumer (consumer.py): the pre-commit
comparison, zero values, cancelling by reversal, and the stale backstop. Each refusal is paired with the acceptance
of the same rule, so an implementation that refuses everything fails."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from erpclaw_core import approvals, ledger
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t
from erpclaw_stock.service import Trusted

import stock_consumer as consumer
from service_plugins import install  # tests/ is on the path

DAY = date(2026, 10, 20)


@pytest.fixture
def books(engine, monkeypatch):
    """The demo books with one warehouse on the inventory account and Copper Wire kept in it (moving average)."""
    return stocked_books(engine, monkeypatch, consumer.plugin())


def stocked_books(engine, monkeypatch, planted) -> dict:
    install(monkeypatch, engine, planted)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        wh = stock.create_warehouse(ctx, "Main Store", default=True)
        item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                                                      m.item.c.name == "Copper Wire")).scalar_one()
        stock.make_stock_item(ctx, item, "moving_average")
        customer = conn.execute(select(m.party.c.id).where(m.party.c.company_id == ctx.company_id,
                                                           m.party.c.name == "Acme Corporation")).scalar_one()
    return {"engine": engine, "warehouse": wh, "item": item, "customer": customer}


def receive(books, qty: str, rate: str, when: date = date(2026, 10, 1)) -> None:
    trusted = Trusted("the test's stock receipt")
    with books["engine"].begin() as conn:
        ctx = company_ctx(conn)
        stock.receive(ctx, f"pr-{new_id()[:8]}", books["item"], books["warehouse"], qty, rate, when, trusted=trusted)
        trusted.verify(ctx)


def business_rows(engine) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(tbl)).scalar()
                     for tbl in (*t.TABLES, m.posting, m.ledger_entry))


def run(books, contract: str, **args) -> approvals.Outcome:
    card = approvals.propose(books["engine"], REGISTRY.contracts[contract], args)
    return approvals.approve(books["engine"], card.approval_id, card.digest)


def sale(books, qty: str, price: str = "100.00", **extra) -> approvals.Outcome:
    return run(books, "stock_sale", customer_id=books["customer"], item_id=books["item"],
               warehouse_id=books["warehouse"], qty=qty, price=price, voucher=extra.pop("voucher", f"inv-{new_id()[:8]}"),
               posting_date=DAY.isoformat(), **extra)


def verify(books) -> None:
    with books["engine"].connect() as conn:
        ctx = company_ctx(conn)
        assert conn.execute(select(func.count()).select_from(t.entry).where(t.entry.c.company_id == ctx.company_id)
                            ).scalar() > 0  # an empty company is not a pass
        assert stock.verify_stock(ctx) == []


def posting_lines(books, voucher: str) -> list[tuple[str, Decimal, Decimal]]:
    with books["engine"].connect() as conn:
        ctx = company_ctx(conn)
        p = ledger.posting_for_voucher(ctx, "planted_sale", voucher)
        le = m.ledger_entry
        return [(r.account_id, r.debit, r.credit) for r in conn.execute(
            select(le.c.account_id, le.c.debit, le.c.credit).where(le.c.posting_id == p["id"]).order_by(le.c.line_no))]


# ---------------------------------------------------------------- the pre-commit comparison

def test_a_valid_stock_backed_sale_passes_the_comparison(books):
    receive(books, "10", "10.00")
    out = sale(books, "4", voucher="inv-ok")
    assert out.status == "committed", out.text
    lines = posting_lines(books, "inv-ok")
    assert len(lines) == 4 and sum(d for _, d, _ in lines) == Decimal("140.00")  # 100.00 receivable + 40.00 cost
    verify(books)


@pytest.mark.parametrize("tamper", ["drop_cost", "off_by_cent", "extra_inventory"])
def test_a_posting_that_differs_from_the_stock_lines_refuses_the_whole_commit(books, tamper):
    receive(books, "10", "10.00")
    before = business_rows(books["engine"])
    out = sale(books, "4", tamper=tamper)
    assert out.status == "refused" and out.data.get("code") == "stock_posting_mismatch", (out.status, out.text)
    assert business_rows(books["engine"]) == before  # the stock rows and the posting rolled back together
    verify(books)


# ---------------------------------------------------------------- zero values

def test_a_zero_cost_issue_stores_zero_posts_no_stock_line_and_the_last_units_carry_the_value(books):
    receive(books, "1", "0.01")
    card = approvals.propose(books["engine"], REGISTRY.contracts["stock_sale"], dict(
        customer_id=books["customer"], item_id=books["item"], warehouse_id=books["warehouse"], qty="0.125",
        price="5.00", voucher="inv-z1", posting_date=DAY.isoformat()))
    assert ["Cost of goods sold", "0.00 USD (less than 0.01 USD)"] in [list(x) for x in card.card["lines"]]
    out = approvals.approve(books["engine"], card.approval_id, card.digest)
    assert out.status == "committed", out.text
    assert len(posting_lines(books, "inv-z1")) == 2  # receivable and sales only
    with books["engine"].connect() as conn:
        assert conn.execute(select(t.entry.c.value).where(t.entry.c.voucher_id == "inv-z1")).scalar_one() == 0
    out = sale(books, "0.875", voucher="inv-z2")
    assert out.status == "committed"
    assert sorted(d for _, d, _ in posting_lines(books, "inv-z2") if d) == [Decimal("0.01"), Decimal("100.00")]
    verify(books)


def test_a_delivery_whose_only_stock_line_costs_zero_makes_no_posting_and_passes(books):
    receive(books, "1", "0.01")
    out = sale(books, "0.125", price="0", voucher="dn-z")
    assert out.status == "committed", out.text
    with books["engine"].connect() as conn:
        assert conn.execute(select(m.posting.c.id).where(m.posting.c.voucher_id == "dn-z")).first() is None
        assert conn.execute(select(t.entry.c.value).where(t.entry.c.voucher_id == "dn-z")).scalar_one() == 0
    verify(books)


def test_the_smallest_unit_is_the_currencys_own():
    assert [stock.smallest_unit(c) for c in ("USD", "JPY", "KWD")] == ["0.01 USD", "1 JPY", "0.001 KWD"]
    assert consumer.cost_words({"cost": "0", "card_note": "less than 1 JPY"}, "JPY") == "0 JPY (less than 1 JPY)"
    assert consumer.cost_words({"cost": "33"}, "JPY") == "33 JPY"


# ---------------------------------------------------------------- cancelling by reversal

def test_a_cancellation_reverses_the_cost_once_and_one_that_posts_it_again_is_refused(books):
    receive(books, "10", "10.00")
    assert sale(books, "4", voucher="inv-a").status == "committed"
    assert sale(books, "2", voucher="inv-b").status == "committed"
    before = business_rows(books["engine"])
    # inv-a is no longer the latest movement: refused at the card, before anything is shown
    with pytest.raises(approvals.Refusal) as caught:
        approvals.propose(books["engine"], REGISTRY.contracts["stock_sale_cancel"],
                          dict(voucher="inv-a", reversal_date=DAY.isoformat()))
    assert caught.value.code == "not_latest"
    out = run(books, "stock_sale_cancel", voucher="inv-b", reversal_date=DAY.isoformat(), tamper="post_stock_twice")
    assert out.status == "refused" and out.data.get("code") == "stock_posting_mismatch", out.text
    assert business_rows(books["engine"]) == before
    out = run(books, "stock_sale_cancel", voucher="inv-b", reversal_date=DAY.isoformat())
    assert out.status == "committed", out.text
    with books["engine"].connect() as conn:
        h = conn.execute(select(t.head)).mappings().one()
    assert (h["qty"], h["value"]) == (Decimal("6"), Decimal("60.00"))  # 10 less inv-a's 4: inv-b's 2 came back
    verify(books)


# ---------------------------------------------------------------- the stale backstop

def test_a_card_whose_read_set_left_out_the_stock_it_read_is_refused_as_stale(books):
    receive(books, "10", "10.00")
    out = sale(books, "4", declare_heads=False)
    assert out.status == "refused" and out.data.get("code") == "stale", out.text
    out = sale(books, "4")  # the same sale, declaring what it read
    assert out.status == "committed", out.text
    verify(books)



def test_a_zero_cost_card_in_yen_says_less_than_one_yen(engine, monkeypatch):
    install(monkeypatch, engine, consumer.plugin())
    from erpclaw_core.setup import create_company
    cid = create_company(engine, f"Yen Co {new_id()[:8]}", "JPY")
    trusted = Trusted("the yen test's receipt")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=cid)
        wh = stock.create_warehouse(ctx, "Tokyo Store", default=True)
        item, customer = new_id(), new_id()
        conn.execute(m.item.insert().values(id=item, company_id=cid, name="Pin", unit="unit", standard_rate="1"))
        conn.execute(m.party.insert().values(id=customer, company_id=cid, kind="customer", name="Sato Trading"))
        stock.make_stock_item(ctx, item, "moving_average")
        stock.receive(ctx, "pr-yen", item, wh, "1", "1", date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
    card = approvals.propose(engine, REGISTRY.contracts["stock_sale"], dict(
        customer_id=customer, item_id=item, warehouse_id=wh, qty="0.125", price="100", voucher="inv-yen",
        posting_date=DAY.isoformat()), company_id=cid)
    assert ["Cost of goods sold", "0 JPY (less than 1 JPY)"] in [list(x) for x in card.card["lines"]]
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    with engine.connect() as conn:
        assert stock.verify_stock(company_ctx(conn, company_id=cid)) == []
