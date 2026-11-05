"""Cancelling an invoice and reversing a payment: a reversal posting dated when the owner says, never an edit; an
invoice with money still applied is not cancelled; and the database checks every amount and status a reversal moves
back against the rows that justify it."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as s
from erpclaw_sales import tables as t

pytestmark = pytest.mark.usefixtures("postgresql_only")


@pytest.fixture
def postgresql_only(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("the books run on PostgreSQL only")


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def card(engine, contract, **args):
    return approvals.propose(engine, REGISTRY.contracts[contract], args)


def commit(engine, contract, **args):
    c = card(engine, contract, **args)
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert out.status == "committed", out.text
    return c, out


def refused(engine, contract, **args) -> Refusal:
    before = count(engine, m.approval), count(engine, m.posting)
    with pytest.raises(Refusal) as e:
        card(engine, contract, **args)
    assert (count(engine, m.approval), count(engine, m.posting)) == before
    return e.value


def today(engine) -> date:
    with engine.connect() as conn:
        return company_ctx(conn).today


def invoice(engine, number=None, *, qty="10", when=None, customer="Hooli", submit=True) -> dict:
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if number is None:
            cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
            item = dict(conn.execute(select(m.item).where(m.item.c.name == "Copper Wire")).mappings().one())
            number = s.create_invoice(ctx, cust, [(item, Decimal(qty))], when or ctx.today, submit=submit)["number"]
        return dict(conn.execute(select(t.invoice).where(t.invoice.c.number == number)).mappings().one())


def pay(engine, amount, *, number=None, oldest=True, customer="Hooli") -> dict:
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
        inv = s.find_invoice(ctx, number) if number else None
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal(amount), ctx.today, invoice=inv,
                                                 oldest_first=oldest))
        return dict(conn.execute(select(t.payment).where(t.payment.c.id == r["id"])).mappings().one())


def posting(engine, voucher_type, voucher_id):
    with engine.connect() as conn:
        p = conn.execute(select(m.posting).where(m.posting.c.voucher_type == voucher_type,
                                                 m.posting.c.voucher_id == voucher_id)).mappings().one()
        lines = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == p["id"]).order_by(
            m.ledger_entry.c.line_no)).mappings().all()
        return dict(p), [dict(x) for x in lines]


@pytest.fixture(autouse=True)
def invariants(engine):
    yield
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        for inv in conn.execute(select(t.invoice)).mappings():
            live = sum((Decimal(a) for a in conn.execute(select(t.allocation.c.amount).join(
                t.payment, t.payment.c.id == t.allocation.c.payment_id).where(
                t.allocation.c.invoice_id == inv["id"], t.payment.c.status == "submitted")).scalars()), Decimal(0))
            if inv["status"] in ("submitted", "partly_paid", "paid"):
                assert Decimal(inv["outstanding"]) == Decimal(inv["total"]) - live, inv["number"]
            if inv["status"] == "cancelled":
                assert Decimal(inv["outstanding"]) == 0 and live == 0, inv["number"]
        for p in conn.execute(select(t.payment)).mappings():
            n = conn.execute(select(func.count()).select_from(m.posting).where(
                m.posting.c.voucher_type == "sales_payment:reversal", m.posting.c.voucher_id == p["id"])).scalar_one()
            assert n == (1 if p["status"] == "cancelled" else 0), p["number"]


# ---------- cancelling an invoice ----------

def test_cancelling_a_draft_changes_no_books(engine):
    inv = invoice(engine, submit=False)
    before = count(engine, m.posting)
    c, out = commit(engine, "cancel_invoice", invoice=inv["number"])
    assert c.card["books"] == ["Nothing in the books changes; it was a draft."]
    assert count(engine, m.posting) == before and invoice(engine, inv["number"])["status"] == "cancelled"
    assert out.text == f"Cancelled draft {inv['number']} (280.00 USD); the books did not change."


def test_cancelling_a_submitted_invoice_writes_one_mirror_and_leaves_the_original_untouched(engine):
    inv = invoice(engine)
    orig, orig_lines = posting(engine, "sales_invoice", inv["id"])
    c, out = commit(engine, "cancel_invoice", invoice=inv["number"])
    assert c.card["books"][:2] == ["Accounts Receivable (Hooli) -280.00", "Sales (4100 Sales) +280.00"]
    again, again_lines = posting(engine, "sales_invoice", inv["id"])
    assert (again, again_lines) == (orig, orig_lines)  # byte for byte
    rev, rev_lines = posting(engine, "sales_invoice:reversal", inv["id"])
    assert rev["reverses_posting_id"] == orig["id"] and rev["posting_date"] == today(engine)
    assert [(x["debit"], x["credit"]) for x in rev_lines] == [(x["credit"], x["debit"]) for x in orig_lines]
    after = invoice(engine, inv["number"])
    assert after["status"] == "cancelled" and Decimal(after["outstanding"]) == 0


def test_an_invoice_with_a_payment_standing_is_refused_naming_it(engine):
    inv = invoice(engine)
    p = pay(engine, "100")
    r = refused(engine, "cancel_invoice", invoice=inv["number"])
    assert r.code == "has_payments" and p["number"] in r.message and "Reverse those first" in r.message


def test_an_invoice_already_cancelled_is_refused(engine):
    inv = invoice(engine, submit=False)
    commit(engine, "cancel_invoice", invoice=inv["number"])
    assert refused(engine, "cancel_invoice", invoice=inv["number"]).code == "already_cancelled"


def test_the_reversal_date_rules(engine):
    inv = invoice(engine, when=today(engine) - timedelta(days=2))
    assert refused(engine, "cancel_invoice", invoice=inv["number"],
                   date=str(today(engine) + timedelta(days=1))).code == "future_date"
    assert refused(engine, "cancel_invoice", invoice=inv["number"],
                   date=str(today(engine) - timedelta(days=5))).code == "reversal_before_original"


def test_a_reversal_in_a_later_period_says_so(engine):
    inv = invoice(engine, when=date(2026, 9, 30))
    c = card(engine, "cancel_invoice", invoice=inv["number"])
    assert any("September 2026" in w for w in c.card["warnings"])
    same = invoice(engine, when=today(engine))
    assert card(engine, "cancel_invoice", invoice=same["number"]).card["warnings"] == []


def test_a_frozen_account_refuses_the_cancellation_before_its_card(engine):
    inv = invoice(engine)
    with engine.begin() as conn:
        conn.execute(update(m.account).where(m.account.c.number == "4100").values(frozen=True))
    assert refused(engine, "cancel_invoice", invoice=inv["number"]).code == "frozen_account"


def test_numbers_are_kept_and_never_reused(engine):
    inv = invoice(engine, submit=False)
    commit(engine, "cancel_invoice", invoice=inv["number"])
    nxt = invoice(engine, submit=False)
    assert int(nxt["number"][-5:]) == int(inv["number"][-5:]) + 1


# ---------- reversing a payment ----------

def test_reversing_a_payment_that_paid_an_invoice_makes_it_owe_again(engine):
    inv = invoice(engine)
    p = pay(engine, "280")
    assert invoice(engine, inv["number"])["status"] == "paid"
    c, out = commit(engine, "reverse_payment", payment=p["number"], reason="cheque bounced")
    assert ["Owes again", f"{inv['number']} (280.00 of 280.00 outstanding)"] in c.card["lines"]
    assert c.card["books"][:2] == ["Bank (1100 Bank) -280.00", "Accounts Receivable (Hooli) +280.00"]
    after = invoice(engine, inv["number"])
    assert after["status"] == "submitted" and Decimal(after["outstanding"]) == Decimal("280.00")
    with engine.connect() as conn:
        assert conn.execute(select(t.payment.c.status).where(t.payment.c.id == p["id"])).scalar_one() == "cancelled"
        assert conn.execute(select(func.count()).select_from(t.allocation).where(
            t.allocation.c.payment_id == p["id"])).scalar_one() == 1  # allocations stay as history
    assert out.text == f"Reversed {p['number']} (280.00 USD) on {today(engine):%d %b %Y}: {inv['number']} owes 280.00 USD again."


def test_a_split_payment_with_credit_gives_each_invoice_its_share_back(engine):
    a = invoice(engine, when=today(engine) - timedelta(days=2))
    b = invoice(engine, qty="5", when=today(engine) - timedelta(days=1))
    p = pay(engine, "500")  # 280 to a, 140 to b, 80 credit
    c, out = commit(engine, "reverse_payment", payment=p["number"])
    assert ["Credit removed", "80.00 USD"] in c.card["lines"]
    assert invoice(engine, a["number"])["status"] == "submitted" and invoice(engine, b["number"])["status"] == "submitted"
    assert "the 80.00 USD credit is removed" in out.text


def test_after_the_payment_is_reversed_the_invoice_can_be_cancelled_and_paid_again(engine):
    inv = invoice(engine)
    p = pay(engine, "100")
    commit(engine, "reverse_payment", payment=p["number"])
    other = invoice(engine)
    pay(engine, "50", number=other["number"])
    commit(engine, "cancel_invoice", invoice=inv["number"])
    assert invoice(engine, inv["number"])["status"] == "cancelled"


def test_payment_refusals_and_questions(engine):
    invoice(engine)
    p = pay(engine, "100")
    commit(engine, "reverse_payment", payment=p["number"])
    assert refused(engine, "reverse_payment", payment=p["number"]).code == "already_reversed"
    assert refused(engine, "reverse_payment", payment="PAY-2099-99999").code == "payment_unknown"
    assert refused(engine, "reverse_payment", customer="Pied Piper").code == "no_payments"
    q = refused(engine, "reverse_payment", customer="Acme Corporation")
    assert q.code == "payment_ambiguous" and q.options and all(o.startswith("PAY-") for o in q.options)
    assert refused(engine, "reverse_payment").code == "payment_missing"


def test_the_read_backs_say_exactly_what_the_commits_said(engine):
    inv = invoice(engine)
    p = pay(engine, "100")
    _, out = commit(engine, "reverse_payment", payment=p["number"])
    _, out2 = commit(engine, "cancel_invoice", invoice=inv["number"])
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        for contract, o in (("reverse_payment", out), ("cancel_invoice", out2)):
            again = REGISTRY.contracts[contract].find_by_operation_key(ctx, o.operation_key)
            assert (again.text, again.figures) == (o.text, o.figures)


# ---------- the database rules ----------

def _raw(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


@pytest.mark.parametrize(("sql", "words"), [
    ("UPDATE sales_invoice SET outstanding = '100.00', status = 'partly_paid' WHERE id = :id", "outstanding must be"),
    ("UPDATE sales_invoice SET status = 'paid' WHERE id = :id", "ck_sales_invoice_paid_owes_nothing"),
    ("UPDATE sales_invoice SET status = 'draft', outstanding = '0.00' WHERE id = :id", "never to a draft"),
    ("UPDATE sales_invoice SET status = 'cancelled', outstanding = '0.00' WHERE id = :id", "posting reversed"),
])
def test_the_database_refuses_an_invoice_change_its_rows_do_not_justify(engine, sql, words):
    inv = invoice(engine)
    with pytest.raises(DBAPIError) as e:
        _raw(engine, sql, id=inv["id"])
    assert words in str(e.value)


def test_the_database_refuses_a_payment_marked_reversed_without_its_reversal(engine):
    invoice(engine)
    p = pay(engine, "100")
    with pytest.raises(DBAPIError) as e:
        _raw(engine, "UPDATE sales_payment SET status = 'cancelled' WHERE id = :id", id=p["id"])
    assert "only once its posting is reversed" in str(e.value)
    with pytest.raises(DBAPIError):
        _raw(engine, "UPDATE sales_payment SET cancel_operation_key = 'k' WHERE id = :id", id=p["id"])


def test_cancelled_never_moves_again(engine):
    inv = invoice(engine, submit=False)
    commit(engine, "cancel_invoice", invoice=inv["number"])
    with pytest.raises(DBAPIError):
        _raw(engine, "UPDATE sales_invoice SET status = 'submitted' WHERE id = :id", id=inv["id"])


def test_cancelling_a_draft_needs_no_period_for_its_date(engine):
    inv = invoice(engine, submit=False)
    c, out = commit(engine, "cancel_invoice", invoice=inv["number"], date="2020-01-01")  # no fiscal period then
    assert c.card["books"] == ["Nothing in the books changes; it was a draft."]
    assert invoice(engine, inv["number"])["status"] == "cancelled"


def test_the_payment_choices_name_the_invoices_each_paid(engine):
    q = refused(engine, "reverse_payment", customer="Acme Corporation")
    assert q.code == "payment_ambiguous" and all(", paid INV-" in o or "not applied" in o for o in q.options)
