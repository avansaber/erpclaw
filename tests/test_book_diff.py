"""Each card on the invoice path changes exactly the rows it should and
nothing else: a book diff of every table after every operation,
against expectations written here, independently of the engine."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from bookdiff import assert_changes, changes, snapshot
from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx, now
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as t


def ids(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        acc = {r.number: r.id for r in conn.execute(select(m.account.c.number, m.account.c.id).where(
            m.account.c.company_id == ctx.company_id))}
        party = {r.name: r.id for r in conn.execute(select(m.party.c.name, m.party.c.id))}
        head = conn.execute(select(m.posting_chain_head.c.last_seq).where(
            m.posting_chain_head.c.company_id == ctx.company_id)).scalar_one()
        series = conn.execute(select(m.number_series.c.next_value).where(  # the posting date's year (the pinned today)
            m.number_series.c.company_id == ctx.company_id, m.number_series.c.series == "INV",
            m.number_series.c.fiscal_year == 2026)).scalar_one()
        return ctx.company_id, acc, party, head, series


def new_ids(before, after, table):
    """The primary keys of rows a table gained: used to bind links, never to supply expected values."""
    return [k[0] for k in after[table] if k not in before[table]]


def item_id(snap, name):
    return next(r["id"] for r in snap["item"].values() if r["name"] == name)


def approve(engine, contract, **args):
    card = approvals.propose(engine, REGISTRY.contracts[contract], args)
    return card, approvals.approve(engine, card.approval_id, card.digest)


def test_a_submitted_invoice_changes_exactly_its_rows(engine):
    cid, acc, party, head, series = ids(engine)
    before = snapshot(engine)
    _, out = approve(engine, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    assert out.status == "committed"
    after = snapshot(engine)
    [inv_id], [posting_id] = new_ids(before, after, "sales_invoice"), new_ids(before, after, "posting")
    # 5 x 28.00 = 140.00, worked out here, not by the engine
    assert_changes(before, after, {
        "sales_invoice": {"added": [{"id": inv_id, "company_id": cid, "customer_id": party["Acme Corporation"], "total": "140.00",
                                     "outstanding": "140.00", "status": "submitted", "currency": "USD",
                                     "operation_key": out.operation_key}]},
        "sales_invoice_line": {"added": [{"invoice_id": inv_id, "item_id": item_id(before, "Copper Wire"),
                                          "quantity": "5", "rate": "28.00", "amount": "140.00"}]},
        "posting": {"added": [{"id": posting_id, "company_id": cid, "voucher_type": "sales_invoice", "voucher_id": inv_id,
                               "seq": head + 1, "ledger": "general"}]},
        "ledger_entry": {"added": [{"posting_id": posting_id, "account_id": acc["1200"], "debit": "140.00",
                                    "credit": "0.00", "party_id": party["Acme Corporation"], "currency": "USD"},
                                   {"posting_id": posting_id, "account_id": acc["4100"], "debit": "0.00",
                                    "credit": "140.00", "party_id": None, "currency": "USD"}]},
        "posting_chain_head": {"changed": [({"company_id": cid}, {"last_seq": head + 1,
                                                                    "last_hash": after["posting"][next(
                                                                        k for k in after["posting"]
                                                                        if k not in before["posting"])]["hash"]})]},
        "number_series": {"changed": [({"company_id": cid, "series": "INV", "fiscal_year": 2026}, {"next_value": series + 1})]},
        "audit_log": {"added": [{"action": "submit_invoice"}, {"action": "commit:create_invoice"}]},
        "approval": {"added": [{"contract": "create_invoice", "status": "consumed"}]},
        "operation_outcome": {"added": [{"status": "committed"}]},
    })


def test_a_draft_posts_nothing(engine):
    cid, acc, party, head, series = ids(engine)
    before = snapshot(engine)
    approve(engine, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}], submit=False)
    after = snapshot(engine)
    [inv_id] = new_ids(before, after, "sales_invoice")
    assert Decimal(after["sales_invoice"][(inv_id,)]["outstanding"]) == 0  # its value here; its spelling, below
    assert_changes(before, after, {
        "sales_invoice": {"added": [{"id": inv_id, "status": "draft", "total": "56.00"}]},
        "sales_invoice_line": {"added": [{"invoice_id": inv_id, "item_id": item_id(before, "Copper Wire"),
                                          "quantity": "2", "amount": "56.00"}]},
        "number_series": {"changed": [({"company_id": cid, "series": "INV", "fiscal_year": 2026}, {"next_value": series + 1})]},
        "audit_log": {"added": [{"action": "save_draft_invoice"}, {"action": "commit:create_invoice"}]},
        "approval": {"added": [{"status": "consumed"}]},
        "operation_outcome": {"added": [{"status": "committed"}]},
    })


def test_a_payment_changes_its_invoice_and_posts_bank_against_receivable(engine):
    cid, acc, party, head, _ = ids(engine)
    _, inv = approve(engine, "create_invoice", customer="Hooli", items=[{"item": "circuit board", "quantity": 2}])
    with engine.connect() as conn:
        invoice = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == inv.operation_key)).mappings().one()
        older = conn.execute(select(t.invoice.c.id).where(t.invoice.c.customer_id == party["Hooli"],
                                                          t.invoice.c.status.in_(("submitted", "partly_paid")),
                                                          t.invoice.c.id != invoice["id"])).first()
    if older is not None:
        pytest.skip("the demo books give Hooli an earlier open invoice; allocation order is not under test here")
    before = snapshot(engine)
    pay = next((r for r in before["number_series"].values() if r["series"] == "PAY" and r["company_id"] == cid
                and r["fiscal_year"] == 2026), None)  # the year of the posting date (the pinned today)
    series_change = ({"added": [{"company_id": cid, "series": "PAY", "fiscal_year": 2026, "next_value": 2}]} if pay is None
                     else {"changed": [({"company_id": cid, "series": "PAY", "fiscal_year": 2026},
                                        {"next_value": pay["next_value"] + 1})]})
    approve(engine, "record_payment", customer="Hooli", amount="40.00", method="check")
    after = snapshot(engine)
    [pay_id], [posting_id] = new_ids(before, after, "sales_payment"), new_ids(before, after, "posting")
    # 2 x 45.00 = 90.00 invoiced; 40.00 paid leaves 50.00, so partly paid
    assert_changes(before, after, {
        "sales_payment": {"added": [{"id": pay_id, "company_id": cid, "customer_id": party["Hooli"], "amount": "40.00",
                                     "unallocated": "0.00"}]},
        "sales_payment_allocation": {"added": [{"payment_id": pay_id, "invoice_id": invoice["id"], "amount": "40.00"}]},
        "sales_invoice": {"changed": [({"id": invoice["id"]}, {"outstanding": "50.00", "status": "partly_paid"})]},
        "posting": {"added": [{"id": posting_id, "voucher_type": "sales_payment", "voucher_id": pay_id, "seq": head + 2}]},
        "ledger_entry": {"added": [{"posting_id": posting_id, "account_id": acc["1100"], "debit": "40.00", "credit": "0.00"},
                                   {"posting_id": posting_id, "account_id": acc["1200"], "debit": "0.00",
                                    "credit": "40.00", "party_id": party["Hooli"]}]},
        "posting_chain_head": {"changed": [({"company_id": cid}, {
            "last_seq": head + 2, "last_hash": next(r["hash"] for k, r in after["posting"].items()
                                                    if k not in before["posting"])})]},
        "number_series": series_change,
        "audit_log": {"added": [{"action": "record_payment"}, {"action": "commit:record_payment"}]},
        "approval": {"added": [{"contract": "record_payment", "status": "consumed"}]},
        "operation_outcome": {"added": [{"status": "committed"}]},
    })


def test_a_cancellation_reverses_with_mirror_lines_and_changes_only_its_invoice(engine):
    cid, acc, party, head, _ = ids(engine)
    _, inv = approve(engine, "create_invoice", customer="Initech", items=[{"item": "copper wire", "quantity": 1}])
    with engine.connect() as conn:
        invoice = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == inv.operation_key)).mappings().one()
    before = snapshot(engine)
    _, out = approve(engine, "cancel_invoice", invoice=invoice["number"])
    after = snapshot(engine)
    original = next(r for r in before["posting"].values() if r["voucher_id"] == invoice["id"])
    [posting_id] = new_ids(before, after, "posting")
    zero = after["sales_invoice"][(invoice["id"],)]["outstanding"]
    assert Decimal(zero) == 0  # its value here; its spelling is checked on its own, below
    assert_changes(before, after, {
        "sales_invoice": {"changed": [({"id": invoice["id"]}, {"status": "cancelled", "outstanding": zero,
                                                                "cancel_operation_key": out.operation_key})]},
        "posting": {"added": [{"id": posting_id, "reverses_posting_id": original["id"], "voucher_id": invoice["id"],
                               "seq": head + 2}]},
        "ledger_entry": {"added": [{"posting_id": posting_id, "account_id": acc["1200"], "debit": "0.00",
                                    "credit": "28.00", "party_id": party["Initech"]},
                                   {"posting_id": posting_id, "account_id": acc["4100"], "debit": "28.00",
                                    "credit": "0.00"}]},
        "posting_chain_head": {"changed": [({"company_id": cid}, {
            "last_seq": head + 2, "last_hash": next(r["hash"] for k, r in after["posting"].items()
                                                    if k not in before["posting"])})]},
        "audit_log": {"added": [{"action": "cancel_invoice"}, {"action": "commit:cancel_invoice"}]},
        "approval": {"added": [{"contract": "cancel_invoice", "status": "consumed"}]},
        "operation_outcome": {"added": [{"status": "committed"}]},
    })


BUSINESS = ("sales_invoice", "sales_invoice_line", "sales_payment", "sales_payment_allocation", "posting",
            "ledger_entry", "posting_chain_head", "number_series", "party", "item", "account", "company",
            "fiscal_year", "fiscal_period", "outbox")


def test_a_declined_card_changes_only_its_own_card(engine):
    before = snapshot(engine)
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1}]})
    approvals.decline(engine, card.approval_id)
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"id": card.approval_id, "status": "declined"}]}})


def test_an_expired_card_changes_only_its_own_card(engine, monkeypatch):
    before = snapshot(engine)
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1}]})
    later = now() + timedelta(minutes=11)
    monkeypatch.setattr(approvals, "now", lambda: later)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "expired"
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"id": card.approval_id, "status": "expired"}]}})


def test_a_stale_card_writes_no_business_row(engine):
    before = snapshot(engine)
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1}]})
    with engine.begin() as conn:
        conn.execute(m.item.update().where(m.item.c.name == "Copper Wire").values(standard_rate="29.00"))
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded"
    assert_changes(before, snapshot(engine), {
        "item": {"changed": [({"name": "Copper Wire"}, {"standard_rate": "29.00"})]},  # the test's own change, only
        "approval": {"added": [{"id": card.approval_id, "status": "superseded"},
                               {"id": out.approval_id, "status": "pending"}]},
    })


def test_the_harness_sees_a_change_nobody_expected(engine):
    """Anti-vacuity: an unrelated row changed alongside an operation fails the check."""
    before = snapshot(engine)
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1}]})
    approvals.decline(engine, card.approval_id)
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Pied Piper").values(city="Elsewhere"))
    with pytest.raises(AssertionError, match="should not have"):
        assert_changes(before, snapshot(engine), {"approval": {"added": [{"status": "declined"}]}})


def test_the_append_only_trigger_alone_refuses_a_posting_delete(engine):
    """With the foreign keys out of the way, inside a
    transaction that is rolled back, the trigger by itself refuses the delete."""
    with engine.connect() as conn:
        with conn.begin() as tx:
            conn.execute(text("ALTER TABLE ledger_entry DROP CONSTRAINT fk_entry_posting"))
            with pytest.raises(DBAPIError, match="append-only table posting: DELETE refused"):
                with conn.begin_nested():
                    conn.execute(text("DELETE FROM posting"))
            tx.rollback()  # the constraint comes back with the rollback
        with conn.begin():
            assert conn.execute(text("SELECT count(*) FROM posting")).scalar_one() > 0


# The pilot wrote zero as '0'; the rebuilt tables write it at the currency's scale, and their
# scale CHECK refuses '0' for USD.
def test_a_drafts_zero_outstanding_is_at_the_currencys_scale(engine):
    before = snapshot(engine)
    approve(engine, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}], submit=False)
    after = snapshot(engine)
    [inv_id] = new_ids(before, after, "sales_invoice")
    assert after["sales_invoice"][(inv_id,)]["outstanding"] == "0.00"


def test_a_cancelled_invoices_zero_outstanding_is_at_the_currencys_scale(engine):
    _, inv = approve(engine, "create_invoice", customer="Initech", items=[{"item": "copper wire", "quantity": 1}])
    with engine.connect() as conn:
        invoice = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == inv.operation_key)).mappings().one()
    approve(engine, "cancel_invoice", invoice=invoice["number"])
    assert snapshot(engine)["sales_invoice"][(invoice["id"],)]["outstanding"] == "0.00"
