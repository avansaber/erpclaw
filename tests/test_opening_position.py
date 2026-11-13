"""Approved opening bank and cash balances, with derived owner's equity."""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import administration as admin, approvals, credentials, ledger, masters, models as m, opening_position, principals as pr
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.contracts import check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company

EQUITY_SENTENCE = "what the business owns minus what it owes, at the start"
SPLIT_SENTENCE = "You or your accountant can split this into capital and retained earnings later."


def opening_contract():
    return REGISTRY.contracts["record_opening_position"]


def bank_line(account, amount):
    return {"account": account, "amount": amount}


def opening_args(bank=None):
    return {"bank_lines": bank if bank is not None else [bank_line("Bank", "2400.00")]}


def propose_opening(engine, args=None, **kwargs):
    return approvals.propose(engine, opening_contract(), opening_args() if args is None else args, **kwargs)


def role_id(engine, role):
    with engine.connect() as conn:
        return ledger.account_by_type(company_ctx(conn), role)["id"]


def books_start(engine):
    with engine.connect() as conn:
        return conn.execute(select(m.fiscal_year.c.start_date).where(
            m.fiscal_year.c.company_id == company_ctx(conn).company_id).order_by(
                m.fiscal_year.c.start_date).limit(1)).scalar_one()


def outcome_diff(before, after, card, status):
    old = before["approval"][(card.approval_id,)]
    row = after["approval"][(card.approval_id,)]
    return {
        "approval": {"changed": [({"id": card.approval_id}, {
            "status": "consumed", "decided_at": row["decided_at"],
            "approved_by": old["principal_id"], "approver_grants_version": old["policy_version"]})]},
        "operation_outcome": {"added": [{"key": card.operation_key, "company_id": old["company_id"],
            "approval_id": card.approval_id, "card_digest": card.digest, "status": status}]},
    }


def refuse_proposal(engine, args, code):
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose_opening(engine, args)
    assert err.value.code == code
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("bank,supplier,total,owed,equity", [
    (None, "1600.00", "0.00", "1,600.00", "-1,600.00"),
    ("5000.00", "1600.00", "5,000.00", "1,600.00", "3,400.00"),
    ("5000.00", None, "5,000.00", "0.00", "5,000.00"),
], ids=["supplier-only", "mixed", "bank-only"])
def test_opening_receipt_reports_assets_debt_and_equity(engine, bank, supplier, total, owed, equity):
    if supplier:
        with engine.begin() as conn:
            masters.create_party(company_ctx(conn), "supplier", "Supplier X")
    args = {
        "bank_lines": [bank_line("Bank", bank)] if bank else [],
        "supplier_lines": [{"supplier": "Supplier X", "amount": supplier}] if supplier else [],
    }
    start = books_start(engine)
    card = propose_opening(engine, args)
    outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert outcome.status == "committed", outcome.text
    # Mutation: printing plan.total alone omits the debt and equity.
    expected = (f"Recorded the opening position dated {start.day} {start:%b %Y}: "
                f"total starting value {total} USD, total owed to suppliers {owed} USD, "
                f"opening balance (owner's equity) {equity} USD.")
    assert outcome.text == expected
    assert outcome.figures == [total, owed, equity]
    assert outcome.amounts == [
        {"what": "value", "of": None, "value": value.replace(",", ""), "currency": "USD"}
        for value in (total, owed, equity)
    ]
    with engine.connect() as conn:
        stored = opening_position.find_opening(company_ctx(conn), card.operation_key)
    assert stored is not None
    assert stored.text == expected
    assert stored.figures == outcome.figures
    assert [(a.what, a.of, a.value, a.currency) for a in stored.amounts] == [
        ("value", None, value.replace(",", ""), "USD") for value in (total, owed, equity)
    ]


@pytest.mark.parametrize("amount", ["2400.00", "17.23"])
def test_positive_equity_is_posted_as_an_equity_credit(engine, amount):
    bank, equity = role_id(engine, "bank"), role_id(engine, "opening_balance")
    start = books_start(engine)
    card = propose_opening(engine, opening_args([bank_line("Bank", amount)]))
    before = snapshot(engine)
    outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert outcome.status == "committed", outcome.text
    after = snapshot(engine)
    [posting] = [r for k, r in after["posting"].items() if k not in before["posting"]]
    rows = sorted([r for r in after["ledger_entry"].values() if r["posting_id"] == posting["id"]],
                  key=lambda r: r["line_no"])
    assert [(r["account_id"], Decimal(r["debit"]), Decimal(r["credit"]), r["party_id"]) for r in rows] == [
        (bank, Decimal(amount), Decimal(0), None), (equity, Decimal(0), Decimal(amount), None)]
    assert all(r["currency"] == r["functional_currency"] == "USD" and Decimal(r["rate"]) == 1
               and r["dimension_set_id"] is None for r in rows)
    company_id = posting["company_id"]
    expected = outcome_diff(before, after, card, "committed")
    expected.update({
        "number_series": {"added": [{"company_id": company_id, "series": "OP",
            "fiscal_year": start.year, "next_value": 2}]},
        "posting": {"added": [{"company_id": company_id, "voucher_type": ledger.OPENING,
            "posting_date": str(start), "operation_key": card.operation_key}]},
        "ledger_entry": {"added": [{"posting_id": posting["id"], "account_id": bank},
                                      {"posting_id": posting["id"], "account_id": equity}]},
        "posting_chain_head": {"changed": [({"company_id": company_id}, {
            "last_seq": posting["seq"], "last_hash": posting["hash"]})]},
        "audit_log": {"added": [{"company_id": company_id, "actor": "owner",
            "action": "commit:record_opening_position", "approval_id": card.approval_id}]},
    })
    assert_changes(before, after, expected)
    with engine.connect() as conn:
        ctx = company_ctx(conn, company_id=company_id)
        assert ledger.balance(ctx, bank, start) == Decimal(amount)
        assert ledger.balance(ctx, equity, start) == -Decimal(amount)
        assert ledger.trial_balance_difference(ctx) == 0
        assert ledger.verify_chain(conn, company_id) is None
    shown = " ".join([card.card["title"], *[" ".join(x) for x in card.card["lines"]],
                      *card.card["books"], *card.card["warnings"], card.card["footer"]])
    assert EQUITY_SENTENCE in shown and SPLIT_SENTENCE in shown
    assert "Opening balance (owner's equity)" in shown
    assert "worth" not in shown.lower()
    check_card_amounts(card.card)
    figures = {(a["where"], a["label"]): a["value"] for a in card.card["amounts"]}
    assert figures[("lines", "Total starting value")] == amount
    assert figures[("lines", "Total owed to suppliers")] == "0.00"
    assert figures[("lines", "Opening balance (owner's equity)")] == amount


def test_bank_and_cash_lines_keep_their_order_and_derive_the_total(engine):
    bank, equity = role_id(engine, "bank"), role_id(engine, "opening_balance")
    cash_card = approvals.propose(engine, REGISTRY.contracts["add_cash_account"], {})
    assert approvals.approve(engine, cash_card.approval_id, cash_card.digest).status == "committed"
    with engine.connect() as conn:
        cash = ledger.cash_account(company_ctx(conn))["id"]
    card = propose_opening(engine, opening_args([bank_line("Cash on Hand", "100.17"), bank_line("1100", "200.06")]))
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    with engine.connect() as conn:
        rows = conn.execute(select(m.ledger_entry).join(m.posting).where(
            m.posting.c.operation_key == card.operation_key).order_by(m.ledger_entry.c.line_no)).mappings().all()
    assert [(r["account_id"], r["debit"], r["credit"]) for r in rows] == [
        (cash, Decimal("100.17"), 0), (bank, Decimal("200.06"), 0), (equity, 0, Decimal("300.23"))]
    figures = {a["label"]: a["value"] for a in card.card["amounts"] if a["where"] == "lines"}
    assert figures["Total starting value"] == figures["Opening balance (owner's equity)"] == "300.23"


@pytest.mark.parametrize("amount", ["-600.00", "0.00", "1.001", "1e2", "NaN", "Infinity", "1,000.00",
                                    "USD 10", "1000000000000000"])
def test_invalid_bank_amount_is_refused(engine, amount):
    refuse_proposal(engine, opening_args([bank_line("Bank", amount)]), "bad_amount")


@pytest.mark.parametrize("amount", [1.2, True, 10, {"value": "10", "currency": "EUR"}])
def test_amount_requires_exact_decimal_text(engine, amount):
    before = snapshot(engine)
    with pytest.raises(ValidationError):
        propose_opening(engine, opening_args([bank_line("Bank", amount)]))
    assert_changes(before, snapshot(engine), {})


def test_same_bank_account_twice_is_refused_as_duplicated(engine):
    refuse_proposal(engine, opening_args([bank_line("Bank", "1000.00"), bank_line("1100", "2000.00")]),
                    "duplicated_part")


@pytest.mark.parametrize("field", ["customer_lines", "stock_lines", "operation_key", "date"])
def test_later_line_fields_and_engine_fields_are_rejected(engine, field):
    before = snapshot(engine)
    with pytest.raises(ValidationError, match="Extra inputs"):
        propose_opening(engine, {**opening_args(), field: []})
    assert_changes(before, snapshot(engine), {})


def test_supplier_lines_are_accepted(engine):
    card = propose_opening(engine, {**opening_args(), "supplier_lines": []})
    assert card.status == "card"


def test_empty_opening_is_refused(engine):
    refuse_proposal(engine, opening_args([]), "empty_opening")


def test_short_posting_missing_a_credit_is_refused_unbalanced(engine):
    before = snapshot(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        bank = ledger.account_by_type(ctx, "bank")["id"]
        start = conn.execute(select(m.fiscal_year.c.start_date).where(
            m.fiscal_year.c.company_id == ctx.company_id).order_by(m.fiscal_year.c.start_date).limit(1)).scalar_one()
        with pytest.raises(Refusal) as err:
            ledger.post(ctx, start, ledger.OPENING, new_id(), [ledger.Line(bank, debit=Decimal("5000")),
                                                              ledger.Line(bank, debit=Decimal("1000"))])
        assert err.value.code == "unbalanced"
    assert_changes(before, snapshot(engine), {})


def test_second_opening_position_is_refused(engine):
    first = propose_opening(engine)
    assert approvals.approve(engine, first.approval_id, first.digest).status == "committed"
    refuse_proposal(engine, opening_args(), "opening_already_exists")


def test_existing_opening_from_another_writer_is_refused(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        bank, equity = [ledger.account_by_type(ctx, role)["id"] for role in ("bank", "opening_balance")]
        start = conn.execute(select(m.fiscal_year.c.start_date).where(
            m.fiscal_year.c.company_id == ctx.company_id).order_by(m.fiscal_year.c.start_date).limit(1)).scalar_one()
        ledger.post(ctx, start, ledger.OPENING, new_id(), [ledger.Line(bank, debit=Decimal(1)),
                                                         ledger.Line(equity, credit=Decimal(1))])
    refuse_proposal(engine, opening_args(), "opening_already_exists")


def test_second_approval_loses_the_commit_time_race(engine, monkeypatch):
    first, second = propose_opening(engine), propose_opening(engine)
    rendezvous = threading.Barrier(2)
    real_lock = approvals._lock_read_set

    def before_lock(conn, ctx, rs):
        # Both cards have been previewed and both approvals claimed. The barrier
        # precedes the chain-head lock, which serializes recompute and commit.
        rendezvous.wait(timeout=15)
        return real_lock(conn, ctx, rs)

    monkeypatch.setattr(approvals, "_lock_read_set", before_lock)
    outcomes, errors = [], []

    def run(card):
        try:
            outcomes.append((card, approvals.approve(engine, card.approval_id, card.digest)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,)) for c in (first, second)]
    before = snapshot(engine)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors
    assert sorted(o.status for _, o in outcomes) == ["committed", "refused"]
    loser, refused = next((c, o) for c, o in outcomes if o.status == "refused")
    assert refused.data["code"] == "opening_already_exists"
    assert refused.operation_key == loser.operation_key
    stored = approvals.read_back(engine, loser.operation_key)
    assert stored.status == "refused" and stored.data["code"] == "opening_already_exists"
    after = snapshot(engine)
    new_postings = [r for k, r in after["posting"].items() if k not in before["posting"]]
    assert len(new_postings) == 1 and new_postings[0]["voucher_type"] == ledger.OPENING
    assert len(after["ledger_entry"]) == len(before["ledger_entry"]) + 2
    expected = outcome_diff(before, after, loser, "refused")
    winner, _ = next((c, o) for c, o in outcomes if o.status == "committed")
    winner_diff = outcome_diff(before, after, winner, "committed")
    for table in expected:
        for kind in expected[table]:
            expected[table][kind] += winner_diff[table][kind]
    posting = new_postings[0]
    expected.update({"number_series": {"added": [{"company_id": posting["company_id"], "series": "OP",
        "fiscal_year": books_start(engine).year, "next_value": 2}]},
        "posting": {"added": [{"id": posting["id"]}]},
        "ledger_entry": {"added": [{"posting_id": posting["id"]}, {"posting_id": posting["id"]}]},
        "posting_chain_head": {"changed": [({"company_id": posting["company_id"]}, {
            "last_seq": posting["seq"], "last_hash": posting["hash"]})]},
        "audit_log": {"added": [{"action": "commit:record_opening_position", "approval_id": winner.approval_id}]}})
    assert_changes(before, after, expected)
    replay = approvals.approve(engine, loser.approval_id, loser.digest)
    assert (replay.status, replay.text, replay.operation_key) == (stored.status, stored.text, stored.operation_key)
    assert_changes(after, snapshot(engine), {})


def test_income_account_line_is_refused(engine):
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose_opening(engine, opening_args([bank_line("Sales", "100.00")]))
    assert err.value.code == "account_not_bank"
    after = snapshot(engine)
    assert after["posting"] == before["posting"]
    assert after["ledger_entry"] == before["ledger_entry"]
    assert after["number_series"] == before["number_series"]
    assert_changes(before, after, {})


@pytest.mark.parametrize("screen", ["preview", "refusal"])
def test_credential_refusal_preserves_opening_number_series(engine, monkeypatch, screen):
    bank = role_id(engine, "bank")
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        company_id = ctx.company_id
        year = conn.execute(select(m.fiscal_year.c.label).where(
            m.fiscal_year.c.company_id == company_id).order_by(m.fiscal_year.c.start_date).limit(1)).scalar_one()
        conn.execute(m.number_series.insert().values(
            company_id=company_id, series="OP", fiscal_year=year, next_value=7))
    card = propose_opening(engine)
    unreadable = "[" * 65  # The screen refuses text it cannot fully parse, without a credential-shaped value.
    assert credentials.check(unreadable) == credentials.UNREADABLE
    if screen == "preview":
        with engine.begin() as conn:
            conn.execute(update(m.account).where(m.account.c.id == bank).values(name=unreadable))

    real_preview = approvals._preview
    drawn = []

    def screen_after_draw(ctx, *args, **kwargs):
        preview = real_preview(ctx, *args, **kwargs)
        drawn.append(ctx.extra["opening_batch_number"])
        if screen == "refusal":
            credentials.refuse(unreadable)
        return preview

    before = snapshot(engine)
    with monkeypatch.context() as patched:
        patched.setattr(approvals, "_preview", screen_after_draw)
        outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert drawn == [f"OP-{year}-00007"]
    assert outcome.status == "refused" and outcome.data["code"] == "credential_refused"
    after = snapshot(engine)
    assert after["number_series"] == before["number_series"]
    assert_changes(before, after, outcome_diff(before, after, card, "refused"))

    if screen == "preview":
        with engine.begin() as conn:
            conn.execute(update(m.account).where(m.account.c.id == bank).values(name="Bank"))
    next_card = propose_opening(engine)
    committed = approvals.approve(engine, next_card.approval_id, next_card.digest)
    assert committed.status == "committed"
    assert committed.data["number"] == f"OP-{year}-00007"


@pytest.mark.parametrize("role", ["bank", "opening_balance"])
def test_frozen_role_account_is_refused(engine, role):
    account = role_id(engine, role)
    with engine.begin() as conn:
        conn.execute(update(m.account).where(m.account.c.id == account).values(frozen=True))
    refuse_proposal(engine, opening_args(), "frozen_account")


@pytest.mark.parametrize("role,code", [("bank", "no_bank_account"), ("opening_balance", "no_opening_balance_account")])
def test_missing_role_account_is_refused(engine, role, code):
    account = role_id(engine, role)
    with engine.begin() as conn:
        # Existing demo entries retain their account foreign key; remove its role.
        conn.execute(update(m.account).where(m.account.c.id == account).values(account_type=None))
    refuse_proposal(engine, opening_args(), code)


def test_second_bank_account_is_never_chosen_silently(engine):
    with engine.begin() as conn:
        conn.execute(m.account.insert().values(id=new_id(), company_id=company_ctx(conn).company_id,
            number="1120", name="Second Bank", root_type="asset", account_type="bank", is_group=False))
    refuse_proposal(engine, opening_args(), "several_bank_accounts")
    card = propose_opening(engine, opening_args([bank_line("Second Bank", "123.45")]))
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"


def test_closed_period_is_refused(engine):
    start = books_start(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        ledger.set_period_status(ctx, ledger.find_period(ctx, start)["id"], "locked")
    refuse_proposal(engine, opening_args(), "period_closed")


def test_replay_returns_the_stored_result_and_writes_nothing(engine, monkeypatch):
    card = propose_opening(engine)
    first = approvals.approve(engine, card.approval_id, card.digest)
    assert first.status == "committed"
    before = snapshot(engine)

    def cannot_commit(*args):
        pytest.fail("A consumed card must not run commit again")

    monkeypatch.setattr(opening_contract(), "commit", cannot_commit)
    replay = approvals.approve(engine, card.approval_id, card.digest)
    assert (replay.status, replay.text, replay.operation_key) == (first.status, first.text, first.operation_key)
    assert approvals.read_back(engine, card.operation_key).status == "committed"
    assert_changes(before, snapshot(engine), {})


def test_consumed_card_rejects_a_different_digest(engine):
    card = propose_opening(engine)
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.approve(engine, card.approval_id, "0" * 64)
    assert err.value.code == "digest"
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("other_company", [False, True], ids=["draft-only", "cross-company"])
def test_approval_requires_company_and_approve_grants(engine, other_company):
    with engine.connect() as conn:
        original_company = company_ctx(conn).company_id
    if other_company:
        create_company(engine, "Other Co")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Other Co") if other_company else company_ctx(conn)
        permissions = ["view", "draft", "approve", "post"] if other_company else ["view", "draft"]
        token = admin.add_principal(conn, "other approver" if other_company else "drafter", permissions, [ctx.company_id])
        principal = pr.authenticate(conn, token)
    card = propose_opening(engine, company_id=original_company,
                           **({} if other_company else {"principal": principal}))
    before = snapshot(engine)
    client = TestClient(create_app(engine, ScriptedModel([])))
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403, response.text
    assert_changes(before, snapshot(engine), {})


def test_direct_commit_without_an_approved_card_is_refused(engine):
    before = snapshot(engine)
    with engine.begin() as conn:
        with pytest.raises(Refusal) as err:
            opening_contract().commit(company_ctx(conn), opening_contract().input_model.model_validate(opening_args()), new_id())
        assert err.value.code == "not_approved"
    assert_changes(before, snapshot(engine), {})
