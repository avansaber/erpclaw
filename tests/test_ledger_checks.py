"""No card for a posting the books would refuse. `ledger.check_post` and
`ledger.check_reverse` run every refusal `post` and `reverse` raise before their first write, without writing and
without a lock; `post` and `reverse` call them first; every preview that will post calls the matching check."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import update

from bookdiff import snapshot
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.db import read_only
from erpclaw_core.plugins import REGISTRY
from test_ledger import D, acc, counts, lines, refusal_cases

INVOICE = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 2}]}


@pytest.mark.parametrize("code,make", refusal_cases(), ids=[c for c, _ in refusal_cases()])
def test_check_post_refuses_with_posts_own_code_and_writes_nothing(engine, code, make):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        bad = make(ctx)
        before = counts(conn)
        with pytest.raises(Refusal) as checked:
            ledger.check_post(ctx, D, "journal", new_id(), bad)
        with pytest.raises(Refusal) as posted:
            ledger.post(ctx, D, "journal", new_id(), bad)
        assert checked.value.code == posted.value.code == code
        assert counts(conn) == before


def test_check_post_refuses_a_missing_period_a_closing_voucher_and_a_duplicate_only_with_its_id(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for when, kind, code in ((date(2031, 1, 1), "journal", "no_period"), (D, ledger.CLOSING, "closing_voucher")):
            with pytest.raises(Refusal) as e:
                ledger.check_post(ctx, when, kind, new_id(), lines(ctx, "1.00", "1.00"))
            assert e.value.code == code
        vid = new_id()
        ledger.post(ctx, D, "journal", vid, lines(ctx, "1.00", "1.00"))
        with pytest.raises(Refusal) as e:
            ledger.check_post(ctx, D, "journal", vid, lines(ctx, "1.00", "1.00"))
        assert e.value.code == "duplicate_voucher"
        ledger.check_post(ctx, D, "journal", None, lines(ctx, "1.00", "1.00"))  # a document not yet created


def _posted(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        return ledger.post(ctx, D, "journal", new_id(), lines(ctx, "5.00", "5.00")).posting_id


@pytest.mark.parametrize("code", ["nothing_to_reverse", "reversal_of_reversal", "reversal_before_original",
                                  "already_reversed", "frozen_account"])
def test_check_reverse_refuses_with_reverses_own_code(engine, code):
    pid = _posted(engine)
    when = D
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if code == "nothing_to_reverse":
            pid = new_id()
        elif code == "reversal_of_reversal":
            pid = ledger.reverse(ctx, pid, D).posting_id
        elif code == "reversal_before_original":
            when = date(2026, 9, 30)
        elif code == "already_reversed":
            ledger.reverse(ctx, pid, D)
        elif code == "frozen_account":  # Reversal must reject a frozen account before presenting a card
            conn.execute(update(m.account).where(m.account.c.id == acc(ctx, "1100")).values(frozen=True))
        before = counts(conn)
        with pytest.raises(Refusal) as checked:
            ledger.check_reverse(ctx, pid, when)
        with pytest.raises(Refusal) as reversed_:
            ledger.reverse(ctx, pid, when)
        assert checked.value.code == reversed_.value.code == code
        assert counts(conn) == before


def test_the_checks_run_on_a_read_only_connection(engine):
    pid = _posted(engine)
    with read_only(engine) as conn:
        ctx = company_ctx(conn)
        assert len(ledger.check_post(ctx, D, "journal", None, lines(ctx, "1.00", "1.00"))) == 2
        assert len(ledger.check_reverse(ctx, pid, D)) == 2


def _freeze(engine, number):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(update(m.account).where(m.account.c.id == acc(ctx, number)).values(frozen=True))


def _lock_october(engine):
    with engine.begin() as conn:
        ledger.lock_through(company_ctx(conn), date(2026, 10, 31))


@pytest.mark.parametrize("why", ["receivable frozen", "sales frozen", "period locked"])
def test_no_create_and_submit_card_for_a_posting_the_books_would_refuse(engine, why):
    """Refuse invalid postings before presenting an approval card."""
    {"receivable frozen": lambda: _freeze(engine, "1200"), "sales frozen": lambda: _freeze(engine, "4100"),
     "period locked": lambda: _lock_october(engine)}[why]()
    before = snapshot(engine)
    with pytest.raises(Refusal) as e:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**INVOICE, "submit": True})
    assert e.value.code in ("frozen_account", "period_closed")
    assert snapshot(engine) == before  # no card, no business row


def test_a_draft_is_still_saved_when_only_its_posting_would_be_refused(engine):
    _freeze(engine, "1200")
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**INVOICE, "submit": False})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"


def test_no_submit_card_for_a_draft_whose_receivable_is_frozen(engine):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**INVOICE, "submit": False})
    number = approvals.approve(engine, card.approval_id, card.digest).data["number"]
    _freeze(engine, "1200")
    before = snapshot(engine)
    with pytest.raises(Refusal) as e:
        approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": number})
    assert e.value.code == "frozen_account"
    assert snapshot(engine) == before


def test_a_change_between_preview_and_commit_is_still_refused_at_commit(engine):
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"], {**INVOICE, "submit": True})
    _freeze(engine, "1200")  # after the card: the commit's own checks, under its locks, still refuse
    with engine.connect() as conn:
        posted_before = counts(conn)
    try:  # The recomputed preview refuses and the card ends superseded
        out = approvals.approve(engine, card.approval_id, card.digest)
        assert out.status in ("refused", "superseded") and "frozen" in out.text, (out.status, out.text)
    except Refusal as r:
        assert r.code == "frozen_account"
    with engine.connect() as conn:
        assert counts(conn) == posted_before  # no posting, no ledger line, the chain head unmoved


def test_post_keeps_its_refusals(engine):
    """post calls check_post first, then its own checks under the locks: nothing weaker at commit."""
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(update(m.account).where(m.account.c.id == acc(ctx, "1100")).values(frozen=True))
        with pytest.raises(Refusal) as e:
            ledger.post(ctx, D, "journal", new_id(), [ledger.Line(acc(ctx, "1100"), debit=Decimal("1.00")),
                                                       ledger.Line(acc(ctx, "4100"), credit=Decimal("1.00"))])
        assert e.value.code == "frozen_account"
