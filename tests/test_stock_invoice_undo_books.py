"""Sales owns the atomic reversal of its invoice and its own stock issues."""
from datetime import date
from decimal import Decimal as D
import json

import pytest
from sqlalchemy import select, update

from bookdiff import changes, snapshot
from erpclaw_core import approval_scope, approvals, ledger, models as m
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as sales, tables as t
from erpclaw_stock import service as stock, tables as st
from sales_stock.sales_books import Books


@pytest.fixture
def books(engine):
    b = Books(engine, "Stark Manufacturing Inc.")
    raw = b.stock_account("1131", "Raw Materials")
    b.warehouse("Stark Manufacturing Inc. - Main Warehouse (the company's default)", raw, default=True)
    b.warehouse("Raw Materials Store", raw)
    b.warehouse("Finished Goods Store", raw)
    b.customer("Acme Corporation")
    b.item("Copper Wire", "28.00", default_warehouse=b.ids["Raw Materials Store"])
    with engine.begin() as conn:
        from erpclaw_core.common import new_id
        b.ids["Consulting"] = new_id()
        conn.execute(m.item.insert().values(id=b.ids["Consulting"], company_id=b.company,
                                            name="Consulting", unit="hour", standard_rate="50.00"))
        conn.execute(update(m.account).where(m.account.c.company_id == b.company,
                    m.account.c.account_type == "cost_of_goods_sold").values(name="COGS - Materials"))
        conn.execute(update(m.account).where(m.account.c.company_id == b.company,
                    m.account.c.account_type == "receivable").values(name="Trade Receivables"))
        conn.execute(update(m.account).where(m.account.c.company_id == b.company,
                    m.account.c.number == "4100").values(name="Sales Revenue"))
        conn.execute(update(m.item).where(m.item.c.id == b.ids["Copper Wire"]).values(unit="Meter"))
    b.receive("Copper Wire", "Raw Materials Store", "200", "28.00", date(2026, 10, 25))
    return b


def invoice(b, items=None, **kw):
    _, out = b.run("create_invoice", customer="Acme Corporation",
                   items=items or [{"item": "Copper Wire", "quantity": 5}], **kw)
    assert out.status == "committed", out
    return out.data["number"]


def invrow(b, number):
    with b.engine.connect() as conn:
        return dict(conn.execute(select(t.invoice).where(t.invoice.c.company_id == b.company,
                                  t.invoice.c.number == number)).mappings().one())


def propose(b, number, **kw):
    return approvals.propose(b.engine, REGISTRY.contracts["cancel_invoice"],
                             {"invoice": number, **kw}, company_id=b.company)


def cancel(b, number, **kw):
    c = propose(b, number, **kw)
    return approvals.approve(b.engine, c.approval_id, c.digest)


def business(before, after):
    return {k: v for k, v in changes(before, after).items()
            if k not in {"approval", "operation_outcome", "audit_log"}}


def mirrors(b, number):
    with b.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(st.entry).where(
            st.entry.c.voucher_id == invrow(b, number)["id"],
            st.entry.c.reverses_entry_id.is_not(None)).order_by(st.entry.c.seq)).mappings()]


def test_stock_invoice_cancel_returns_moving_average_exactly(books):
    b = books
    number = invoice(b)
    assert b.head("Copper Wire", "Raw Materials Store") == (D(195), D("5460.00"))
    out = cancel(b, number)
    assert out.status == "committed", out
    assert out.text == f"Cancelled {number} (140.00 USD), reversed on 26 Oct 2026; the original entries are kept."
    assert (invrow(b, number)["status"], invrow(b, number)["outstanding"]) == ("cancelled", D(0))
    assert b.head("Copper Wire", "Raw Materials Store") == (D(200), D("5600.00"))
    for name in ("Stark Manufacturing Inc. - Main Warehouse (the company's default)", "Finished Goods Store"):
        assert b.head("Copper Wire", name) == (D(0), D(0))
    b.verify()


def test_service_invoice_cancel_is_ledger_only(books, monkeypatch):
    b = books
    numbers = [invoice(b, [{"item": "Consulting", "quantity": 2}]) for _ in range(2)]
    assert cancel(b, numbers[0]).status == "committed"
    with b.engine.begin() as conn:
        stock.make_stock_item(b.ctx(conn), b.ids["Consulting"], "moving_average")
    from erpclaw_sales import stock_sales as ss
    def forbidden(*a, **kw):
        pytest.fail("a historical service invoice must not call stock")
    monkeypatch.setattr(ss, "stock", forbidden)
    before = snapshot(b.engine)
    assert cancel(b, numbers[1]).status == "committed"
    assert not any(k.startswith("stock_") for k in business(before, snapshot(b.engine)))


def test_mixed_invoice_reverses_only_issued_lines(books):
    b = books
    number = invoice(b, [{"item": "Copper Wire", "quantity": 5}, {"item": "Consulting", "quantity": 2}])
    assert cancel(b, number).status == "committed"
    rows = mirrors(b, number)
    assert [(r["item_id"], r["warehouse_id"], r["qty"], r["value"]) for r in rows] == [
        (b.ids["Copper Wire"], b.ids["Raw Materials Store"], D(5), D("140.00"))]
    b.verify()


def test_delivery_backed_invoice_is_ledger_only_with_sentence(books):
    b = books
    _, order = b.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                     items=[{"item": "Copper Wire", "quantity": 5}])
    _, delivery = b.run("deliver_order", order=order.data["number"])
    assert delivery.status == "committed", delivery
    _, out = b.run("create_invoice", customer="Acme Corporation", order=order.data["number"])
    assert out.status == "committed", out
    number = out.data["number"]
    c = propose(b, number)
    assert "The stock left through the delivery and stays there; this card reverses only the invoice's books." in c.card["books"]
    before = snapshot(b.engine)
    assert approvals.approve(b.engine, c.approval_id, c.digest).status == "committed"
    assert not any(k.startswith("stock_") for k in business(before, snapshot(b.engine)))
    assert b.head("Copper Wire", "Raw Materials Store") == (D(195), D("5460.00"))
    b.verify()


@pytest.mark.parametrize("state", ["disabled", "missing"])
def test_stock_invoice_with_stock_disabled_refuses_planned(books, state):
    b = books
    number = invoice(b)
    with b.engine.begin() as conn:
        q = m.plugin_install.c.name == "stock"
        if state == "disabled":
            conn.execute(m.plugin_install.update().where(q).values(status="disabled"))
        else:
            conn.execute(m.plugin_install.delete().where(q))
    before = snapshot(b.engine)
    with pytest.raises(Refusal) as caught:
        propose(b, number)
    assert (caught.value.code, caught.value.capability, str(caught.value)) == (
        "planned", "planned.stock", "Selling from stock isn't available yet.")
    assert snapshot(b.engine) == before


def test_combined_card_shows_stock_and_ledger(books):
    b = books
    number = invoice(b)
    c = propose(b, number)
    assert c.card["title"] == "Cancel a sales invoice"
    assert ["Copper Wire", "Raw Materials Store: restore 5 Meter, value 140.00 USD; afterward 200 Meter, value 5,600.00 USD"] in c.card["lines"]
    text = json.dumps(c.card)
    for name in ("Trade Receivables", "Acme Corporation", "Sales Revenue", "COGS - Materials", "Raw Materials"):
        assert name in text
    for key in b.ids.values():
        assert key not in text
    assert {"140.00", "5600.00"} <= {a["value"] for a in c.card["amounts"]}
    with b.engine.connect() as conn:
        rs = json.loads(conn.execute(select(m.approval.c.read_set_json).where(m.approval.c.id == c.approval_id)).scalar_one())
        head = stock.head(b.ctx(conn), b.ids["Copper Wire"], b.ids["Raw Materials Store"])
    assert ("stock_head", head["id"]) in {(r[0], r[1]) for r in rs}


def test_cancel_atomic_rolls_back_ledger_refusal(books, monkeypatch):
    b = books
    number = invoice(b)
    seen = []
    def refuse(ctx, posting_id, when):
        rows = ctx.conn.execute(select(st.entry).where(st.entry.c.voucher_id == invrow(b, number)["id"],
                               st.entry.c.reverses_entry_id.is_not(None))).mappings().all()
        assert len(rows) == 1 and rows[0]["qty"] == D(5)
        seen.append(True)
        raise Refusal("ledger refused in the test", "unbalanced")
    monkeypatch.setattr(ledger, "reverse", refuse)
    before = snapshot(b.engine)
    out = cancel(b, number)
    assert seen and out.status == "refused" and out.data["code"] == "unbalanced", out
    assert "Nothing was saved" in out.text
    assert business(before, snapshot(b.engine)) == {}
    assert invrow(b, number)["status"] == "submitted"
    with b.engine.connect() as conn:
        row = conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == out.operation_key)).mappings().one()
    assert row["status"] == "refused"


def test_cancel_atomic_rolls_back_comparison(books, monkeypatch):
    b = books
    number = invoice(b)
    real = ledger.post
    seen = []
    def tamper(ctx, when, vtype, vid, lines, **kw):
        if vtype == "sales_invoice:reversal":
            lines = [ledger.Line(ln.account_id, debit=ln.debit + (D(1) if ln.debit else D(0)),
                                 credit=ln.credit + (D(1) if ln.credit else D(0)), party_id=ln.party_id) for ln in lines]
            seen.append(True)
        return real(ctx, when, vtype, vid, lines, **kw)
    monkeypatch.setattr(ledger, "post", tamper)
    before = snapshot(b.engine)
    out = cancel(b, number)
    assert seen and out.status == "refused" and out.data["code"] == "stock_posting_mismatch", out
    assert "Nothing was saved" in out.text
    assert business(before, snapshot(b.engine)) == {}
    assert invrow(b, number)["status"] == "submitted"


def test_cancel_same_card_replay_returns_stored_outcome(books):
    b = books
    c = propose(b, invoice(b))
    first = approvals.approve(b.engine, c.approval_id, c.digest)
    assert first.status == "committed", first
    before = snapshot(b.engine)
    second = approvals.approve(b.engine, c.approval_id, c.digest)
    assert second.status == "committed", second
    for attr in ("text", "data", "figures", "amounts", "operation_key"):
        assert getattr(first, attr) == getattr(second, attr)
    assert snapshot(b.engine) == before


def test_later_movement_supersedes_card(books):
    b = books
    c = propose(b, invoice(b))
    b.receive("Copper Wire", "Raw Materials Store", "5", "7.00", date(2026, 10, 26))
    before = snapshot(b.engine)
    out = approvals.approve(b.engine, c.approval_id, c.digest)
    assert out.status == "superseded" and out.data["code"] == "not_latest", out
    assert "credit note" in out.text
    assert business(before, snapshot(b.engine)) == {}


def test_stale_read_set_refuses_write_service(books, monkeypatch):
    """Omit a head after core's digest comparison, while the voucher is still latest."""
    b = books
    number = invoice(b)
    c = propose(b, number)
    inv = invrow(b, number)
    reached = []
    def direct(ctx, inp, key):
        stored = json.loads(ctx.conn.execute(select(m.approval.c.read_set_json).where(
            m.approval.c.id == c.approval_id)).scalar_one())
        ctx.conn.execute(m.approval.update().where(m.approval.c.id == c.approval_id).values(
            read_set_json=json.dumps([r for r in stored if r[0] != "stock_head"])))
        reached.append(True)
        stock.reverse_movements(ctx, "sales_invoice", inv["id"], ctx.today,
                                "sales_invoice_reversal", inv["id"])
        ledger.reverse(ctx, ledger.posting_for_voucher(ctx, "sales_invoice", inv["id"])["id"], ctx.today)
        pytest.fail("the independent write service must refuse stale")
    monkeypatch.setattr(REGISTRY.contracts["cancel_invoice"], "commit",
                        approval_scope._wrap_contract(direct, "sales", "cancel_invoice", "commit"))
    before = snapshot(b.engine)
    out = approvals.approve(b.engine, c.approval_id, c.digest)
    assert reached and out.status == "refused" and out.data["code"] == "stale", out
    assert business(before, snapshot(b.engine)) == {}


def test_write_service_not_latest_refuses(books, monkeypatch):
    """An unrelated authorized sales card reaches the service independently of cancel preview."""
    b = books
    number = invoice(b)
    inv = invrow(b, number)
    b.receive("Copper Wire", "Raw Materials Store", "5", "7.00", date(2026, 10, 26))
    contract = REGISTRY.contracts["create_invoice"]
    c = approvals.propose(b.engine, contract, {"customer": "Acme Corporation",
        "items": [{"item": "Copper Wire", "quantity": 1}]}, company_id=b.company)
    reached = []
    def direct(ctx, inp, key):
        reached.append(True)
        stock.reverse_movements(ctx, "sales_invoice", inv["id"], ctx.today,
                                "sales_invoice_reversal", inv["id"])
        pytest.fail("the independent write service must refuse not_latest")
    monkeypatch.setattr(contract, "commit",
                        approval_scope._wrap_contract(direct, "sales", "create_invoice", "commit"))
    before = snapshot(b.engine)
    out = approvals.approve(b.engine, c.approval_id, c.digest)
    assert reached and out.status == "refused" and out.data["code"] == "not_latest", out
    assert business(before, snapshot(b.engine)) == {}


def test_cancel_twice_refuses_already_cancelled(books):
    b = books
    number = invoice(b)
    assert cancel(b, number).status == "committed"
    before = snapshot(b.engine)
    with pytest.raises(Refusal) as caught:
        propose(b, number)
    assert caught.value.code == "already_cancelled"
    assert snapshot(b.engine) == before


def test_cancel_with_payment_standing_names_the_payment(books):
    b = books
    number = invoice(b)
    _, pay = b.run("record_payment", customer="Acme Corporation", invoice=number, amount="50.00")
    assert pay.status == "committed", pay
    before = snapshot(b.engine)
    with pytest.raises(Refusal) as caught:
        propose(b, number)
    assert caught.value.code == "has_payments" and pay.data["number"] in str(caught.value)
    assert snapshot(b.engine) == before


def test_cancel_draft_writes_nothing(books):
    b = books
    number = invoice(b, submit=False)
    before = snapshot(b.engine)
    out = cancel(b, number, date="2099-01-01")
    assert out.status == "committed", out
    assert out.text == f"Cancelled draft {number} (140.00 USD); the books did not change."
    assert invrow(b, number)["status"] == "cancelled"
    delta = business(before, snapshot(b.engine))
    assert set(delta) == {"sales_invoice"}
    assert len(delta["sales_invoice"]["changed"]) == 1


def test_cancel_restores_fifo_layers_exactly(engine):
    b = Books(engine)
    b.warehouse("Main", default=True)
    b.customer("Acme Corporation")
    b.item("Copper Wire", "28.00", method="fifo")
    b.receive("Copper Wire", "Main", "60", "6.25", date(2026, 10, 20))
    b.receive("Copper Wire", "Main", "50", "6.70", date(2026, 10, 21))
    layers = snapshot(engine)["stock_layer"]
    number = invoice(b, [{"item": "Copper Wire", "quantity": 40}, {"item": "Copper Wire", "quantity": 30}])
    before = snapshot(engine)
    assert cancel(b, number).status == "committed"
    after = snapshot(engine)
    rows = mirrors(b, number)
    assert [(r["qty"], r["value"], r["qty_after"], r["value_after"]) for r in rows] == [
        (D(30), D(192), D(70), D(460)), (D(40), D(250), D(110), D(710))]
    assert b.head("Copper Wire", "Main") == (D(110), D(710))
    assert after["stock_layer"] == layers
    takes = [r for r in before["stock_layer_take"].values() if r["company_id"] == b.company]
    backs = changes(before, after)["stock_layer_take"]["added"]
    assert sorted((r["group_id"], r["qty"], r["value"]) for r in takes) == sorted(
        (r["group_id"], r["qty"], r["value"]) for r in backs)
    assert all(r["kind"] == "give_back" for r in backs)
    for mirror in rows:
        original_takes = [(r["group_id"], r["qty"], r["value"]) for r in takes
                          if r["entry_id"] == mirror["reverses_entry_id"]]
        restored = [(r["group_id"], r["qty"], r["value"]) for r in backs if r["entry_id"] == mirror["id"]]
        assert sorted(restored) == sorted(original_takes)
    assert sorted((d, c) for _, _, d, c in b.posting("sales_invoice:reversal", invrow(b, number)["id"])) == [
        (D(0), D(442)), (D(0), D(1960)), (D(442), D(0)), (D(1960), D(0))]
    assert set(business(before, after)) == {"sales_invoice", "posting", "ledger_entry", "posting_chain_head",
                                            "stock_head", "stock_ledger_entry", "stock_layer_take"}
    b.verify()


def test_reversing_rows_are_exact_mirror(books):
    b = books
    number = invoice(b)
    before = snapshot(b.engine)
    assert cancel(b, number).status == "committed"
    after = snapshot(b.engine)
    delta = business(before, after)
    assert set(delta) == {"sales_invoice", "posting", "ledger_entry", "posting_chain_head", "stock_head", "stock_ledger_entry"}
    assert {k: (len(v["added"]), len(v["removed"]), len(v["changed"])) for k, v in delta.items()} == {
        "sales_invoice": (0, 0, 1), "posting": (1, 0, 0), "ledger_entry": (4, 0, 0),
        "posting_chain_head": (0, 0, 1), "stock_head": (0, 0, 1), "stock_ledger_entry": (1, 0, 0)}
    original = next(r for r in before["posting"].values() if r["voucher_id"] == invrow(b, number)["id"])
    rev = delta["posting"]["added"][0]
    assert rev["reverses_posting_id"] == original["id"]
    with b.engine.connect() as conn:
        rows = conn.execute(select(m.account.c.name, m.account.c.account_type, m.party.c.name.label("party"),
            m.ledger_entry.c.debit, m.ledger_entry.c.credit).select_from(m.ledger_entry).join(
            m.account, m.account.c.id == m.ledger_entry.c.account_id).outerjoin(
            m.party, m.party.c.id == m.ledger_entry.c.party_id).where(m.ledger_entry.c.posting_id == rev["id"])).all()
    assert sorted(tuple(r) for r in rows) == sorted([
        ("Trade Receivables", "receivable", "Acme Corporation", D(0), D(140)),
        ("Sales Revenue", "income", None, D(140), D(0)),
        ("COGS - Materials", "cost_of_goods_sold", None, D(0), D(140)),
        ("Raw Materials", "stock", None, D(140), D(0))])
    row = mirrors(b, number)[0]
    assert row["posting_date"] == date(2026, 10, 26)
    assert row["reverses_entry_id"] in {r["id"] for r in before["stock_ledger_entry"].values()}
    b.verify()


def test_cancel_explicit_date_uses_named_date_with_warning(books, monkeypatch):
    b = books
    number = invoice(b)
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-11-15")
    with b.engine.connect() as conn:
        assert ledger.find_period(b.ctx(conn), date(2026, 11, 12))["status"] == "open"
    c = approvals.propose(b.engine, REGISTRY.contracts["cancel_invoice"], {"invoice": number, "date": "2026-11-12"},
        company_id=b.company, date_source={"phrase": "12 November 2026", "date": "2026-11-12"},
        owner_words=f"cancel {number} on 12 November 2026")
    assert c.card["date_source"] == {"phrase": "12 November 2026", "date": "2026-11-12"}
    assert c.card["warnings"] == [f"{number} stays in the figures of October 2026; the reversal is dated 12 Nov 2026. To take it out of October 2026 instead, give a date in that month."]
    before = snapshot(b.engine)
    out = approvals.approve(b.engine, c.approval_id, c.digest)
    assert out.status == "committed", out
    delta = business(before, snapshot(b.engine))
    assert set(delta) == {"sales_invoice", "posting", "ledger_entry", "posting_chain_head", "stock_head", "stock_ledger_entry"}
    assert len(delta["posting"]["added"]) == 1 and len(delta["ledger_entry"]["added"]) == 4
    assert delta["posting"]["added"][0]["reverses_posting_id"] in {r["id"] for r in before["posting"].values()}
    with b.engine.connect() as conn:
        p = conn.execute(select(m.posting).where(m.posting.c.voucher_id == invrow(b, number)["id"],
            m.posting.c.reverses_posting_id.is_not(None))).mappings().one()
    assert p["posting_date"] == mirrors(b, number)[0]["posting_date"] == date(2026, 11, 12)
    assert "12 Nov 2026" in out.text
