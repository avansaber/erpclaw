"""Recording a customer payment: which invoices it goes to, when the engine asks instead of choosing, credit that
stays in the customer's favour, the bank account, the date, possible duplicates, and a card, result and read-back
that show what was stored."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from erpclaw_core import approvals, ledger
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as s
from erpclaw_sales import tables as t

PAY = "record_payment"


def card(engine, **args):
    return approvals.propose(engine, REGISTRY.contracts[PAY], args)


def commit(engine, **args):
    c = card(engine, **args)
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert out.status == "committed", out.text
    return c, out


def refused(engine, **args) -> Refusal:
    before = count(engine, m.approval), count(engine, t.payment)
    with pytest.raises(Refusal) as e:
        card(engine, **args)
    assert (count(engine, m.approval), count(engine, t.payment)) == before  # no card, nothing written
    return e.value


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def lines(c) -> dict:
    out = {}
    for k, v in c.card["lines"]:
        out.setdefault(k, []).append(v)
    return out


def new_invoice(engine, customer="Hooli", qty="10", when=None) -> dict:
    """A submitted invoice for Copper Wire at 28.00 each; Hooli has no open invoice in the demo books."""
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
        item = dict(conn.execute(select(m.item).where(m.item.c.name == "Copper Wire")).mappings().one())
        r = s.create_invoice(ctx, cust, [(item, Decimal(qty))], when or ctx.today)
        return dict(conn.execute(select(t.invoice).where(t.invoice.c.id == r["id"])).mappings().one())


def invoice(engine, number) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(t.invoice).where(t.invoice.c.number == number)).mappings().one())


@pytest.fixture(autouse=True)
def invariants(engine):
    """After every test: a payment's amount is its allocations plus its credit; an invoice's outstanding is its total
    less its allocations, with the matching status; a customer's receivable is what their open invoices owe less
    their credit."""
    yield
    with engine.connect() as conn:
        for p in conn.execute(select(t.payment)).mappings():
            alloc = sum((Decimal(a) for a in conn.execute(select(t.allocation.c.amount).where(
                t.allocation.c.payment_id == p["id"])).scalars()), Decimal(0))
            assert Decimal(p["amount"]) == alloc + Decimal(p["unallocated"]), p["number"]
        for inv in conn.execute(select(t.invoice).where(t.invoice.c.status.in_(
                ("submitted", "partly_paid", "paid")))).mappings():
            alloc = sum((Decimal(a) for a in conn.execute(select(t.allocation.c.amount).where(
                t.allocation.c.invoice_id == inv["id"])).scalars()), Decimal(0))
            out = Decimal(inv["outstanding"])
            assert out == Decimal(inv["total"]) - alloc, inv["number"]
            assert (inv["status"] == "paid") == (out == 0), inv["number"]
            assert (inv["status"] == "partly_paid") == (0 < out < Decimal(inv["total"])), inv["number"]
        ctx = company_ctx(conn)
        ar = s.receivable_account(ctx)["id"]
        for cust in conn.execute(select(m.party).where(m.party.c.kind == "customer")).mappings():
            bal = sum((Decimal(d) - Decimal(c) for d, c in conn.execute(select(
                m.ledger_entry.c.debit, m.ledger_entry.c.credit).where(
                m.ledger_entry.c.account_id == ar, m.ledger_entry.c.party_id == cust["id"])).all()), Decimal(0))
            owed = sum((Decimal(o) for o in conn.execute(select(t.invoice.c.outstanding).where(
                t.invoice.c.customer_id == cust["id"],
                t.invoice.c.status.in_(("submitted", "partly_paid", "paid")))).scalars()), Decimal(0))
            credit = sum((Decimal(u) for u in conn.execute(select(t.payment.c.unallocated).where(
                t.payment.c.customer_id == cust["id"], t.payment.c.status == "submitted")).scalars()), Decimal(0))
            assert bal == owed - credit, cust["name"]


# ---------- the amount ----------

@pytest.mark.parametrize("amount", ["0", "-5", 10.5])
def test_zero_negative_and_float_amounts_are_refused_before_a_card(engine, amount):
    from pydantic import ValidationError
    before = count(engine, m.approval)
    with pytest.raises(ValidationError):
        card(engine, customer="Hooli", amount=amount)
    assert count(engine, m.approval) == before


def test_an_amount_beyond_the_currency_scale_is_refused_naming_the_scale(engine):
    r = refused(engine, customer="Hooli", amount="10.001")
    assert r.code == "bad_amount" and "2 decimal places for USD" in r.message and "cents" not in r.message


# ---------- which invoices ----------

def test_one_open_invoice_takes_the_payment_and_an_exact_amount_pays_it(engine):
    inv = new_invoice(engine)  # 280.00
    c, out = commit(engine, customer="Hooli", amount="280")
    assert lines(c)["Applied to"] == [f"{inv['number']} (280.00 of 280.00 outstanding), now paid"]
    after = invoice(engine, inv["number"])
    assert after["status"] == "paid" and Decimal(after["outstanding"]) == 0
    assert f"Applied to {inv['number']} 280.00 USD (now paid)" in out.text


def test_a_smaller_payment_leaves_it_partly_paid_and_a_second_settles_it(engine):
    inv = new_invoice(engine)
    commit(engine, customer="Hooli", amount="100")
    assert invoice(engine, inv["number"])["status"] == "partly_paid"
    commit(engine, customer="Hooli", amount="180")
    assert invoice(engine, inv["number"])["status"] == "paid"


def test_several_open_invoices_and_none_named_is_a_question_with_each_and_oldest_first(engine):
    a = new_invoice(engine, when=date(2026, 10, 1))
    b = new_invoice(engine, qty="5", when=date(2026, 10, 2))
    r = refused(engine, customer="Hooli", amount="100")
    assert r.code == "invoice_ambiguous"
    assert r.options == [f"{a['number']} (280.00 outstanding)", f"{b['number']} (140.00 outstanding)", "oldest first"]


def test_oldest_first_applies_the_oldest_until_the_money_runs_out(engine):
    a = new_invoice(engine, when=date(2026, 10, 1))
    b = new_invoice(engine, qty="5", when=date(2026, 10, 2))
    c, _ = commit(engine, customer="Hooli", amount="300", oldest_first=True)
    assert lines(c)["Applied to"] == [f"{a['number']} (280.00 of 280.00 outstanding), now paid",
                                      f"{b['number']} (20.00 of 140.00 outstanding), now partly paid"]


def test_a_named_invoice_takes_only_its_share_and_the_rest_stays_as_credit(engine):
    a = new_invoice(engine, when=date(2026, 10, 1))
    new_invoice(engine, qty="5", when=date(2026, 10, 2))
    c, out = commit(engine, invoice=a["number"], amount="300")
    assert lines(c)["Applied to"] == [f"{a['number']} (280.00 of 280.00 outstanding), now paid"]
    assert lines(c)["Credit left"] == ["20.00 USD"]
    assert any("20.00 USD stays as credit in Hooli's favour" in w for w in c.card["warnings"])
    assert "20.00 USD stays as credit in Hooli's favour." in out.text


def test_no_open_invoice_keeps_the_whole_amount_as_credit(engine):
    c, out = commit(engine, customer="Hooli", amount="50")
    assert "Applied to" not in lines(c) and lines(c)["Credit left"] == ["50.00 USD"]


def test_the_customer_comes_from_a_named_invoice(engine):
    inv = new_invoice(engine)
    c, _ = commit(engine, invoice=inv["number"], amount="280")
    assert lines(c)["Customer"] == ["Hooli"]


def test_a_named_invoice_of_another_customer_than_the_one_named_asks(engine):
    inv = new_invoice(engine)
    r = refused(engine, customer="Initech", invoice=inv["number"], amount="10")
    assert r.code == "customer_ambiguous" and r.options == ["Hooli"]


@pytest.mark.parametrize(("status", "words"), [("paid", "is already paid"), ("cancelled", "is cancelled"),
                                               ("draft", "is a draft")])
def test_a_named_invoice_that_is_not_open_is_refused_with_its_own_message(engine, status, words):
    with engine.begin() as conn:
        num = conn.execute(select(t.invoice.c.number).where(t.invoice.c.status == status)).scalars().first()
    r = refused(engine, invoice=num, amount="10")
    assert r.code == "invoice_not_open" and words in r.message


def test_a_payment_dated_before_an_invoice_is_not_applied_to_it(engine):
    with engine.connect() as conn:
        today = company_ctx(conn).today
    later = new_invoice(engine, when=today)
    r = refused(engine, invoice=later["number"], amount="10", date=str(today - timedelta(days=3)))
    assert r.code == "payment_before_invoice"
    c, _ = commit(engine, customer="Hooli", amount="10", date=str(today - timedelta(days=3)))
    assert "Applied to" not in lines(c)
    assert any(f"{later['number']} is dated" in w and "left out" in w for w in c.card["warnings"])


# ---------- the date ----------

def test_the_date_received_is_on_the_card_and_in_the_posting(engine):
    new_invoice(engine, when=date(2026, 10, 1))
    c, out = commit(engine, customer="Hooli", amount="10", date="2026-10-20")
    assert lines(c)["Date"] == ["Received 20 Oct 2026"]
    with engine.connect() as conn:
        p = conn.execute(select(m.posting).where(m.posting.c.voucher_type == "sales_payment").order_by(
            m.posting.c.seq.desc())).mappings().first()
    assert p["posting_date"] == date(2026, 10, 20)


def test_a_future_date_is_refused(engine):
    with engine.connect() as conn:
        today = company_ctx(conn).today
    assert refused(engine, customer="Hooli", amount="10", date=str(today + timedelta(days=1))).code == "future_date"


# ---------- the accounts ----------

def _add_bank(engine, number, name, **kw):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        parent = ledger.account_by_number(ctx, "1000")["id"]
        conn.execute(m.account.insert().values(id=f"acct-{number}", company_id=ctx.company_id, number=number,
                                               name=name, root_type="asset", account_type="bank",
                                               is_group=False, parent_id=parent, **kw))


def test_two_bank_accounts_and_none_named_asks_and_a_named_one_is_used(engine):
    _add_bank(engine, "1110", "Savings")
    r = refused(engine, customer="Hooli", amount="10")
    assert r.code == "account_ambiguous" and r.options == ["1100 Bank", "1110 Savings"]
    c, out = commit(engine, customer="Hooli", amount="10", account="Savings")
    assert lines(c)["Account"] == ["1110 Savings"]
    assert c.card["books"][0] == "Savings (1110 Savings) +10.00"
    with engine.connect() as conn:  # the posting itself: the named bank debited, the receivable credited for Hooli
        pay_id = conn.execute(select(t.payment.c.id).where(t.payment.c.operation_key == out.operation_key)).scalar_one()
        p = conn.execute(select(m.posting.c.id).where(m.posting.c.voucher_type == "sales_payment",
                                                      m.posting.c.voucher_id == pay_id)).scalar_one()
        rows = conn.execute(select(m.account.c.number, m.ledger_entry.c.debit, m.ledger_entry.c.credit,
                                   m.party.c.name).join(m.account, m.account.c.id == m.ledger_entry.c.account_id)
                            .outerjoin(m.party, m.party.c.id == m.ledger_entry.c.party_id)
                            .where(m.ledger_entry.c.posting_id == p).order_by(m.ledger_entry.c.line_no)).all()
    assert [(r[0], Decimal(r[1]), Decimal(r[2]), r[3]) for r in rows] == [
        ("1110", Decimal("10.00"), Decimal("0"), None), ("1200", Decimal("0"), Decimal("10.00"), "Hooli")]


def test_a_named_account_is_resolved_first_even_with_one_bank_account(engine):
    assert refused(engine, customer="Hooli", amount="10", account="Petty Box").code == "account_unknown"
    assert refused(engine, customer="Hooli", amount="10", account="1200").code == "account_not_bank"
    assert refused(engine, customer="Hooli", amount="10", account="1000").code == "account_not_bank"  # a group


def test_a_frozen_bank_account_is_refused_before_a_card(engine):
    _add_bank(engine, "1110", "Old Bank", frozen=True)
    assert refused(engine, customer="Hooli", amount="10", account="Old Bank").code == "account_frozen"
    with engine.begin() as conn:
        conn.execute(update(m.account).where(m.account.c.number == "1200").values(frozen=True))
    r = refused(engine, customer="Hooli", amount="10")  # the receivable frozen: the posting service's own check
    assert r.code == "frozen_account"


# ---------- the card ----------

def test_the_card_lists_its_fields_in_order_and_names_both_accounts(engine):
    new_invoice(engine)
    c = card(engine, customer="Hooli", amount="100", method="check", reference="CHQ 4471")
    keys = [k for k, _ in c.card["lines"]]
    assert keys == ["Company", "Customer", "Amount received", "Date", "Account", "Method", "Reference", "Applied to",
                    "Tax"]
    assert lines(c)["Tax"] == ["A payment carries no tax of its own."]
    assert c.card["books"] == ["Bank (1100 Bank) +100.00", "Accounts Receivable (Hooli) -100.00"]


def test_a_possible_duplicate_is_a_warning_not_a_refusal(engine):
    new_invoice(engine)
    with engine.connect() as conn:
        today = company_ctx(conn).today
    first, _ = commit(engine, customer="Hooli", amount="40", date=str(today - timedelta(days=3)))
    second = card(engine, customer="Hooli", amount="40")
    assert any("already has a 40.00 USD payment" in w for w in second.card["warnings"])
    assert approvals.approve(engine, second.approval_id, second.digest).status == "committed"
    eight = card(engine, customer="Hooli", amount="40", date=str(today - timedelta(days=11)))  # 8 days from the first
    assert not any("already has" in w for w in eight.card["warnings"])
    by_ref = card(engine, customer="Hooli", amount="7", reference=" chq  4471 ")
    commit(engine, customer="Hooli", amount="7.5", reference="CHQ 4471", date=str(today - timedelta(days=30)))
    again = card(engine, customer="Hooli", amount="7", reference="chq 4471")
    assert any("already has" in w for w in again.card["warnings"]) and by_ref


# ---------- the result ----------

def test_the_read_back_says_exactly_what_the_commit_said(engine):
    inv = new_invoice(engine)
    _, out = commit(engine, customer="Hooli", amount="300", method="bank transfer")
    with engine.connect() as conn:
        again = REGISTRY.contracts[PAY].find_by_operation_key(company_ctx(conn), out.operation_key)
    assert again.text == out.text and again.figures == out.figures
    assert out.text.startswith("Recorded PAY-") and inv["number"] in out.text


def test_the_commit_writes_the_card_s_allocation(engine):
    inv = new_invoice(engine)
    c = card(engine, customer="Hooli", amount="100")
    with engine.begin() as conn:  # another payment settles part of it before approval: the card is stale
        ctx = company_ctx(conn)
        ledger.lock_chain(ctx)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("50"), ctx.today))
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert out.status == "superseded"
    assert Decimal(invoice(engine, inv["number"])["outstanding"]) == Decimal("230.00")


def test_a_payment_reads_back_the_same_after_a_later_payment_settles_its_invoice(engine):
    inv = new_invoice(engine, qty="30")  # 840.00
    _, first = commit(engine, customer="Hooli", amount="100")
    assert f"{inv['number']} 100.00 USD (now partly paid)" in first.text
    commit(engine, customer="Hooli", amount="740")  # settles it
    with engine.connect() as conn:
        again = REGISTRY.contracts[PAY].find_by_operation_key(company_ctx(conn), first.operation_key)
    assert again.text == first.text and again.figures == first.figures


@pytest.mark.parametrize("clock", ["equal", "backwards"])
def test_the_read_back_holds_when_the_clock_ties_or_goes_backwards(engine, monkeypatch, clock):
    from datetime import datetime, timezone
    import erpclaw_sales.service as svc
    times = iter([datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),
                  datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc) if clock == "equal"
                  else datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc)])
    inv = new_invoice(engine, qty="30")  # 840.00
    monkeypatch.setattr(svc, "now", lambda: next(times))
    _, first = commit(engine, customer="Hooli", amount="100")
    commit(engine, customer="Hooli", amount="740")  # settles it, stamped at the same time or earlier
    with engine.connect() as conn:
        again = REGISTRY.contracts[PAY].find_by_operation_key(company_ctx(conn), first.operation_key)
    assert f"{inv['number']} 100.00 USD (now partly paid)" in again.text and again.text == first.text
