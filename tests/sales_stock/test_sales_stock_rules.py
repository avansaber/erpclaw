"""The rules around the stock-backed sales documents: the item's kind, the planned stock reply when stock is off, the
warehouse questions (no stock moves between warehouses), a draft's warehouse at its submission, credit amounts
allocated cumulatively, the limits of a credit, and the model's tool unchanged. Each refusal is paired with the
acceptance of the same rule."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.masters import mark_stock_item
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_sales import tables as sales_t
from erpclaw_stock import service as stock
from erpclaw_stock import tables as st

from sales_books import Books

D = Decimal


def _kind(b: Books, item: str) -> str:
    with b.engine.connect() as conn:
        return conn.execute(select(m.item.c.kind).where(m.item.c.id == b.ids[item])).scalar_one()


def _invoices(b: Books) -> int:
    with b.engine.connect() as conn:
        return len(conn.execute(select(sales_t.invoice.c.id).where(sales_t.invoice.c.company_id == b.company)).all())


@pytest.fixture
def wired(engine):
    """Copper Wire kept in two warehouses, neither the item's default; a service item beside it."""
    b = Books(engine, "Rules Co")
    raw = b.stock_account("1131", "Raw Materials")
    b.warehouse("North", raw)
    b.warehouse("South", raw)
    b.customer("Acme Corporation")
    b.item("Copper Wire", "28.00")
    b.receive("Copper Wire", "North", "10", "20.00", date(2026, 1, 1))
    b.receive("Copper Wire", "South", "10", "22.00", date(2026, 1, 1))
    with engine.begin() as conn:
        b.ids["Consulting"] = new_id()
        conn.execute(m.item.insert().values(id=b.ids["Consulting"], company_id=b.company, name="Consulting",
                                            unit="hour", standard_rate="150.00"))
    return b


# ---------------------------------------------------------------- the kind

def test_making_a_stock_item_marks_the_core_item_stock_in_the_same_transaction(wired):
    assert _kind(wired, "Copper Wire") == "stock" and _kind(wired, "Consulting") == "service"


def test_a_stock_item_never_becomes_a_service_again(wired):
    with pytest.raises(DBAPIError, match="never becomes a service"):
        with wired.engine.begin() as conn:
            conn.execute(update(m.item).where(m.item.c.id == wired.ids["Copper Wire"]).values(kind="service"))
    with wired.engine.begin() as conn:  # marking it again is harmless
        mark_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Copper Wire"])
    assert _kind(wired, "Copper Wire") == "stock"


def test_verify_stock_reports_a_stock_item_whose_core_item_is_a_service(wired):
    with wired.engine.begin() as conn:  # a stock item row written around make_stock_item: its core kind disagrees
        conn.execute(st.stock_item.insert().values(id=new_id(), company_id=wired.company,
                                                   item_id=wired.ids["Consulting"], method="moving_average",
                                                   created_at=stock.now()))
    with wired.engine.connect() as conn:
        problems = stock.verify_stock(company_ctx(conn, company_id=wired.company))
    assert problems == [f"stock item {wired.ids['Consulting']} is a service in the core item list"]
    with wired.engine.begin() as conn:
        mark_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Consulting"])
    wired.verify()


# ---------------------------------------------------------------- a stock item while stock is off

def _off(b: Books, how: str) -> None:
    with b.engine.begin() as conn:
        if how == "disabled":
            disable_plugin(conn, "stock")
        else:  # not installed: its install row gone, as in books that never installed it
            conn.execute(m.plugin_install.delete().where(m.plugin_install.c.name == "stock"))


@pytest.mark.parametrize("how", ["disabled", "not installed"])
def test_a_stock_item_with_stock_off_gets_the_planned_reply_and_no_card(wired, how):
    _off(wired, how)
    for contract, args in (("create_invoice", dict(customer="Acme Corporation",
                                                   items=[{"item": "Copper Wire", "quantity": 1}])),
                           ("create_invoice", dict(customer="Acme Corporation", submit=False,
                                                   items=[{"item": "Copper Wire", "quantity": 1}])),
                           ("create_sales_order", dict(customer="Acme Corporation", delivery_date="2026-11-30",
                                                       items=[{"item": "Copper Wire", "quantity": 1}]))):
        with pytest.raises(Refusal) as caught:
            approvals.propose(wired.engine, REGISTRY.contracts[contract], args, company_id=wired.company)
        assert caught.value.code == "planned" and caught.value.capability == "planned.stock"
        assert caught.value.message == "Selling from stock isn't available yet."
    with wired.engine.connect() as conn:
        assert conn.execute(select(m.approval.c.id).where(m.approval.c.company_id == wired.company)).first() is None
    card, out = wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 2}])
    assert out.status == "committed", out.text  # a service item is invoiced as before


def test_the_same_stock_item_with_stock_enabled_gets_its_card(wired):
    card, out = wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 2},
                                                                               {"item": "Copper Wire", "quantity": 1,
                                                                                "warehouse": "North"}])
    assert out.status == "committed", out.text
    wired.verify()


def test_a_draft_made_before_its_item_became_stock_is_refused_at_submission(wired):
    _, draft = wired.run("create_invoice", customer="Acme Corporation", submit=False,
                         items=[{"item": "Consulting", "quantity": 1}])
    with wired.engine.begin() as conn:  # the item becomes a stock item after the draft was written
        stock.make_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Consulting"], "moving_average")
    with pytest.raises(Refusal) as caught:
        wired.run("submit_invoice", invoice=draft.data["number"])
    assert caught.value.code == "planned" and caught.value.capability == "planned.stock"


# ---------------------------------------------------------------- the warehouse, never moved between warehouses

def test_two_warehouses_holding_enough_is_the_warehouse_question_by_name(wired):
    with pytest.raises(Refusal) as caught:
        wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5}])
    q = caught.value
    assert q.code == "warehouse_ambiguous" and q.data["field"] == "warehouse"
    assert [c["ref"] for c in q.data["choices"]] == ["North", "South"]
    assert q.options == ["North (10 on hand)", "South (10 on hand)"]


def test_an_unknown_warehouse_name_asks_with_the_items_warehouses(wired):
    with pytest.raises(Refusal) as caught:
        wired.run("create_invoice", customer="Acme Corporation",
                  items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "Pune"}])
    q = caught.value
    assert q.code == "warehouse_unknown" and q.data["field"] == "warehouse" and "Pune" in q.message
    assert [c["ref"] for c in q.data["choices"]] == ["North", "South"]


def test_short_stock_is_refused_with_each_warehouses_figure(wired):
    with pytest.raises(Refusal) as caught:
        wired.run("create_invoice", customer="Acme Corporation",
                  items=[{"item": "Copper Wire", "quantity": 12, "warehouse": "North"}])
    assert caught.value.code == "insufficient_stock"
    assert caught.value.message == "Copper Wire: 10 on hand in North, 10 on hand in South; the invoice needs 12."
    with pytest.raises(Refusal) as caught:  # more than any one warehouse holds, none named
        wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 12}])
    assert caught.value.code == "insufficient_stock"


def test_a_named_warehouse_that_holds_enough_is_taken_and_nothing_moves_elsewhere(wired):
    _, out = wired.run("create_invoice", customer="Acme Corporation",
                       items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "south"}])
    assert out.status == "committed", out.text
    assert wired.head("Copper Wire", "South") == (D("5"), D("110.00"))
    assert wired.head("Copper Wire", "North") == (D("10"), D("200.00"))
    wired.verify()


# ---------------------------------------------------------------- a draft's warehouse

def _issue_rows(b: Books, number: str) -> list[tuple[str, Decimal]]:
    i, ln, inv = sales_t.invoice_issue, sales_t.invoice_line, sales_t.invoice
    with b.engine.connect() as conn:
        rows = conn.execute(select(i.c.warehouse_id, i.c.cost).join(ln, ln.c.id == i.c.invoice_line_id).join(
            inv, inv.c.id == ln.c.invoice_id).where(inv.c.number == number, inv.c.company_id == b.company)).all()
    names = {v: k for k, v in b.ids.items()}
    return [(names[r.warehouse_id], r.cost) for r in rows]


def test_a_drafts_unnamed_warehouse_is_chosen_at_submission_from_the_stock_then(engine):
    b = Books(engine, "Draft Co")
    raw = b.stock_account("1131", "Raw Materials")
    b.warehouse("Main", raw, default=True)
    b.warehouse("Annex", raw)
    b.customer("Acme Corporation")
    b.item("Copper Wire", "28.00")
    b.receive("Copper Wire", "Main", "10", "20.00", date(2026, 1, 1))
    _, draft = b.run("create_invoice", customer="Acme Corporation", submit=False,
                     items=[{"item": "Copper Wire", "quantity": 5}])
    number = draft.data["number"]
    with b.engine.connect() as conn:
        line_before = conn.execute(select(sales_t.invoice_line).where(
            sales_t.invoice_line.c.company_id == b.company)).mappings().one()
    _, other = b.run("create_invoice", customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 8}])
    b.receive("Copper Wire", "Annex", "6", "21.00", date(2026, 1, 2))  # Main holds 2 now; only Annex holds 5
    card, out = b.run("submit_invoice", invoice=number)
    assert ("From", "Annex (6 on hand)") in [tuple(x) for x in card.card["lines"]]
    assert out.status == "committed", out.text
    assert _issue_rows(b, number) == [("Annex", D("105.00"))]
    with b.engine.connect() as conn:
        assert conn.execute(select(sales_t.invoice_line).where(
            sales_t.invoice_line.c.id == line_before["id"])).mappings().one() == line_before  # never updated
    b.verify()


def test_a_drafts_named_warehouse_is_kept(wired):
    _, draft = wired.run("create_invoice", customer="Acme Corporation", submit=False,
                         items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "South"}])
    card, out = wired.run("submit_invoice", invoice=draft.data["number"])
    assert out.status == "committed", out.text
    assert _issue_rows(wired, draft.data["number"]) == [("South", D("110.00"))]


def test_an_invoice_line_is_still_never_updated(wired):
    wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 1}])
    with pytest.raises(DBAPIError):
        with wired.engine.begin() as conn:
            conn.execute(update(sales_t.invoice_line).where(sales_t.invoice_line.c.company_id == wired.company)
                         .values(named_warehouse_id=wired.ids["North"]))


# ---------------------------------------------------------------- credits

@pytest.fixture
def cheap(engine):
    """Three units at 0.3350: the line's amount is 1.01."""
    b = Books(engine, "Cheap Co")
    b.warehouse("Main", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Acme Corporation")
    b.item("Washer", "0.3350")
    b.receive("Washer", "Main", "10", "0.10", date(2026, 1, 1))
    _, inv = b.run("create_invoice", customer="Acme Corporation", items=[{"item": "Washer", "quantity": 3}])
    b.ids["invoice"] = inv.data["number"]
    return b


def _credit_amounts(b: Books) -> list[Decimal]:
    cn, cl = sales_t.credit_note, sales_t.credit_note_line
    with b.engine.connect() as conn:
        return [r.amount for r in conn.execute(select(cl.c.amount).join(cn, cn.c.id == cl.c.credit_note_id).where(
            cn.c.company_id == b.company).order_by(cn.c.number))]


def _invoice(b: Books) -> dict:
    with b.engine.connect() as conn:
        return dict(conn.execute(select(sales_t.invoice).where(sales_t.invoice.c.number == b.ids["invoice"],
                                                               sales_t.invoice.c.company_id == b.company)
                                 ).mappings().one())


def test_credits_one_unit_at_a_time_are_allocated_cumulatively(cheap):
    assert _invoice(cheap)["total"] == D("1.01")
    for _ in range(3):
        _, out = cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 1}])
        assert out.status == "committed", out.text
    assert _credit_amounts(cheap) == [D("0.34"), D("0.33"), D("0.34")]
    inv = _invoice(cheap)
    assert (inv["outstanding"], inv["status"]) == (D("0.00"), "credited")
    cheap.verify()


def test_one_credit_for_all_three_is_the_line_amount(cheap):
    _, out = cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 3}])
    assert out.status == "committed", out.text
    assert _credit_amounts(cheap) == [D("1.01")]
    assert _invoice(cheap)["status"] == "credited"


def test_over_credit_is_refused_with_both_figures_and_the_limit_is_accepted(cheap):
    cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 1}])
    with pytest.raises(Refusal) as caught:
        cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 3}])
    assert caught.value.code == "over_credit" and "2" in caught.value.message and "3" in caught.value.message
    _, out = cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 2}])
    assert out.status == "committed", out.text  # exactly what is left: two partial credits make the invoice
    assert _invoice(cheap)["status"] == "credited"


def test_a_credit_beyond_what_the_invoice_still_owes_is_refused_no_refund(engine, monkeypatch):
    b = Books(engine, "Refund Co")
    b.warehouse("Main", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Acme Corporation")
    b.item("Circuit Board", "24.99")
    b.receive("Circuit Board", "Main", "40", "15.00", date(2026, 3, 31))
    _, inv = b.run("create_invoice", customer="Acme Corporation", items=[{"item": "Circuit Board", "quantity": 12}])
    b.run("record_payment", customer="Acme Corporation", amount="100.00", invoice=inv.data["number"])
    with pytest.raises(Refusal) as caught:  # 299.88 credited, 199.88 still owed
        b.run("create_credit_note", invoice=inv.data["number"], everything=True)
    assert caught.value.code == "refund_not_available" and "a refund is not available" in caught.value.message
    with pytest.raises(Refusal) as caught:  # 8 x 24.99 = 199.92, a cent more than is owed
        b.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Circuit Board", "quantity": 8}])
    assert caught.value.code == "refund_not_available"
    _, out = b.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Circuit Board", "quantity": 7}])
    assert out.status == "committed", out.text  # 174.93 of 199.88: no payment or refund row, partly paid still
    with b.engine.connect() as conn:
        after = conn.execute(select(sales_t.invoice).where(sales_t.invoice.c.number == inv.data["number"],
                                                           sales_t.invoice.c.company_id == b.company)).mappings().one()
    assert (after["outstanding"], after["status"]) == (D("24.95"), "partly_paid")
    b.verify()


def test_reversing_the_only_payment_of_a_partly_credited_invoice_leaves_it_submitted(engine):
    """A credit lowers what an invoice owes, so an invoice owing less than its total is not therefore partly paid:
    with its one payment reversed it is submitted again, owing the total less the credit."""
    b = Books(engine, "Reversal Co")
    b.warehouse("Main", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Acme Corporation")
    b.item("Circuit Board", "24.99")
    b.receive("Circuit Board", "Main", "40", "15.00", date(2026, 3, 31))
    _, inv = b.run("create_invoice", customer="Acme Corporation", items=[{"item": "Circuit Board", "quantity": 12}])
    _, pay = b.run("record_payment", customer="Acme Corporation", amount="100.00", invoice=inv.data["number"])
    b.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Circuit Board", "quantity": 7}])
    _, out = b.run("reverse_payment", payment=pay.data["number"])
    assert out.status == "committed", out.text
    with b.engine.connect() as conn:
        after = conn.execute(select(sales_t.invoice).where(sales_t.invoice.c.number == inv.data["number"],
                                                           sales_t.invoice.c.company_id == b.company)).mappings().one()
    assert (after["outstanding"], after["status"]) == (D("124.95"), "submitted")  # 299.88 less the 174.93 credit
    b.verify()


# ---------------------------------------------------------------- the schema

def test_the_invoice_check_refuses_an_outstanding_that_disagrees_with_payments_and_credits(cheap):
    cheap.run("create_credit_note", invoice=cheap.ids["invoice"], items=[{"item": "Washer", "quantity": 1}])
    inv = _invoice(cheap)
    assert (inv["outstanding"], inv["status"]) == (D("0.67"), "submitted")  # credited in part: submitted, owing less
    with pytest.raises(DBAPIError, match="credit notes"):
        with cheap.engine.begin() as conn:
            conn.execute(update(sales_t.invoice).where(sales_t.invoice.c.id == inv["id"]).values(outstanding=D("1.01")))


# ---------------------------------------------------------------- the model's tool

def test_the_model_is_offered_create_invoice_without_the_stock_fields():
    from erpclaw_sales import OfferedInvoice

    schema = REGISTRY.contracts["create_invoice"].tool_schema()["function"]["parameters"]
    assert "order" not in schema["properties"]
    assert "warehouse" not in schema["properties"]["items"]["items"]["properties"]
    assert schema["required"] == ["customer", "items"] and schema["properties"]["items"]["minItems"] == 1
    assert set(schema["properties"]) == set(OfferedInvoice.model_fields)


# ---------------------------------------------------------------- the comparison, failing closed, and two writers

def _rows(b: Books) -> tuple:
    with b.engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(tbl)).scalar()
                     for tbl in (sales_t.invoice, sales_t.invoice_issue, st.entry, m.posting, m.ledger_entry))


@pytest.mark.parametrize("tamper", ["dropped", "altered"])
def test_a_sales_commit_whose_posting_differs_from_the_stock_lines_is_refused_whole(wired, monkeypatch, tamper):
    """A tampering wrapper changes the stock lines the invoice posts, keeping the posting balanced: the stock
    plugin's comparison refuses the whole commit, and nothing is written."""
    from erpclaw_sales import stock_sales as ss

    real = ss.issue

    def tampered(*a, **k):
        out = real(*a, **k)
        if tamper == "dropped":
            out.lines = []
        else:
            out.lines = [ln.__class__(ln.account_id, debit=ln.debit and ln.debit + D("0.01"),
                                      credit=ln.credit and ln.credit + D("0.01"), party_id=ln.party_id)
                         for ln in out.lines]
        return out

    card = approvals.propose(wired.engine, REGISTRY.contracts["create_invoice"], dict(
        customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "North"}]),
        company_id=wired.company)
    before = _rows(wired)
    monkeypatch.setattr(ss, "issue", tampered)
    out = approvals.approve(wired.engine, card.approval_id, card.digest)
    assert out.status == "refused" and out.data["code"] == "stock_posting_mismatch", out.text
    assert _rows(wired) == before
    monkeypatch.setattr(ss, "issue", real)
    wired.verify()


def test_stock_switched_off_between_the_card_and_its_approval_writes_no_invoice(wired):
    card = approvals.propose(wired.engine, REGISTRY.contracts["create_invoice"], dict(
        customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "North"}]),
        company_id=wired.company)
    before = _rows(wired)
    _off(wired, "disabled")
    out = approvals.approve(wired.engine, card.approval_id, card.digest)
    assert out.status != "committed" and out.data["code"] == "planned", out.text
    assert _rows(wired) == before  # never an invoice without its issue


def _threads(fns) -> list[BaseException]:
    """Run the functions at once, each on its own connection; none may hang (a deadlock fails the test)."""
    import threading

    errors: list[BaseException] = []
    start = threading.Barrier(len(fns))

    def wrap(fn):
        try:
            start.wait(timeout=30)
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported by the caller
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(fn,)) for fn in fns]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
        assert not th.is_alive(), "a thread is stuck: a deadlock or a lock never released"
    return errors


def test_two_invoices_taking_the_last_stock_one_commits_and_stock_is_never_negative(wired):
    cards = [approvals.propose(wired.engine, REGISTRY.contracts["create_invoice"], dict(
        customer="Acme Corporation", items=[{"item": "Copper Wire", "quantity": 6, "warehouse": "North"}]),
        company_id=wired.company) for _ in range(2)]
    outcomes: list = []
    errors = _threads([lambda c=c: outcomes.append(approvals.approve(wired.engine, c.approval_id, c.digest))
                       for c in cards])
    assert not errors, errors
    statuses = sorted(o.status for o in outcomes)
    assert statuses.count("committed") == 1 and all(s in ("committed", "superseded", "refused") for s in statuses)
    assert wired.head("Copper Wire", "North") == (D("4"), D("80.00"))
    assert _invoices(wired) == 1
    wired.verify()


# ---------------------------------------------------------------- one line from two warehouses; repeated returns

def _returns(b: Books, number: str) -> list[tuple[str, Decimal, Decimal]]:
    cn, cl, r = sales_t.credit_note, sales_t.credit_note_line, sales_t.credit_note_return
    names = {v: k for k, v in b.ids.items()}
    with b.engine.connect() as conn:
        rows = conn.execute(select(r.c.warehouse_id, r.c.quantity, r.c.cost).join(
            cl, cl.c.id == r.c.credit_note_line_id).join(cn, cn.c.id == cl.c.credit_note_id).where(
            cn.c.number == number, cn.c.company_id == b.company).order_by(cl.c.line_no, r.c.warehouse_id)).all()
    return [(names[x.warehouse_id], x.quantity, x.cost) for x in rows]


def test_an_order_line_delivered_from_two_warehouses_keeps_each_part_and_comes_back_to_each(wired):
    _, so = wired.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                      items=[{"item": "Copper Wire", "quantity": 4}])
    card, out = wired.run("deliver_order", order=so.data["number"], items=[
        {"item": "Copper Wire", "quantity": 2, "warehouse": "North"},
        {"item": "Copper Wire", "quantity": 2, "warehouse": "South"}])
    assert out.status == "committed", out.text
    froms = [v for k, v in card.card["lines"] if k == "From"]
    assert froms == ["North (10 on hand)", "South (10 on hand)"]
    assert wired.head("Copper Wire", "North") == (D("8"), D("160.00"))
    assert wired.head("Copper Wire", "South") == (D("8"), D("176.00"))
    _, inv = wired.run("create_invoice", customer="Acme Corporation", order=so.data["number"])
    _, cn = wired.run("create_credit_note", invoice=inv.data["number"], everything=True)
    assert cn.status == "committed", cn.text
    assert sorted(_returns(wired, cn.data["number"])) == [("North", D("2"), D("40.00")), ("South", D("2"), D("44.00"))]
    assert wired.head("Copper Wire", "North") == (D("10"), D("200.00"))
    assert wired.head("Copper Wire", "South") == (D("10"), D("220.00"))
    wired.verify()


def test_two_credit_lines_against_one_issue_post_exactly_the_cost_the_card_shows(engine):
    """Three washers issued for 1.01 in all: 1 + 1 back on two lines of one credit note comes back cumulatively,
    0.34 then 0.33, and the card shows what posts, never 0.34 twice."""
    b = Books(engine, "Repeat Co")
    b.warehouse("Main", b.stock_account("1131", "Raw Materials"), default=True)
    b.customer("Acme Corporation")
    b.item("Washer", "2.00")
    b.receive("Washer", "Main", "3", "0.3367", date(2026, 1, 1))  # 1.01 for three
    _, inv = b.run("create_invoice", customer="Acme Corporation", items=[{"item": "Washer", "quantity": 3}])
    card, out = b.run("create_credit_note", invoice=inv.data["number"],
                      items=[{"item": "Washer", "quantity": 1}, {"item": "Washer", "quantity": 1}])
    assert out.status == "committed", out.text
    shown = [D(v.rsplit(" at ", 1)[1]) for k, v in card.card["lines"] if k == "Back into stock"]
    with b.engine.connect() as conn:
        cn = conn.execute(select(sales_t.credit_note.c.id).where(sales_t.credit_note.c.number == out.data["number"],
                                                                 sales_t.credit_note.c.company_id == b.company)
                          ).scalar_one()
    posted = [r for r in b.posting("sales_credit_note", cn) if r[0] == "1131"]
    assert sum(shown) == posted[0][2] == D("0.67"), (shown, posted)  # 0.34 + 0.33, never 0.34 + 0.34
    assert [c for *_, c in _returns(b, out.data["number"])] == shown
    b.verify()


def test_an_invoice_for_an_order_is_refused_when_stock_is_switched_off_before_approval(wired):
    _, so = wired.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                      items=[{"item": "Copper Wire", "quantity": 2}])
    wired.run("deliver_order", order=so.data["number"], items=[{"item": "Copper Wire", "quantity": 2,
                                                               "warehouse": "North"}])
    card = approvals.propose(wired.engine, REGISTRY.contracts["create_invoice"], dict(
        customer="Acme Corporation", order=so.data["number"]), company_id=wired.company)
    before = _invoices(wired)
    _off(wired, "disabled")
    out = approvals.approve(wired.engine, card.approval_id, card.digest)
    assert out.status != "committed" and out.data["code"] == "planned", out.text
    assert _invoices(wired) == before


def test_a_service_sale_of_an_item_that_later_became_stock_is_credited_without_stock(wired):
    _, inv = wired.run("create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 2}])
    with wired.engine.begin() as conn:  # the item is kept in stock from now on
        stock.make_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Consulting"], "moving_average")
    _, out = wired.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Consulting", "quantity": 1}])
    assert out.status == "committed", out.text  # sold as a service: credited as one, nothing comes into stock
    assert _returns(wired, out.data["number"]) == []
    _off(wired, "disabled")  # and with stock off too: that line never moved stock
    _, again = wired.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Consulting", "quantity": 1}])
    assert again.status == "committed", again.text


def test_an_order_delivered_as_a_service_then_made_stock_is_invoiced_and_credited_without_stock(wired):
    """The order's delivery moved no stock, so neither does its credit, though the item is a stock item by then."""
    _, so = wired.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                      items=[{"item": "Consulting", "quantity": 2}])
    wired.run("deliver_order", order=so.data["number"])
    with wired.engine.begin() as conn:  # the item is kept in stock from now on
        stock.make_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Consulting"], "moving_average")
    _, inv = wired.run("create_invoice", customer="Acme Corporation", order=so.data["number"])
    assert inv.status == "committed", inv.text
    _, out = wired.run("create_credit_note", invoice=inv.data["number"], items=[{"item": "Consulting", "quantity": 1}])
    assert out.status == "committed", out.text
    assert _returns(wired, out.data["number"]) == []


@pytest.fixture
def mixed(wired):
    """One order line of 5 Consulting: 3 delivered while it was a service (no stock moved), then made a stock item and
    the other 2 delivered from North; then the order invoiced in one go."""
    _, so = wired.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                      items=[{"item": "Consulting", "quantity": 5}])
    wired.run("deliver_order", order=so.data["number"], items=[{"item": "Consulting", "quantity": 3}])
    with wired.engine.begin() as conn:
        stock.make_stock_item(company_ctx(conn, company_id=wired.company), wired.ids["Consulting"], "moving_average")
    wired.receive("Consulting", "North", "10", "40.00", date(2026, 1, 2))
    wired.run("deliver_order", order=so.data["number"], items=[{"item": "Consulting", "quantity": 2,
                                                               "warehouse": "North"}])
    _, inv = wired.run("create_invoice", customer="Acme Corporation", order=so.data["number"])
    assert inv.status == "committed", inv.text
    wired.ids["invoice"] = inv.data["number"]
    return wired


def test_an_order_line_delivered_partly_as_a_service_is_invoiced_as_one_line_per_kind(mixed):
    with mixed.engine.connect() as conn:
        inv = conn.execute(select(sales_t.invoice.c.id).where(sales_t.invoice.c.number == mixed.ids["invoice"],
                                                              sales_t.invoice.c.company_id == mixed.company)).scalar_one()
        lines = conn.execute(select(sales_t.invoice_line.c.quantity, sales_t.invoice_line.c.item_kind).where(
            sales_t.invoice_line.c.invoice_id == inv).order_by(sales_t.invoice_line.c.line_no)).all()
    assert [(q, k) for q, k in lines] == [(D("3"), "service"), (D("2"), "stock")]  # each kind true for its quantity


def test_crediting_all_of_a_mixed_order_line_brings_back_only_what_moved(mixed):
    _, out = mixed.run("create_credit_note", invoice=mixed.ids["invoice"], everything=True)
    assert out.status == "committed", out.text
    assert _returns(mixed, out.data["number"]) == [("North", D("2"), D("80.00"))]  # never the 3 that never left
    assert mixed.head("Consulting", "North") == (D("10"), D("400.00"))
    mixed.verify()


def test_crediting_just_the_service_units_of_a_mixed_order_line_moves_no_stock(mixed):
    _, out = mixed.run("create_credit_note", invoice=mixed.ids["invoice"],
                       items=[{"item": "Consulting", "quantity": 3, "line": 1}])
    assert out.status == "committed", out.text
    assert _returns(mixed, out.data["number"]) == []
    assert mixed.head("Consulting", "North") == (D("8"), D("320.00"))


def _invoice_lines(b: Books, numbers: list[str]) -> list[tuple[str, Decimal, Decimal, Decimal, Decimal]]:
    """(invoice number, quantity, rate, amount, the invoice's total) of each line, in order."""
    i, ln = sales_t.invoice, sales_t.invoice_line
    with b.engine.connect() as conn:
        return [(r.number, r.quantity, r.rate, r.amount, r.total) for r in conn.execute(
            select(i.c.number, i.c.total, ln.c.quantity, ln.c.rate, ln.c.amount).join(i, i.c.id == ln.c.invoice_id)
            .where(i.c.company_id == b.company, i.c.number.in_(numbers)).order_by(i.c.number, ln.c.line_no))]


def _bills_each_unit_once(rows, delivered: Decimal) -> None:
    """No unit billed twice or missed; each line its quantity x rate (core's rounding); each invoice its lines' sum."""
    from erpclaw_core.money import quantize
    assert sum((q for _, q, _, _, _ in rows), D(0)) == delivered
    assert all(a == quantize(q * r, "USD") for _, q, r, a, _ in rows), rows
    for number in {n for n, *_ in rows}:
        mine = [x for x in rows if x[0] == number]
        assert sum((a for _, _, _, a, _ in mine), D(0)) == mine[0][4]


def test_a_mixed_order_line_and_partial_invoices_bill_each_unit_once_at_quantity_times_rate(mixed):
    """Every unit of an order billed exactly once, across the split lines and across its invoices, each line reading
    true on its own as quantity x rate. Rounding per line is by design: three invoices of one unit at 0.3350 bill 0.34
    each, 1.02 in all, against the order line's own 1.01 (an order posts nothing; its invoices do)."""
    _bills_each_unit_once(_invoice_lines(mixed, [mixed.ids["invoice"]]), D("5"))
    with mixed.engine.begin() as conn:
        mixed.ids["Washer"] = new_id()
        conn.execute(m.item.insert().values(id=mixed.ids["Washer"], company_id=mixed.company, name="Washer",
                                            unit="unit", standard_rate="0.3350"))
    _, so = mixed.run("create_sales_order", customer="Acme Corporation", delivery_date="2026-11-30",
                      items=[{"item": "Washer", "quantity": 3}])
    numbers = []
    for _ in range(3):
        mixed.run("deliver_order", order=so.data["number"], items=[{"item": "Washer", "quantity": 1}])
        _, inv = mixed.run("create_invoice", customer="Acme Corporation", order=so.data["number"])
        numbers.append(inv.data["number"])
    rows = _invoice_lines(mixed, numbers)
    _bills_each_unit_once(rows, D("3"))
    assert [a for *_, a, _ in rows] == [D("0.34")] * 3
    with mixed.engine.connect() as conn:
        line = conn.execute(select(sales_t.order_line.c.amount).join(
            sales_t.order, sales_t.order.c.id == sales_t.order_line.c.order_id).where(
            sales_t.order.c.number == so.data["number"], sales_t.order.c.company_id == mixed.company)).scalar_one()
    assert (line, sum(a for *_, a, _ in rows)) == (D("1.01"), D("1.02"))  # a cent apart, by design
