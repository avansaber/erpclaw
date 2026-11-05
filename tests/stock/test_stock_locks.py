"""Stock commits under concurrency, on PostgreSQL with real connections: two cards taking the last stock
of one item, an ordinary posting and a period lock, all at once. No deadlock; stock is never negative; the loser is
superseded or refused; the books still agree."""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from erpclaw_core import approvals, ledger
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t

from test_approvals_concurrency import a_session_waits
from test_stock_commits import DAY, books, receive  # noqa: F401  (the fixture and its helper)


def _threads(fns) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(len(fns))

    def wrap(fn):
        try:
            start.wait(timeout=30)
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(fn,)) for fn in fns]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
        assert not th.is_alive(), "a thread is stuck: a deadlock or a lock never released"
    return errors


@pytest.mark.parametrize("third", ["posting", "period_lock"])
def test_two_cards_taking_the_last_stock_with_a_posting_or_a_period_lock_racing(books, third):
    engine = books["engine"]
    receive(books, "10", "10.00")
    cards = [approvals.propose(engine, REGISTRY.contracts["stock_sale"], dict(
        customer_id=books["customer"], item_id=books["item"], warehouse_id=books["warehouse"], qty="6",
        price="80.00", voucher=f"race-{n}", posting_date=DAY.isoformat())) for n in range(2)]
    outcomes: list = []

    def approve(card):
        return lambda: outcomes.append(approvals.approve(engine, card.approval_id, card.digest))

    def other():
        with engine.begin() as conn:
            ctx = company_ctx(conn)
            if third == "posting":  # an ordinary posting on the same chain
                bank, income = ledger.account_by_type(ctx, "bank")["id"], ledger.account_by_type(ctx, "income")["id"]
                ledger.post(ctx, DAY, "journal", f"j-{third}", [ledger.Line(bank, debit=Decimal("5.00")),
                                                               ledger.Line(income, credit=Decimal("5.00"))])
            else:  # the sale's own period, locked meanwhile
                ledger.set_period_status(ctx, ledger.find_period(ctx, DAY)["id"], "locked")

    errors = _threads([approve(cards[0]), approve(cards[1]), other])
    assert not errors, errors
    statuses = sorted(o.status for o in outcomes)
    committed = statuses.count("committed")
    # a posting: one takes 6 of 10 and the other cannot take 6 of the 4 left; a lock of the sale's period: at most
    # the one that ran before the lock commits
    assert committed == 1 if third == "posting" else committed <= 1, statuses
    assert all(s in ("committed", "superseded", "refused", "pending") for s in statuses), statuses
    with engine.connect() as conn:
        h = conn.execute(select(t.head)).mappings().one()
        left = Decimal(10 - 6 * committed)
        assert (h["qty"], h["value"]) == (left, left * 10)  # never negative, and only what committed moved
        assert stock.verify_stock(company_ctx(conn)) == []


def test_the_loser_of_a_race_cannot_issue_more_than_is_left(books):
    """After the winner, the loser's card is recomputed under the locks: the stock it needs is no longer there."""
    engine = books["engine"]
    receive(books, "10", "10.00")
    late = approvals.propose(engine, REGISTRY.contracts["stock_sale"], dict(
        customer_id=books["customer"], item_id=books["item"], warehouse_id=books["warehouse"], qty="6",
        price="80.00", voucher="late", posting_date=DAY.isoformat()))
    first = approvals.propose(engine, REGISTRY.contracts["stock_sale"], dict(
        customer_id=books["customer"], item_id=books["item"], warehouse_id=books["warehouse"], qty="6",
        price="80.00", voucher="first", posting_date=DAY.isoformat()))
    assert approvals.approve(engine, first.approval_id, first.digest).status == "committed"
    out = approvals.approve(engine, late.approval_id, late.digest)
    assert out.status != "committed", out.text
    with pytest.raises(Refusal) as caught:  # asked again, the engine says what is left
        approvals.propose(engine, REGISTRY.contracts["stock_sale"], dict(
            customer_id=books["customer"], item_id=books["item"], warehouse_id=books["warehouse"], qty="6",
            price="80.00", voucher="again", posting_date=DAY.isoformat()))
    assert caught.value.code == "insufficient_stock" and caught.value.data["on_hand"] == "4"


def test_a_trusted_write_takes_the_chain_head_before_any_stock_row(books):
    """The engine's one lock order: the chain head first, then the stock heads. An approved commit holds the chain
    head (the card's approval takes it first); a trusted write waiting behind it must hold no stock row meanwhile,
    or the two could deadlock."""
    engine = books["engine"]
    receive(books, "10", "10.00")
    holder = engine.connect()
    held = holder.begin()
    ledger.lock_chain(company_ctx(holder))  # as an approval's commit does before its stock writes
    done: list = []
    worker = threading.Thread(target=lambda: (receive(books, "5", "10.00"), done.append(True)))
    worker.start()
    try:
        assert a_session_waits(engine), "the trusted receipt never waited for the chain head"
        with engine.connect() as probe:  # while it waits, no stock row is locked
            probe.execute(text("SET lock_timeout = '500ms'"))
            try:
                probe.execute(select(t.head).with_for_update(nowait=True)).all()
            except OperationalError as e:
                pytest.fail(f"the trusted write locked a stock head before the chain head: {e.orig}")
            probe.rollback()
    finally:
        held.commit()
        holder.close()
        worker.join(timeout=30)
    assert done == [True]
    with engine.connect() as conn:
        h = conn.execute(select(t.head)).mappings().one()
        assert (h["qty"], h["value"]) == (Decimal("15"), Decimal("150.00"))
        assert stock.verify_stock(company_ctx(conn)) == []
