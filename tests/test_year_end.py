"""Year-end close, opening entries and the freeze."""
from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from erpclaw_core import ledger, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core.masters import create_party
from erpclaw_core.setup import create_company

EXTRA = [("2100", "Payables", "liability", "payable"), ("3200", "Opening Balance Equity", "equity", None),
         ("6100", "Rent", "expense", None)]


def company(engine, name: str, years=range(2025, 2028)) -> str:
    cid = create_company(engine, name, years=years)
    with engine.begin() as conn:
        for number, aname, root, atype in EXTRA:
            conn.execute(m.account.insert().values(id=new_id(), company_id=cid, number=number, name=aname,
                                                   root_type=root, account_type=atype, is_group=False, frozen=False))
    return name


def ctx_of(conn, name):
    return company_ctx(conn, actor="owner", company_name=name)


def A(ctx, number):
    return ledger.account_by_number(ctx, number)["id"]


def jp(ctx, d, debit, credit, amount, voucher="journal", party=None):
    amt = Decimal(amount)
    dl = ledger.Line(A(ctx, debit), debit=amt, party_id=party if debit in ("1200", "2100") else None)
    cl = ledger.Line(A(ctx, credit), credit=amt, party_id=party if credit in ("1200", "2100") else None)
    return ledger.post(ctx, d, voucher, new_id(), [dl, cl])


def year(ctx, label) -> dict:
    return dict(ctx.conn.execute(select(m.fiscal_year).where(m.fiscal_year.c.company_id == ctx.company_id,
                                                             m.fiscal_year.c.label == label)).mappings().one())


def bal(ctx, number, as_of):
    return ledger.balance(ctx, A(ctx, number), as_of)


def refused_code(fn) -> str:
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


@pytest.fixture
def co(engine):
    """A company with a 2025 of trading: sales 1,000.00, rent 300.00, all in the bank."""
    name = company(engine, "Close Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx, date(2025, 3, 10), "1100", "4100", "1000.00")
        jp(ctx, date(2025, 6, 5), "6100", "1100", "300.00")
    return name


def lock_2025(engine, name):
    with engine.begin() as conn:
        ledger.lock_through(ctx_of(conn, name), date(2025, 12, 31))


def close(engine, name, label=2025):
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        return ledger.close_year(ctx, year(ctx, label)["id"])


# --- closing writes -----------------------------------------------------------

def test_a_profit_year_credits_retained_earnings_by_the_net(engine, co):
    lock_2025(engine, co)
    with engine.connect() as conn:
        ctx = ctx_of(conn, co)
        before = {n: bal(ctx, n, date(2025, 12, 31)) for n in ("1100", "3100", "1200")}
        mid = bal(ctx, "4100", date(2025, 6, 30))
    r = close(engine, co)
    with engine.connect() as conn:
        ctx = ctx_of(conn, co)
        end = date(2025, 12, 31)
        assert bal(ctx, "4100", end) == 0 and bal(ctx, "6100", end) == 0
        assert bal(ctx, "3100", end) - before["3100"] == Decimal("-700.00")  # a credit of exactly the net
        assert bal(ctx, "1100", end) == before["1100"] and bal(ctx, "1200", end) == before["1200"]
        assert bal(ctx, "4100", date(2025, 6, 30)) == mid  # an as-of inside the year is unchanged
        fy = year(ctx, 2025)
        assert fy["status"] == "closed"
        statuses = set(conn.execute(select(m.fiscal_period.c.status).where(
            m.fiscal_period.c.fiscal_year_id == fy["id"])).scalars())
        assert statuses == {"closed"}
        rec = conn.execute(select(m.fiscal_year_close)).mappings().one()
        assert rec["closing_posting_id"] == r.posting_id and rec["closed_by"] == "owner"
        assert ledger.verify_chain(conn, ctx.company_id) is None


def test_a_loss_year_debits_retained_earnings(engine):
    name = company(engine, "Loss Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx, date(2025, 3, 10), "1100", "4100", "100.00")
        jp(ctx, date(2025, 6, 5), "6100", "1100", "300.00")
    lock_2025(engine, name)
    close(engine, name)
    with engine.connect() as conn:
        assert bal(ctx_of(conn, name), "3100", date(2025, 12, 31)) == Decimal("200.00")


def test_a_break_even_year_posts_offsetting_lines_and_no_retained_earnings_line(engine):
    name = company(engine, "Even Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx, date(2025, 3, 10), "1100", "4100", "300.00")
        jp(ctx, date(2025, 6, 5), "6100", "1100", "300.00")
    lock_2025(engine, name)
    r = close(engine, name)
    with engine.connect() as conn:
        ctx = ctx_of(conn, name)
        accounts = set(conn.execute(select(m.ledger_entry.c.account_id).where(
            m.ledger_entry.c.posting_id == r.posting_id)).scalars())
        assert accounts == {A(ctx, "4100"), A(ctx, "6100")}
        assert bal(ctx, "3100", date(2025, 12, 31)) == 0


def test_a_year_with_no_income_or_expense_closes_without_a_posting(engine):
    name = company(engine, "Quiet Co")
    lock_2025(engine, name)
    assert close(engine, name) is None
    with engine.connect() as conn:
        assert conn.execute(select(m.fiscal_year_close.c.closing_posting_id)).scalar_one() is None
        assert year(ctx_of(conn, name), 2025)["status"] == "closed"


def test_closing_one_company_leaves_another_unchanged(engine, co):
    other = company(engine, "Other Co")
    with engine.begin() as conn:
        jp(ctx_of(conn, other), date(2025, 4, 1), "1100", "4100", "50.00")

    def snapshot():
        with engine.connect() as conn:
            ctx = ctx_of(conn, other)
            return (conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == ctx.company_id)
                                 .order_by(m.posting.c.seq)).scalars().all(),
                    conn.execute(select(m.fiscal_period.c.status).where(m.fiscal_period.c.company_id == ctx.company_id)
                                 .order_by(m.fiscal_period.c.start_date)).scalars().all())

    before = snapshot()
    lock_2025(engine, co)
    close(engine, co)
    assert snapshot() == before


# --- closing refusals ---------------------------------------------------------

def test_close_needs_every_period_locked(engine, co):
    assert refused_code(lambda: close(engine, co)) == "periods_not_locked"


def test_close_refuses_a_period_already_closed(engine, co):
    lock_2025(engine, co)
    with engine.begin() as conn:  # not reachable through the service; forced to test the check
        ctx = ctx_of(conn, co)
        conn.execute(update(m.fiscal_period).where(m.fiscal_period.c.fiscal_year_id == year(ctx, 2025)["id"],
                                                   m.fiscal_period.c.period_no == 1).values(status="closed"))
    assert refused_code(lambda: close(engine, co)) == "periods_not_locked"


def test_years_close_in_order(engine, co):
    with engine.begin() as conn:
        ledger.lock_through(ctx_of(conn, co), date(2026, 12, 31))
    assert refused_code(lambda: close(engine, co, 2026)) == "earlier_year_open"


def test_close_needs_a_next_year(engine):
    name = company(engine, "One Year Co", years=range(2025, 2026))
    lock_2025(engine, name)
    assert refused_code(lambda: close(engine, name)) == "no_next_year"


@pytest.mark.parametrize("change", ["none", "two", "group"])
def test_close_needs_exactly_one_proper_retained_earnings_account(engine, co, change):
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        if change == "none":  # another company (the demo) still has one: it does not count
            conn.execute(update(m.account).where(m.account.c.id == A(ctx, "3100")).values(account_type=None))
        elif change == "two":
            conn.execute(m.account.insert().values(id=new_id(), company_id=ctx.company_id, number="3150",
                                                   name="More Retained", root_type="equity",
                                                   account_type="retained_earnings", is_group=False, frozen=False))
        else:
            conn.execute(update(m.account).where(m.account.c.id == A(ctx, "3100")).values(is_group=True))
    lock_2025(engine, co)
    assert refused_code(lambda: close(engine, co)) == "retained_earnings"


def test_close_refuses_books_whose_chain_does_not_verify(engine, co):
    lock_2025(engine, co)
    with engine.begin() as conn:
        conn.execute(update(m.posting_chain_head).where(
            m.posting_chain_head.c.company_id == ctx_of(conn, co).company_id).values(last_hash="f" * 64))
    assert refused_code(lambda: close(engine, co)) == "chain_broken"


def test_close_refuses_a_trial_balance_that_does_not_balance(engine, co, monkeypatch):
    lock_2025(engine, co)
    monkeypatch.setattr(ledger, "verify_chain", lambda conn, cid: None)  # reach the next check
    monkeypatch.setattr(ledger, "trial_balance_difference", lambda ctx: Decimal("0.01"))
    assert refused_code(lambda: close(engine, co)) == "trial_balance"


def test_a_frozen_income_or_expense_account_with_a_balance_refuses_the_close(engine, co):
    with engine.begin() as conn:
        conn.execute(update(m.account).where(m.account.c.id == A(ctx_of(conn, co), "6100")).values(frozen=True))
    lock_2025(engine, co)
    assert refused_code(lambda: close(engine, co)) == "frozen_account"


def test_a_second_close_is_refused(engine, co):
    lock_2025(engine, co)
    close(engine, co)
    assert refused_code(lambda: close(engine, co)) == "year_closed"


def test_a_failure_after_the_closing_posting_rolls_everything_back(engine, co):
    lock_2025(engine, co)
    with engine.begin() as conn:  # a close record already there makes the final insert fail
        ctx = ctx_of(conn, co)
        conn.execute(m.fiscal_year_close.insert().values(company_id=ctx.company_id,
                                                         fiscal_year_id=year(ctx, 2025)["id"],
                                                         closed_by="test", closed_at=now()))
        head = conn.execute(select(m.posting_chain_head.c.last_seq).where(
            m.posting_chain_head.c.company_id == ctx.company_id)).scalar_one()
        count = conn.execute(select(func.count()).select_from(m.posting).where(
            m.posting.c.company_id == ctx.company_id)).scalar_one()
    with pytest.raises(IntegrityError):  # the close record's key, after the posting was inserted
        close(engine, co)
    with engine.connect() as conn:
        ctx = ctx_of(conn, co)
        assert conn.execute(select(m.posting_chain_head.c.last_seq).where(
            m.posting_chain_head.c.company_id == ctx.company_id)).scalar_one() == head
        assert conn.execute(select(func.count()).select_from(m.posting).where(
            m.posting.c.company_id == ctx.company_id)).scalar_one() == count
        fy = year(ctx, 2025)
        assert fy["status"] == "open"
        assert set(conn.execute(select(m.fiscal_period.c.status).where(
            m.fiscal_period.c.fiscal_year_id == fy["id"])).scalars()) == {"locked"}


# --- the closing path an ordinary caller cannot reach --------------------------

@pytest.mark.parametrize("status", ["open", "locked"])
def test_the_public_post_refuses_the_closing_voucher_type(engine, co, status):
    if status == "locked":
        lock_2025(engine, co)
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        assert refused_code(lambda: jp(ctx, date(2025, 12, 31), "4100", "3100", "1.00",
                                       voucher=ledger.CLOSING)) == "closing_voucher"


@pytest.mark.parametrize("case", ["other_company", "other_year", "not_last_day", "other_voucher"])
def test_the_private_closing_path_checks_its_target(engine, co, case):
    other = company(engine, "Other Co")
    lock_2025(engine, co)
    lock_2025(engine, other)
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        fy = year(ctx, 2025)
        target, d, vid = fy["id"], date(2025, 12, 31), fy["id"]
        if case == "other_company":
            target = vid = year(ctx_of(conn, other), 2025)["id"]
        elif case == "other_year":
            target = vid = year(ctx, 2026)["id"]
        elif case == "not_last_day":
            d = date(2025, 11, 30)
        else:
            vid = new_id()
        lines = [ledger.Line(A(ctx, "4100"), debit=Decimal("1.00")), ledger.Line(A(ctx, "3100"), credit=Decimal("1.00"))]
        assert refused_code(lambda: ledger._post(ctx, d, ledger.CLOSING, vid, lines, memo=None, operation_key=None,
                                                 reverses=None, closing=ledger._Closing(target))) == "closing_target"


# --- posting against statuses --------------------------------------------------

def test_posting_into_locked_and_closed_periods_is_refused(engine, co):
    lock_2025(engine, co)
    with engine.begin() as conn:
        assert refused_code(lambda: jp(ctx_of(conn, co), date(2025, 7, 1), "1100", "4100", "1.00")) == "period_closed"
    close(engine, co)
    with engine.begin() as conn:
        assert refused_code(lambda: jp(ctx_of(conn, co), date(2025, 7, 1), "1100", "4100", "1.00")) == "period_closed"


def test_a_closing_entry_is_never_reversed(engine, co):
    lock_2025(engine, co)
    r = close(engine, co)
    with engine.begin() as conn:
        assert refused_code(lambda: ledger.reverse(ctx_of(conn, co), r.posting_id,
                                                   date(2026, 1, 5))) == "closing_not_reversed"


# --- statuses and the freeze ----------------------------------------------------

def test_closed_is_terminal(engine, co):
    lock_2025(engine, co)
    close(engine, co)
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        pid = ledger.find_period(ctx, date(2025, 5, 1))["id"]
        assert refused_code(lambda: ledger.set_period_status(ctx, pid, "open")) == "period_closed"
        assert refused_code(lambda: ledger.set_period_status(ctx, pid, "locked")) == "period_closed"
        assert refused_code(lambda: ledger.set_period_status(ctx, ledger.find_period(ctx, date(2026, 5, 1))["id"],
                                                             "closed")) == "close_by_year"


def test_lock_through_locks_in_order_and_only_at_a_period_end(engine, co):
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        with pytest.raises(Refusal, match="31 March 2025, the end of that period, or through 28 February 2025") as e:
            ledger.lock_through(ctx, date(2025, 3, 15))
        assert e.value.code == "freeze_not_period_end"
        assert ledger.lock_through(ctx, date(2025, 3, 31)) == 3
        statuses = conn.execute(select(m.fiscal_period.c.status).where(m.fiscal_period.c.company_id == ctx.company_id)
                                .order_by(m.fiscal_period.c.start_date)).scalars().all()
        assert statuses[:4] == ["locked", "locked", "locked", "open"]
        assert ledger.lock_through(ctx, date(2025, 3, 31)) == 0  # nothing more to lock


def test_lock_through_refuses_a_gap(engine, co):
    with engine.begin() as conn:
        ctx = ctx_of(conn, co)
        ledger.set_period_status(ctx, ledger.find_period(ctx, date(2025, 6, 1))["id"], "locked")
        assert refused_code(lambda: ledger.lock_through(ctx, date(2025, 3, 31))) == "freeze_gap"


# --- opening entries ------------------------------------------------------------

@pytest.fixture
def fresh(engine):
    name = company(engine, "Opening Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        create_party(ctx, "customer", "Acme Old")
        create_party(ctx, "supplier", "Supply Old")
    return name


def party(ctx, kind):
    return ctx.conn.execute(select(m.party.c.id).where(m.party.c.company_id == ctx.company_id,
                                                       m.party.c.kind == kind)).scalar_one()


def test_an_opening_entry_carries_balance_sheet_balances_only(engine, fresh):
    with engine.begin() as conn:
        ctx = ctx_of(conn, fresh)
        assert refused_code(lambda: jp(ctx, date(2025, 1, 1), "1100", "4100", "10.00",
                                       voucher=ledger.OPENING)) == "opening_pl"
        jp(ctx, date(2025, 1, 1), "1200", "3200", "500.00", voucher=ledger.OPENING, party=party(ctx, "customer"))


def test_an_opening_receivable_needs_this_companys_customer(engine, fresh):
    other = company(engine, "Other Co")
    with engine.begin() as conn:
        ctx = ctx_of(conn, fresh)
        create_party(ctx_of(conn, other), "customer", "Elsewhere")
        foreign = party(ctx_of(conn, other), "customer")
        for pid in (foreign, party(ctx, "supplier")):
            assert refused_code(lambda: jp(ctx, date(2025, 1, 1), "1200", "3200", "1.00", voucher=ledger.OPENING,
                                           party=pid)) == "bad_party"


def test_an_opening_entry_is_dated_in_the_first_year(engine, fresh):
    with engine.begin() as conn:
        assert refused_code(lambda: jp(ctx_of(conn, fresh), date(2026, 1, 1), "1100", "3200", "1.00",
                                       voucher=ledger.OPENING)) == "opening_year"


def test_opening_entries_come_first_on_both_paths(engine, fresh):
    with engine.begin() as conn:
        ctx = ctx_of(conn, fresh)
        jp(ctx, date(2025, 2, 1), "1100", "4100", "5.00")
        assert refused_code(lambda: jp(ctx, date(2025, 3, 1), "1100", "3200", "1.00",
                                       voucher=ledger.OPENING)) == "opening_after_entries"
        jp(ctx, date(2025, 2, 1), "1100", "3200", "1.00", voucher=ledger.OPENING)  # equal date: allowed
    name = company(engine, "Opening Two")
    with engine.begin() as conn:
        ctx = ctx_of(conn, name)
        jp(ctx, date(2025, 3, 1), "1100", "3200", "1.00", voucher=ledger.OPENING)
        assert refused_code(lambda: jp(ctx, date(2025, 2, 28), "1100", "4100", "5.00")) == "before_opening"
        jp(ctx, date(2025, 3, 1), "1100", "4100", "5.00")  # equal date, the other order


# --- races (both databases) -------------------------------------------------------

def _threads(*fns):
    errors: list[BaseException] = []

    def wrap(fn):
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    ts = [threading.Thread(target=wrap, args=(fn,)) for fn in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


def test_unlock_and_post_against_close(engine, monkeypatch):
    """Either the unlocking post commits first and the close refuses, or the
    close commits first and the unlock is refused; never both."""
    checks = ledger._check_lines

    def slow(ctx, lines_):
        time.sleep(0.03)  # widen the window inside both transactions
        return checks(ctx, lines_)

    monkeypatch.setattr(ledger, "_check_lines", slow)
    for i in range(6):
        name = company(engine, f"Race Co {i}")
        with engine.begin() as conn:
            jp(ctx_of(conn, name), date(2025, 3, 10), "1100", "4100", "10.00")
        lock_2025(engine, name)
        with engine.connect() as conn:
            ctx = ctx_of(conn, name)
            dec = ledger.find_period(ctx, date(2025, 12, 1))["id"]
            before = set(conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == ctx.company_id)).scalars())
        outcome: dict[str, str] = {}

        def closer():
            try:
                close(engine, name)
                outcome["close"] = "ok"
            except Refusal as e:
                outcome["close"] = e.code

        def unlocker():
            time.sleep(0.01 * (i % 3))
            try:
                with engine.begin() as conn:
                    c = ctx_of(conn, name)
                    ledger.set_period_status(c, dec, "open")
                    jp(c, date(2025, 12, 2), "1100", "4100", "1.00")
                outcome["unlock"] = "ok"
            except Refusal as e:
                outcome["unlock"] = e.code

        assert not _threads(closer, unlocker)
        assert [outcome["close"], outcome["unlock"]].count("ok") == 1, outcome
        with engine.connect() as conn:
            ctx = ctx_of(conn, name)
            fy = year(ctx, 2025)
            in_year = set(conn.execute(select(m.posting.c.id).where(
                m.posting.c.company_id == ctx.company_id, m.posting.c.posting_date <= fy["end_date"])).scalars())
            if fy["status"] == "closed":
                closing = conn.execute(select(m.fiscal_year_close.c.closing_posting_id).where(
                    m.fiscal_year_close.c.fiscal_year_id == fy["id"])).scalar_one()
                assert in_year == before | {closing}
                assert bal(ctx, "4100", fy["end_date"]) == 0
            assert ledger.verify_chain(conn, ctx.company_id) is None


def test_two_closes_of_one_year_close_it_once(engine, co):
    lock_2025(engine, co)
    results: list[str] = []

    def closer():
        try:
            close(engine, co)
            results.append("ok")
        except Refusal as e:
            results.append(e.code)

    assert not _threads(closer, closer)
    assert sorted(results) == ["ok", "year_closed"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.fiscal_year_close)).scalar_one() == 1


def test_opening_and_ordinary_submissions_serialize(engine):
    for i in range(5):
        name = company(engine, f"Order Co {i}")
        results: list[str] = []

        def submit(voucher, d):
            def run():
                try:
                    with engine.begin() as conn:
                        jp(ctx_of(conn, name), d, "1100", "3200" if voucher == ledger.OPENING else "4100", "1.00",
                           voucher=voucher)
                    results.append("ok")
                except Refusal as e:
                    results.append(e.code)
            return run

        assert not _threads(submit(ledger.OPENING, date(2025, 3, 1)), submit("journal", date(2025, 2, 1)))
        assert results.count("ok") == 1, results
        with engine.connect() as conn:
            ctx = ctx_of(conn, name)
            p = m.posting
            first_ordinary = conn.execute(select(func.min(p.c.posting_date)).where(
                p.c.company_id == ctx.company_id, p.c.voucher_type != ledger.OPENING)).scalar_one()
            last_opening = conn.execute(select(func.max(p.c.posting_date)).where(
                p.c.company_id == ctx.company_id, p.c.voucher_type == ledger.OPENING)).scalar_one()
            assert first_ordinary is None or last_opening is None or last_opening <= first_ordinary
