"""The stock rules the fixtures do not already pin, each refusal paired with its acceptance: the company's own
warehouse, a closed period, the valuation method, the account's role; the warehouse a line takes from; a warehouse's
inventory account; books made before the chart revision; two warehouses sharing one account; the append-only rows;
and the reversals with figures."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t
from erpclaw_stock.service import Trusted

D = Decimal


@pytest.fixture
def co(engine):
    """A fresh USD company with one item kept in stock, its method chosen by the test."""
    cid = create_company(engine, f"Rules Co {new_id()[:8]}")
    return {"engine": engine, "company": cid}


def ctx_of(conn, co):
    return company_ctx(conn, company_id=co["company"])


TRUSTED = Trusted("the rules tests' books")


def item(conn, co, name: str = "Bearing") -> str:
    iid = new_id()
    conn.execute(m.item.insert().values(id=iid, company_id=co["company"], name=name, unit="unit", standard_rate="1"))
    return iid


def stocked(co, method: str = "fifo", warehouses: tuple[str, ...] = ("Main",)) -> dict:
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        whs = [stock.create_warehouse(ctx, n, default=i == 0) for i, n in enumerate(warehouses)]
        iid = item(conn, co)
        stock.make_stock_item(ctx, iid, method)
    return {"item": iid, "warehouses": whs}


def receive(co, s, qty, rate, when=date(2026, 3, 2), wh=0) -> str:
    voucher = f"pr-{new_id()[:8]}"
    with co["engine"].begin() as conn:
        stock.receive(ctx_of(conn, co), voucher, s["item"], s["warehouses"][wh], qty, rate, when, trusted=TRUSTED)
    return voucher


def issue(co, s, qty, when=date(2026, 3, 12), wh=0) -> str:
    voucher = f"inv-{new_id()[:8]}"
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' sale")
        out = stock.issue(ctx, "sales_invoice", voucher, when, [{"item": s["item"], "warehouse": s["warehouses"][wh],
                                                                  "qty": qty}], trusted=trusted)
        if out["ledger_lines"]:
            ledger.post(ctx, when, "sales_invoice", voucher, [ledger.Line(x["account"], debit=D(x["debit"]),
                                                                          credit=D(x["credit"]))
                                                              for x in out["ledger_lines"]])
        trusted.verify(ctx)
    return voucher


def reverse(co, voucher_type, voucher, when):
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' reversal")
        out = stock.reverse_movements(ctx, voucher_type, voucher, when, f"{voucher_type}_reversal", f"{voucher}-rev",
                                      trusted=trusted)
        posting = ledger.posting_for_voucher(ctx, voucher_type, voucher)
        ledger.reverse(ctx, posting["id"], when)
        trusted.verify(ctx)
    return out


def verify(co) -> None:
    with co["engine"].connect() as conn:
        ctx = company_ctx(conn, company_id=co["company"])
        assert conn.execute(select(func.count()).select_from(t.entry).where(
            t.entry.c.company_id == co["company"])).scalar() > 0  # an empty company is not a pass
        assert stock.verify_stock(ctx) == []


def refused(co, fn) -> Refusal:
    with co["engine"].begin() as conn:
        with pytest.raises(Refusal) as caught:
            fn(ctx_of(conn, co))
    return caught.value


# ---------------------------------------------------------------- 2.4, the rules the fixtures do not pin

def test_rule_1_a_warehouse_of_another_company_is_refused_and_its_own_is_taken(co, engine):
    s = stocked(co, "moving_average")
    other = create_company(engine, f"Other Co {new_id()[:8]}")
    with engine.begin() as conn:
        theirs = stock.create_warehouse(company_ctx(conn, company_id=other), "Theirs")
    r = refused(co, lambda ctx: stock.receive(ctx, "pr-x", s["item"], theirs, "1", "1.00", date(2026, 3, 2),
                                              trusted=TRUSTED))
    assert r.code == "not_company_warehouse"
    receive(co, s, "1", "1.00")
    verify(co)


def test_rule_3_a_closed_period_is_refused_and_an_open_one_taken(co):
    s = stocked(co, "moving_average")
    with co["engine"].begin() as conn:
        conn.execute(update(m.fiscal_period).where(m.fiscal_period.c.company_id == co["company"],
                                                   m.fiscal_period.c.start_date == date(2026, 2, 1)).values(
            status="closed"))
    r = refused(co, lambda ctx: stock.receive(ctx, "pr-x", s["item"], s["warehouses"][0], "1", "1.00",
                                              date(2026, 2, 10), trusted=TRUSTED))
    assert r.code == "period_closed" and "2026-02-10" in r.message
    receive(co, s, "1", "1.00", date(2026, 3, 2))
    verify(co)


def test_rule_6_a_method_other_than_the_two_is_refused_never_defaulted(co):
    with co["engine"].begin() as conn:
        iid = item(conn, co, "Widget")
        with pytest.raises(Refusal) as caught:
            stock.make_stock_item(ctx_of(conn, co), iid, "lifo")
        assert caught.value.code == "bad_valuation_method" and "lifo" in caught.value.message
        assert stock.make_stock_item(ctx_of(conn, co), iid, "fifo")


def test_rule_7_a_warehouse_on_an_account_without_the_stock_role_is_refused(co):
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        bank = ledger.account_by_type(ctx, "bank")["id"]
        with pytest.raises(Refusal) as caught:
            stock.create_warehouse(ctx, "Odd Store", bank)
        assert caught.value.code == "not_inventory_account"
    with co["engine"].begin() as conn:
        assert stock.create_warehouse(ctx_of(conn, co), "Good Store")  # the stock account: accepted


def test_a_quantity_is_refused_with_its_field_and_value_floats_included(co):
    s = stocked(co, "moving_average")
    receive(co, s, "10", "1.00")
    for bad in ("0", "-1", "abc", "1.00001", 1.5):
        r = refused(co, lambda ctx: stock.preview_issue(ctx, date(2026, 3, 12), [
            {"item": s["item"], "warehouse": s["warehouses"][0], "qty": bad}]))
        assert r.code == "bad_quantity" and r.data["field"] == "qty", bad
    with co["engine"].connect() as conn:
        ok = stock.preview_issue(company_ctx(conn, company_id=co["company"]), date(2026, 3, 12), [
            {"item": s["item"], "warehouse": s["warehouses"][0], "qty": "1.5000"}])
    assert ok["lines"] == [{"cost": "1.50", "currency": "USD"}]
    verify(co)


# ---------------------------------------------------------------- 2.6, the warehouse a line takes from

def test_the_items_own_default_warehouse_comes_first_when_it_holds_enough(co):
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        a = stock.create_warehouse(ctx, "North", default=True)
        b = stock.create_warehouse(ctx, "South")
        iid = item(conn, co)
        stock.make_stock_item(ctx, iid, "moving_average", default_warehouse=b)
    s = {"item": iid, "warehouses": [a, b]}
    receive(co, s, "10", "1.00", wh=0)
    receive(co, s, "10", "1.00", wh=1)
    with co["engine"].connect() as conn:
        ctx = company_ctx(conn, company_id=co["company"])
        assert stock.choose_warehouse(ctx, iid, "5")["chosen"]["warehouse"] == b  # the item's, before the company's
        assert stock.choose_warehouse(ctx, iid, "11")["short"] == [{"warehouse": a, "qty": "10"},
                                                                   {"warehouse": b, "qty": "10"}]
    verify(co)


# ---------------------------------------------------------------- a warehouse's inventory account

def test_a_warehouse_takes_the_one_inventory_account_asks_between_two_and_refuses_with_none(co):
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        one = stock.create_warehouse(ctx, "First")
        assert stock.warehouse(ctx, one)["account_id"] == ledger.account_by_type(ctx, "stock")["id"]
        inv = ledger.account_by_type(ctx, "stock")
        conn.execute(m.account.insert().values(id=new_id(), company_id=co["company"], number="1310",
                                               name="Finished Goods", root_type="asset", account_type="stock",
                                               is_group=False, parent_id=inv["parent_id"], normal_side="debit"))
        with pytest.raises(Refusal) as caught:
            stock.create_warehouse(ctx, "Second")
        q = caught.value
        assert q.code == "account_ambiguous" and q.data["field"] == "account"
        assert q.options == ["1300 Inventory", "1310 Finished Goods"]  # each by number and name
    with co["engine"].begin() as conn:
        conn.execute(update(m.account).where(m.account.c.company_id == co["company"],
                                             m.account.c.account_type == "stock").values(account_type=None))
        with pytest.raises(Refusal) as caught:
            stock.create_warehouse(ctx_of(conn, co), "Third")
        assert caught.value.code == "no_stock_account" and caught.value.message.endswith("has no inventory account.")


# ---------------------------------------------------------------- books made before the chart revision

def test_books_without_stock_roles_refuse_a_stock_write_in_words_and_write_nothing(co):
    s = stocked(co, "moving_average")
    receive(co, s, "10", "1.00")
    with co["engine"].begin() as conn:  # the stock roles gone, as on books made before the chart revision
        conn.execute(update(m.account).where(m.account.c.company_id == co["company"], m.account.c.account_type.in_(
            ("stock", "cost_of_goods_sold", "stock_received_not_billed"))).values(account_type=None))
    with co["engine"].connect() as conn:
        rows = tuple(conn.execute(select(func.count()).select_from(x)).scalar()
                     for x in (*t.TABLES, m.posting, m.audit_log))
    for write in (lambda ctx: stock.create_warehouse(ctx, "New"),
                  lambda ctx: stock.issue(ctx, "sales_invoice", "inv-x", date(2026, 3, 12), [
                      {"item": s["item"], "warehouse": s["warehouses"][0], "qty": "1"}], trusted=TRUSTED)):
        r = refused(co, write)
        assert r.code in ("no_stock_account", "not_inventory_account", "no_cost_of_goods_sold_account"), r.code
        assert r.message.startswith(("Rules Co", "The warehouse"))
    with co["engine"].connect() as conn:
        assert tuple(conn.execute(select(func.count()).select_from(x)).scalar()
                     for x in (*t.TABLES, m.posting, m.audit_log)) == rows


def test_the_same_write_on_books_with_the_stock_roles_is_accepted(co):
    s = stocked(co, "moving_average")
    receive(co, s, "10", "1.00")
    issue(co, s, "1")
    verify(co)


# ---------------------------------------------------------------- two warehouses on one inventory account

def test_two_warehouses_on_one_account_reconcile_and_a_value_moved_without_its_posting_is_reported(co):
    s = stocked(co, "moving_average", warehouses=("North", "South"))
    receive(co, s, "10", "2.00", wh=0)
    receive(co, s, "5", "3.00", wh=1)
    verify(co)
    with co["engine"].begin() as conn:  # a value changed with no posting (the owner role, past every check)
        conn.execute(text("ALTER TABLE stock_head DISABLE TRIGGER USER"))
        conn.execute(update(t.head).where(t.head.c.warehouse_id == s["warehouses"][1]).values(value=D("16.00")))
        conn.execute(text("ALTER TABLE stock_head ENABLE TRIGGER USER"))
    with co["engine"].connect() as conn:
        problems = stock.verify_stock(company_ctx(conn, company_id=co["company"]))
    assert any("inventory account" in p for p in problems) and any("head" in p for p in problems)


# ---------------------------------------------------------------- append only

@pytest.mark.parametrize("table", ["stock_ledger_entry", "stock_layer", "stock_layer_take", "stock_warehouse",
                                   "stock_item"])
def test_an_update_or_delete_of_a_stock_row_is_refused_by_the_database(co, table):
    s = stocked(co, "fifo")
    receive(co, s, "10", "1.00")
    issue(co, s, "4")
    for statement in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
        with co["engine"].begin() as conn, pytest.raises(DBAPIError, match="refused"):
            conn.execute(text(statement))
    verify(co)


def test_fifo_merging_and_reversal_change_no_existing_row(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 8, 1))

    def rows():
        with co["engine"].connect() as conn:
            return {tbl.name: sorted(tuple(r) for r in conn.execute(select(tbl))) for tbl in
                    (t.entry, t.layer, t.take)}
    first = rows()
    merged = receive(co, s, "50", "10.00", date(2026, 8, 2))
    second = rows()
    assert all(set(first[k]) <= set(second[k]) for k in first)  # only added
    reverse(co, "stock_receipt", merged, date(2026, 8, 3))
    assert all(set(second[k]) <= set(rows()[k]) for k in second)
    verify(co)


# ---------------------------------------------------------------- reversals, with figures

def _group_state(co, s):
    with co["engine"].connect() as conn:
        ctx = company_ctx(conn, company_id=co["company"])
        return [(g.qty, g.value) for g in stock.fifo_groups(ctx, s["item"], s["warehouses"][0])]


def _head(co, s):
    with co["engine"].connect() as conn:
        h = stock.head(company_ctx(conn, company_id=co["company"]), s["item"], s["warehouses"][0])
        inv = ledger.balance(company_ctx(conn, company_id=co["company"]), h and stock.warehouse(
            company_ctx(conn, company_id=co["company"]), s["warehouses"][0])["account_id"], date.max)
    return h["qty"], h["value"], inv


def test_reversing_the_latest_receipt_subtracts_its_own_layer(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 8, 1))
    later = receive(co, s, "30", "11.50", date(2026, 8, 2))
    reverse(co, "stock_receipt", later, date(2026, 8, 3))
    assert _head(co, s) == (D("100"), D("1000.00"), D("1000.00"))
    assert _group_state(co, s) == [(D("100"), D("1000.00")), (D("0"), D("0.00"))]  # the 11.50 layer: nothing left
    verify(co)


def test_reversing_a_receipt_merged_into_a_group_returns_the_group_to_its_other_layers(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 8, 1))
    merged = receive(co, s, "50", "10.00", date(2026, 8, 2))
    assert _group_state(co, s) == [(D("150"), D("1500.00"))]  # one group
    reverse(co, "stock_receipt", merged, date(2026, 8, 3))
    assert _head(co, s) == (D("100"), D("1000.00"), D("1000.00"))  # a give-back would leave 200 / 2000.00
    assert _group_state(co, s) == [(D("100"), D("1000.00"))]
    verify(co)


def test_a_reversed_fifo_issue_restores_its_groups_exactly(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 3, 2))
    receive(co, s, "50", "13.00", date(2026, 3, 5))
    before = _group_state(co, s)
    sold = issue(co, s, "120", date(2026, 3, 12))
    assert _group_state(co, s) != before
    reverse(co, "sales_invoice", sold, date(2026, 3, 13))
    assert _group_state(co, s) == before and _head(co, s)[:2] == (D("150"), D("1650.00"))
    verify(co)


def test_a_reversal_dated_before_the_pairs_last_movement_is_refused_at_preview(co):
    s = stocked(co, "moving_average")
    receive(co, s, "10", "1.00", date(2026, 3, 2))
    sold = issue(co, s, "4", date(2026, 3, 12))
    r = refused(co, lambda ctx: stock.preview_reverse(ctx, "sales_invoice", sold, date(2026, 3, 11)))
    assert r.code == "backdated" and r.data["last_movement"] == "2026-03-12"
    with co["engine"].connect() as conn:
        ok = stock.preview_reverse(company_ctx(conn, company_id=co["company"]), "sales_invoice", sold,
                                   date(2026, 3, 12))
    assert ok["contribution"][0]["net"] == "-4.00"
    verify(co)



# ---------------------------------------------------------------- what a refusal leaves, bounds, rows

def test_a_refused_warehouse_leaves_no_row_even_when_the_caller_carries_on_and_commits(co):
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        bank = ledger.account_by_type(ctx, "bank")["id"]
        with pytest.raises(Refusal):
            stock.create_warehouse(ctx, "Odd Store", bank, default=True)
        # the caller caught the refusal and goes on; its transaction commits
    with co["engine"].begin() as conn:
        assert conn.execute(select(func.count()).select_from(t.warehouse).where(
            t.warehouse.c.company_id == co["company"])).scalar() == 0
        assert stock.create_warehouse(ctx_of(conn, co), "Good Store", default=True)  # the default is still free


def test_an_oversized_or_inexact_quantity_is_refused_with_its_field_and_never_rounded(co):
    s = stocked(co, "moving_average")
    receive(co, s, "10", "1.00")
    for bad in ("1" + "0" * 30, "123456789012345.5", "9" * 40 + ".0001", "1e3", "1E-2", "0.00001000", "NaN",
                "Infinity", " ", "1,000"):
        r = refused(co, lambda ctx: stock.preview_issue(ctx, date(2026, 3, 12), [
            {"item": s["item"], "warehouse": s["warehouses"][0], "qty": bad}]))
        assert r.code == "bad_quantity" and r.data["field"] == "qty" and r.data["value"] == bad, bad
    assert stock.parse_qty("1.50000") == D("1.5") and str(stock.parse_qty("12345678901234")) == "12345678901234"
    assert str(stock.parse_qty("0.0001")) == "0.0001"


def test_fifo_writes_one_layer_per_receipt_and_one_take_per_group_an_issue_takes_from(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 3, 2))
    receive(co, s, "50", "13.00", date(2026, 3, 5))
    receive(co, s, "30", "11.50", date(2026, 3, 9))
    sold = issue(co, s, "120", date(2026, 3, 12))
    with co["engine"].connect() as conn:
        layers = conn.execute(select(t.layer.c.id, t.layer.c.group_id, t.layer.c.qty, t.layer.c.value).join(
            t.entry, t.entry.c.id == t.layer.c.entry_id).where(t.layer.c.item_id == s["item"]).order_by(
            t.entry.c.seq)).all()
        assert [(r.qty, r.value) for r in layers] == [(D("100"), D("1000.00")), (D("50"), D("650.00")),
                                                      (D("30"), D("345.00"))]
        assert all(r.group_id == r.id for r in layers)  # three rates: three groups
        entry = conn.execute(select(t.entry.c.id).where(t.entry.c.voucher_id == sold)).scalar_one()
        takes = conn.execute(select(t.take.c.group_id, t.take.c.qty, t.take.c.value, t.take.c.kind).where(
            t.take.c.entry_id == entry)).all()
    assert sorted(takes, key=lambda r: r.qty, reverse=True) == [(layers[0].id, D("100"), D("1000.00"), "take"),
                                                                (layers[1].id, D("20"), D("260.00"), "take")]
    verify(co)


def test_a_zero_cost_return_and_its_reversal_store_zero_and_add_no_ledger_line(co):
    s = stocked(co, "moving_average")
    receive(co, s, "1", "0.01")
    sold = issue(co, s, "0.125")
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' zero-cost return")
        out = stock.return_in(ctx, "sales_credit_note", "cn-z", date(2026, 3, 12), sold, 1, "0.125", trusted=trusted)
        assert (out["cost"], out["ledger_lines"]) == ("0.00", [])
        trusted.verify(ctx)  # no posting, and none expected
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' zero-cost reversal")
        out = stock.reverse_movements(ctx, "sales_credit_note", "cn-z", date(2026, 3, 12), "sales_credit_note_reversal",
                                      "cn-z-rev", trusted=trusted)
        assert out["contribution"] == []
        trusted.verify(ctx)
    with co["engine"].connect() as conn:
        rows = conn.execute(select(t.entry.c.value).where(t.entry.c.voucher_id.in_(["cn-z", "cn-z-rev"]))).scalars()
        assert list(rows) == [D("0.00"), D("0.00")]
        assert conn.execute(select(func.count()).select_from(m.posting).where(
            m.posting.c.voucher_id.in_(["cn-z", "cn-z-rev"]))).scalar() == 0
    verify(co)


def test_a_fifo_return_is_a_new_group_and_its_reversal_takes_that_layer_out(co):
    s = stocked(co, "fifo")
    receive(co, s, "100", "10.00", date(2026, 3, 2))
    sold = issue(co, s, "40", date(2026, 3, 12))
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' FIFO return")
        out = stock.return_in(ctx, "sales_credit_note", "cn-f", date(2026, 3, 13), sold, 1, "10", trusted=trusted)
        ledger.post(ctx, date(2026, 3, 13), "sales_credit_note", "cn-f", [
            ledger.Line(x["account"], debit=D(x["debit"]), credit=D(x["credit"])) for x in out["ledger_lines"]])
        trusted.verify(ctx)
    with co["engine"].connect() as conn:
        ret = conn.execute(select(t.entry.c.id).where(t.entry.c.voucher_id == "cn-f")).scalar_one()
        layer = conn.execute(select(t.layer).where(t.layer.c.entry_id == ret)).mappings().one()
        assert (layer["group_id"], layer["qty"], layer["value"]) == (layer["id"], D("10"), D("100.00"))  # own group
    assert _group_state(co, s) == [(D("60"), D("600.00")), (D("10"), D("100.00"))]
    with co["engine"].begin() as conn:
        ctx = ctx_of(conn, co)
        trusted = Trusted("the rules tests' FIFO return reversal")
        stock.reverse_movements(ctx, "sales_credit_note", "cn-f", date(2026, 3, 13), "sales_credit_note_reversal",
                                "cn-f-rev", trusted=trusted)
        ledger.reverse(ctx, ledger.posting_for_voucher(ctx, "sales_credit_note", "cn-f")["id"], date(2026, 3, 13))
        trusted.verify(ctx)
    with co["engine"].connect() as conn:
        mirror = conn.execute(select(t.entry.c.id).where(t.entry.c.voucher_id == "cn-f-rev")).scalar_one()
        takes = conn.execute(select(t.take.c.group_id, t.take.c.layer_id, t.take.c.qty, t.take.c.value,
                                    t.take.c.kind).where(t.take.c.entry_id == mirror)).all()
    assert takes == [(layer["id"], layer["id"], D("10"), D("100.00"), "take")]  # its own layer, once
    assert _group_state(co, s) == [(D("60"), D("600.00")), (D("0"), D("0.00"))]
    verify(co)
