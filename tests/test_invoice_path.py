"""The invoice path on the rebuilt sales tables: the database rules.

Raw SQL here runs as the books' owner, below the engine's code: the keys, CHECKs and triggers are what
refuse it. The runtime role's own refusals are in test_runtime_role.py.
"""
from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company
from erpclaw_sales import service as s
from erpclaw_sales import tables as t

STATUSES = ("draft", "submitted", "partly_paid", "paid", "cancelled")
# The status rule, the forward rule beside the reversal revision's (sales_0002). "yes" is admitted by the transition
# rule; the amount and posting rules still decide (outstanding must match the payments standing; a submitted
# invoice is cancelled only with its posting reversed).
#
#   from \ to    draft   submitted  partly_paid  paid   cancelled      Forward rule
#   draft        same    yes        no           no     yes            the same
#   submitted    no      same       yes          yes    yes (reversed) the same
#   partly_paid  no      yes (new)  same         yes    no             back to submitted was refused
#   paid         no      yes (new)  yes (new)    same   no             back to submitted or partly paid was refused
#   cancelled    no      no         no           no     same           the same
OPEN = ("submitted", "partly_paid", "paid")
ADMITTED = ({("draft", "submitted"), ("draft", "cancelled"), ("submitted", "cancelled")}
            | {(a, b) for a in OPEN for b in OPEN} | {(x, x) for x in STATUSES})
NOW = "2026-10-26 12:00:00+00:00"


def approve(engine, contract, **args):
    card = approvals.propose(engine, REGISTRY.contracts[contract], args)
    return card, approvals.approve(engine, card.approval_id, card.digest)


def _invoice(engine, key):
    with engine.connect() as conn:
        inv = dict(conn.execute(select(t.invoice).where(t.invoice.c.operation_key == key)).mappings().one())
        lines = [dict(r) for r in conn.execute(select(t.invoice_line).where(t.invoice_line.c.invoice_id == inv["id"]))
                 .mappings()]
    return inv, lines


def submitted(engine, qty=5):
    _, out = approve(engine, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": qty}])
    assert out.status == "committed", out.text
    return _invoice(engine, out.operation_key)


def draft(engine, qty=2):
    _, out = approve(engine, "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": qty}],
                     submit=False)
    assert out.status == "committed", out.text
    return _invoice(engine, out.operation_key)


def refused(engine, sql, params=None, match=None):
    """The statement is refused by the database and leaves nothing behind."""
    with pytest.raises(DBAPIError, match=match):
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})


def passes(engine, sql, params=None):
    """The statement is admitted; rolled back, so the books stay as they were."""
    with engine.connect() as conn:
        conn.begin()
        conn.execute(text(sql), params or {})
        conn.rollback()


def _company(conn):
    ctx = company_ctx(conn, company_name="Stark Manufacturing")  # the demo company, also when a second exists
    party = conn.execute(select(m.party.c.id).where(m.party.c.company_id == ctx.company_id,
                                                    m.party.c.name == "Acme Corporation")).scalar_one()
    item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                                                  m.item.c.name == "Copper Wire")).scalar_one()
    return ctx.company_id, party, item


def raw_draft(conn, cid, party, currency="USD", total="10.00", number=None, status="draft"):
    """A draft inserted as the owner, below the engine; returns its id."""
    iid = new_id()
    zero = {"JPY": "0", "KWD": "0.000"}.get(currency, "0.00")
    conn.execute(text(
        "INSERT INTO sales_invoice (id, company_id, number, customer_id, posting_date, due_date, currency, total, "
        "outstanding, status, created_at) VALUES (:id, :c, :n, :p, '2026-10-26', '2026-11-25', :cur, :total, :zero, "
        ":status, :now)"), {"id": iid, "c": cid, "n": number or f"T-{iid[:8]}", "p": party, "cur": currency,
                            "total": total, "zero": zero, "status": status, "now": NOW})
    return iid


def raw_line(conn, cid, inv, item, currency="USD", qty="1", rate="10.00", amount="10.00"):
    conn.execute(text(
        "INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, rate, amount) "
        "VALUES (:id, :c, :i, :cur, :item, :q, :r, :a)"),
        {"id": new_id(), "c": cid, "i": inv, "cur": currency, "item": item, "q": qty, "r": rate, "a": amount})


def at_status(engine, status):
    """An invoice of 10.00 brought to `status` through the engine: submitted by the service (its posting), partly
    paid or paid by a real payment, cancelled by a real cancellation (its posting reversed). Never by raw updates the
    rules would refuse."""
    with engine.begin() as conn:
        cid, party, item = _company(conn)
        iid = raw_draft(conn, cid, party)
        raw_line(conn, cid, iid, item)
        if status == "draft":
            return iid
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        ledger.lock_chain(ctx)
        s.submit_draft(ctx, iid, None)
        inv = dict(conn.execute(select(t.invoice).where(t.invoice.c.id == iid)).mappings().one())
        cust = dict(conn.execute(select(m.party).where(m.party.c.id == party)).mappings().one())
        if status in ("partly_paid", "paid"):
            amount = Decimal("6.00") if status == "partly_paid" else Decimal("10.00")
            s.record_payment(ctx, s.plan_payment(ctx, cust, amount, inv["posting_date"], invoice=inv))
        elif status == "cancelled":
            s.cancel_invoice(ctx, s.plan_cancel(ctx, inv, inv["posting_date"]), "cancel-key")
    return iid


# --- no upsert back to draft -----------------------------------------------------------------------------------

def test_postgresql_on_conflict_cannot_move_a_submitted_invoice_back_to_draft(engine):
    inv, _ = submitted(engine)
    cols = [c.name for c in t.invoice.columns]
    row = {c: inv[c] for c in cols}
    refused(engine, f"INSERT INTO sales_invoice ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                    "ON CONFLICT (id) DO UPDATE SET status = 'draft', outstanding = '0.00'",
            {**row, "status": "draft", "outstanding": "0.00", "total": str(row["total"])}, match="refused")


# --- status only forward, fixed columns once submitted --------------------------------------------------------

@pytest.mark.parametrize("old", STATUSES)
def test_every_status_transition_the_rule_refuses_is_refused(engine, old):
    """Each pair the table refuses is refused by the transition rule itself, whatever the amount (the rule is checked
    first): no return to draft, nothing out of cancelled, no cancelling once money stood on it."""
    iid = at_status(engine, old)
    for new in STATUSES:
        if (old, new) in ADMITTED:
            continue
        refused(engine, "UPDATE sales_invoice SET status = :s, outstanding = '0.00' WHERE id = :i",
                {"s": new, "i": iid}, match="status only moves forward")


def test_the_admitted_moves_happen_only_with_the_rows_that_justify_them(engine):
    """paid and partly paid go back only when a payment is reversed; a raw move without that is refused by the
    amount rule; submitted to cancelled only with the posting reversed."""
    iid = at_status(engine, "paid")
    refused(engine, "UPDATE sales_invoice SET status = 'submitted', outstanding = total WHERE id = :i", {"i": iid},
            match="outstanding must be")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        ledger.lock_chain(ctx)
        pay = dict(conn.execute(select(t.payment).join(t.allocation, t.allocation.c.payment_id == t.payment.c.id)
                                .where(t.allocation.c.invoice_id == iid)).mappings().one())
        s.reverse_payment(ctx, s.plan_reverse_payment(ctx, pay, pay["posting_date"]))
    with engine.connect() as conn:
        assert conn.execute(select(t.invoice.c.status).where(t.invoice.c.id == iid)).scalar_one() == "submitted"
    sub = at_status(engine, "submitted")
    refused(engine, "UPDATE sales_invoice SET status = 'cancelled', outstanding = '0.00' WHERE id = :i", {"i": sub},
            match="posting reversed")


@pytest.mark.parametrize("column, value", [
    ("number", "'X-1'"), ("customer_id", "(SELECT id FROM party WHERE name = 'Hooli')"), ("posting_date", "'2026-10-01'"),
    ("due_date", "'2026-12-31'"), ("total", "'11.00'"), ("operation_key", "'k'"), ("submit_operation_key", "'k'"),
    ("created_at", "'2026-10-01 00:00:00+00:00'"), ("currency", "'EUR'"),
])
def test_a_fixed_column_changes_on_a_draft_but_not_once_submitted(engine, column, value):
    sql = f"UPDATE sales_invoice SET {column} = {value} WHERE id = :i"
    if column != "currency":  # a draft's currency is bound to its lines' by the composite key
        passes(engine, sql, {"i": at_status(engine, "draft")})
    for status in ("submitted", "partly_paid", "paid", "cancelled"):
        refused(engine, sql, {"i": at_status(engine, status)}, match="refused")


def test_status_outstanding_and_the_cancel_key_change_only_with_the_rows_behind_them(engine):
    iid = at_status(engine, "submitted")
    refused(engine, "UPDATE sales_invoice SET outstanding = '4.00', status = 'partly_paid' WHERE id = :i", {"i": iid},
            match="outstanding must be")  # no payment stands behind it
    assert at_status(engine, "partly_paid")  # a real payment of 6.00 moves it to partly paid, 4.00 outstanding
    cancelled = at_status(engine, "cancelled")  # a real cancellation sets the cancel key with its reversal
    refused(engine, "UPDATE sales_invoice SET cancel_operation_key = 'c2' WHERE id = :i", {"i": cancelled},
            match="refused")


def test_the_back_to_draft_sequence_cannot_start_and_ids_never_change(engine):
    """r2's sequence: back to draft, add a line, submitted again with the stale posting."""
    inv, lines = submitted(engine)
    refused(engine, "UPDATE sales_invoice SET status = 'draft', outstanding = '0.00' WHERE id = :i",
            {"i": inv["id"]}, match="status only moves forward")
    refused(engine, "UPDATE sales_invoice SET company_id = company_id || 'x' WHERE id = :i", {"i": inv["id"]})
    assert _invoice(engine, inv["operation_key"]) == (inv, lines)


def test_a_payment_only_moves_forward_and_keeps_its_columns(engine):
    with engine.connect() as conn:
        pid = conn.execute(text("SELECT id FROM sales_payment LIMIT 1")).scalar_one()
    for column, value in [("amount", "'1.00'"), ("unallocated", "'1.00'"), ("customer_id", "customer_id"),
                          ("number", "'X'"), ("posting_date", "'2026-01-01'"), ("method", "'cash'")]:
        sql = f"UPDATE sales_payment SET {column} = {value} WHERE id = :i"
        if column == "customer_id":
            passes(engine, sql, {"i": pid})  # an unchanged value is not a change
        else:
            refused(engine, sql, {"i": pid}, match="refused")
    refused(engine, "UPDATE sales_payment SET status = 'cancelled' WHERE id = :i", {"i": pid},
            match="only once its posting is reversed")  # never marked reversed while the ledger shows it received
    with engine.begin() as conn:  # a real reversal: the posting first, then the payment marked reversed
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        ledger.lock_chain(ctx)
        pay = dict(conn.execute(select(t.payment).where(t.payment.c.id == pid)).mappings().one())
        s.reverse_payment(ctx, s.plan_reverse_payment(ctx, pay, ctx.today))
    refused(engine, "UPDATE sales_payment SET status = 'submitted' WHERE id = :i", {"i": pid}, match="forward")


def test_an_invoice_is_inserted_only_as_a_draft(engine):
    with engine.connect() as conn:
        cid, party, _ = _company(conn)
    for status in ("submitted", "partly_paid", "paid", "cancelled"):
        with pytest.raises(DBAPIError, match="created as a draft"):
            with engine.begin() as conn:
                raw_draft(conn, cid, party, status=status)


def test_deletes_are_refused_except_a_bare_draft(engine):
    iid = at_status(engine, "submitted")
    refused(engine, "DELETE FROM sales_invoice WHERE id = :i", {"i": iid}, match="only a draft")
    refused(engine, "DELETE FROM sales_invoice_line WHERE invoice_id = :i", {"i": iid}, match="refused")
    refused(engine, "DELETE FROM sales_payment", match="refused|never deleted")
    refused(engine, "DELETE FROM sales_payment_allocation", match="refused")
    with engine.begin() as conn:
        cid, party, _ = _company(conn)
        bare = raw_draft(conn, cid, party)
    passes(engine, "DELETE FROM sales_invoice WHERE id = :i", {"i": bare})


@pytest.mark.parametrize("table", ["sales_invoice", "sales_invoice_line", "sales_payment", "sales_payment_allocation"])
def test_postgresql_truncate_is_refused(engine, table):
    refused(engine, f"TRUNCATE {table} CASCADE", match="refused")


# --- lines fixed once submitted -------------------------------------------------------------------------------

def test_a_line_goes_into_a_draft_and_never_into_a_submitted_invoice(engine):
    d, _ = draft(engine)
    with engine.connect() as conn:
        _, _, item = _company(conn)
    passes(engine, "INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, rate, "
                   "amount) VALUES ('l1', :c, :i, 'USD', :item, '1', '28.00', '28.00')",
           {"c": d["company_id"], "i": d["id"], "item": item})
    inv, lines = submitted(engine)
    refused(engine, "INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, rate, "
                    "amount) VALUES ('l2', :c, :i, 'USD', :item, '1', '28.00', '28.00')",
            {"c": inv["company_id"], "i": inv["id"], "item": item}, match="only into a draft")
    refused(engine, "UPDATE sales_invoice_line SET amount = '1.00' WHERE invoice_id = :i", {"i": inv["id"]},
            match="refused")
    refused(engine, "UPDATE sales_invoice_line SET amount = '1.00' WHERE invoice_id = :i", {"i": d["id"]},
            match="refused")  # a draft's lines are not edited either: a draft is cancelled and made again
    assert _invoice(engine, inv["operation_key"]) == (inv, lines)


def test_postgresql_a_line_insert_waits_for_a_submission_then_is_refused(engine):
    """Ordered by locks, not sleeps: the submitter holds the draft's row; the line insert's FOR SHARE waits
    (seen in pg_locks), then sees the invoice submitted and is refused."""
    d, _ = draft(engine)
    with engine.connect() as conn:
        _, _, item = _company(conn)
    submitter = engine.connect()
    submitter.begin()
    submitter.execute(text("SELECT id FROM sales_invoice WHERE id = :i FOR UPDATE"), {"i": d["id"]})
    submitter.execute(text("UPDATE sales_invoice SET status = 'submitted', outstanding = total WHERE id = :i"),
                      {"i": d["id"]})
    result, pid = {}, {}

    def insert_line():
        with engine.connect() as conn:
            pid["pid"] = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            try:
                raw_line(conn, d["company_id"], d["id"], item, rate="28.00", amount="28.00")
                conn.commit()
                result["ok"] = True
            except DBAPIError as e:
                result["error"] = str(e.orig)

    th = threading.Thread(target=insert_line)
    th.start()
    with engine.connect() as watcher:  # wait until the insert is blocked on the submitter's lock
        for _ in range(500):
            if "pid" in pid and watcher.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE pid = :p AND NOT granted"), {"p": pid["pid"]}).scalar_one():
                break
            watcher.rollback()
            time.sleep(0.01)
        else:
            pytest.fail("the line insert never waited for the submission")
    submitter.commit()
    submitter.close()
    th.join(10)
    assert "only into a draft" in result.get("error", ""), result


# --- company keys ---------------------------------------------------------------------------------------------

@pytest.fixture
def two(engine):
    """A second company in the same books, with its own customer, item, draft and payment."""
    create_company(engine, "Other Co")
    with engine.begin() as conn:
        c1, p1, i1 = _company(conn)
        c2 = conn.execute(select(m.company.c.id).where(m.company.c.name == "Other Co")).scalar_one()
        p2, i2 = new_id(), new_id()
        conn.execute(m.party.insert().values(id=p2, company_id=c2, kind="customer", name="Their Customer"))
        conn.execute(m.item.insert().values(id=i2, company_id=c2, name="Their Item", unit="unit", standard_rate="5.00"))
        d2 = raw_draft(conn, c2, p2)
        raw_line(conn, c2, d2, i2, qty="2", rate="5.00", amount="10.00")  # its lines add up to its 10.00
    return {"c1": c1, "p1": p1, "i1": i1, "c2": c2, "p2": p2, "i2": i2, "d2": d2}


def test_cross_company_references_are_refused_by_the_database(engine, two):
    k = two
    with pytest.raises(DBAPIError):  # an invoice with another company's customer
        with engine.begin() as conn:
            raw_draft(conn, k["c1"], k["p2"])
    with engine.begin() as conn:
        d1 = raw_draft(conn, k["c1"], k["p1"])
    with pytest.raises(DBAPIError):  # a line with another company's item
        with engine.begin() as conn:
            raw_line(conn, k["c1"], d1, k["i2"])
    with pytest.raises(DBAPIError):  # a line on another company's invoice
        with engine.begin() as conn:
            raw_line(conn, k["c1"], k["d2"], k["i1"])
    pay = ("INSERT INTO sales_payment (id, company_id, number, customer_id, posting_date, currency, amount, unallocated, "
           "status, created_at) VALUES (:id, :c, :n, :p, '2026-10-26', 'USD', '5.00', '5.00', 'submitted', :now)")
    refused(engine, pay, {"id": new_id(), "c": k["c1"], "n": "P-x", "p": k["p2"], "now": NOW})  # another's customer
    with engine.begin() as conn:
        pay1 = new_id()
        conn.execute(text(pay), {"id": pay1, "c": k["c1"], "n": "P-y", "p": k["p1"], "now": NOW})
    refused(engine, "INSERT INTO sales_payment_allocation (id, company_id, payment_id, invoice_id, currency, amount) "
                    "VALUES (:id, :c, :pay, :inv, 'USD', '5.00')",
            {"id": new_id(), "c": k["c1"], "pay": pay1, "inv": k["d2"]})  # across companies
    passes(engine, "INSERT INTO sales_invoice_line (id, company_id, invoice_id, currency, item_id, quantity, rate, "
                   "amount) VALUES ('ok', :c, :i, 'USD', :item, '1', '10.00', '10.00')",
           {"c": k["c1"], "i": d1, "item": k["i1"]})  # the keys themselves work


def test_numbers_are_per_company_and_unique_within_one(engine, two):
    with engine.begin() as conn:
        ctx2 = company_ctx(conn, company_name="Other Co")
        cust = dict(conn.execute(select(m.party).where(m.party.c.id == two["p2"])).mappings().one())
        item = dict(conn.execute(select(m.item).where(m.item.c.id == two["i2"])).mappings().one())
        r = s.create_invoice(ctx2, cust, [(item, Decimal(1))], date(2026, 10, 26), submit=False)
        ours = conn.execute(select(t.invoice.c.number).where(t.invoice.c.company_id == two["c1"],
                                                             t.invoice.c.number == r["number"])).scalar_one_or_none()
    assert r["number"] == "INV-2026-00001" and ours == "INV-2026-00001"  # the same number in each company
    with engine.connect() as conn:
        taken = conn.execute(select(t.invoice.c.number).where(t.invoice.c.company_id == two["c1"])).scalars().first()
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            raw_draft(conn, two["c1"], two["p1"], number=taken)


def test_two_invoices_submitted_at_once_get_distinct_consecutive_numbers(engine):
    out, errors = [], []

    def make():
        try:
            out.append(approve(engine, "create_invoice", customer="Acme Corporation",
                               items=[{"item": "copper wire", "quantity": 1}])[1])
        except Exception as e:  # noqa: BLE001  (reported below)
            errors.append(e)

    threads = [threading.Thread(target=make) for _ in range(2)]
    [th.start() for th in threads]
    [th.join(60) for th in threads]
    assert not errors and all(o.status == "committed" for o in out), (errors, [o.text for o in out])
    nums = sorted(int(_invoice(engine, o.operation_key)[0]["number"].rsplit("-", 1)[1]) for o in out)
    assert nums[1] == nums[0] + 1


# --- money ----------------------------------------------------------------------------------------------------

SCALE = {"JPY": ("100", "100.0"), "USD": ("100.00", "100.0"), "KWD": ("100.000", "100.00")}


@pytest.mark.parametrize("currency", sorted(SCALE))
def test_amounts_rates_and_quantities_are_refused_unless_exact_and_canonical(engine, currency):
    good, wrong_scale = SCALE[currency]
    neg = "-" + good
    zero = {"JPY": "0", "USD": "0.00", "KWD": "0.000"}[currency]
    with engine.connect() as conn:
        cid, party, item = _company(conn)
    with engine.begin() as conn:  # the valid rows first, so the keys are shown to admit them
        inv = raw_draft(conn, cid, party, currency=currency, total=good)
        raw_line(conn, cid, inv, item, currency=currency, qty="1", rate="100", amount=good)
    for total in (wrong_scale, neg, zero, "1e2", " " + good):
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                raw_draft(conn, cid, party, currency=currency, total=total)
    for amount in (wrong_scale, neg, zero):
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                raw_line(conn, cid, inv, item, currency=currency, amount=amount)
    other = "USD" if currency != "USD" else "KWD"
    with pytest.raises(DBAPIError):  # a line in another currency than its invoice
        with engine.begin() as conn:
            raw_line(conn, cid, inv, item, currency=other, amount=SCALE[other][0])
    for field, bad in (("qty", "1.00001"), ("qty", "01"), ("qty", "0"), ("rate", "28.00001"), ("rate", "-1"),
                       ("rate", "1e2")):
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                raw_line(conn, cid, inv, item, currency=currency, amount=good, **{field: bad})
    with pytest.raises(DBAPIError):  # a draft owes nothing
        with engine.begin() as conn:
            conn.execute(text("UPDATE sales_invoice SET outstanding = :o WHERE id = :i"), {"o": good, "i": inv})


# --- the path: submit_invoice through its card ----------------------------------------------------------------

from bookdiff import assert_changes, snapshot  # noqa: E402
from erpclaw_core import ledger  # noqa: E402
from erpclaw_core.common import Refusal  # noqa: E402


def _ids(engine):
    with engine.connect() as conn:
        cid, party, item = _company(conn)
        acc = {r.number: r.id for r in conn.execute(select(m.account.c.number, m.account.c.id).where(
            m.account.c.company_id == cid))}
        head = conn.execute(select(m.posting_chain_head.c.last_seq).where(
            m.posting_chain_head.c.company_id == cid)).scalar_one()
    return cid, party, acc, head


def test_submit_invoice_posts_the_draft_through_its_card_and_reads_back(engine):
    d, lines = draft(engine, qty=3)  # 3 x 28.00 = 84.00, worked out here
    cid, party, acc, head = _ids(engine)
    before = snapshot(engine)
    card, out = approve(engine, "submit_invoice", invoice=d["number"])
    assert out.status == "committed", out.text
    assert "Accounts Receivable (Acme Corporation) +84.00" in card.card["books"] and "Sales +84.00" in card.card["books"]
    after = snapshot(engine)
    [posting_id] = [k[0] for k in after["posting"] if k not in before["posting"]]
    assert_changes(before, after, {
        "sales_invoice": {"changed": [({"id": d["id"]}, {"status": "submitted", "outstanding": "84.00",
                                                         "submit_operation_key": out.operation_key})]},
        "posting": {"added": [{"id": posting_id, "company_id": cid, "voucher_type": "sales_invoice", "voucher_id": d["id"],
                               "seq": head + 1, "posting_date": "2026-10-26"}]},
        "ledger_entry": {"added": [{"posting_id": posting_id, "account_id": acc["1200"], "debit": "84.00",
                                    "credit": "0.00", "party_id": party},
                                   {"posting_id": posting_id, "account_id": acc["4100"], "debit": "0.00",
                                    "credit": "84.00", "party_id": None}]},
        "posting_chain_head": {"changed": [({"company_id": cid}, {"last_seq": head + 1,
                                                                    "last_hash": after["posting"][(posting_id,)]["hash"]})]},
        "audit_log": {"added": [{"action": "submit_invoice", "document_id": d["id"]},
                                {"action": "commit:submit_invoice"}]},
        "approval": {"added": [{"contract": "submit_invoice", "status": "consumed"}]},
        "operation_outcome": {"added": [{"status": "committed"}]},
    })
    back = approvals.read_back(engine, out.operation_key)
    assert back.status == "committed" and d["number"] in back.text
    with engine.connect() as conn:
        found = REGISTRY.contracts["submit_invoice"].find_by_operation_key(company_ctx(conn, company_name="Stark Manufacturing"),
                                                                           out.operation_key)
    assert found.data == {"number": d["number"], "total": "84.00", "posting_seq": head + 1}


def test_the_submit_card_is_refused_for_a_non_draft_and_a_draft_in_a_locked_period(engine):
    inv, _ = submitted(engine)
    with pytest.raises(Refusal, match="is not a draft"):
        approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": inv["number"]})
    d, _ = draft(engine)
    with engine.begin() as conn:
        ledger.lock_through(company_ctx(conn), date(2026, 10, 31))
    with pytest.raises(Refusal, match="period"):
        approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": d["number"]})


def test_another_companys_draft_cannot_be_submitted(engine, two):
    """A card refuses books with two companies today (it asks which one), so this is shown at the
    contract's preview, which the card calls, with each company's context."""
    preview = REGISTRY.contracts["submit_invoice"].preview
    inp = REGISTRY.contracts["submit_invoice"].input_model
    with engine.begin() as conn:  # another company's draft, under a number no invoice of ours has
        conn.execute(text("UPDATE sales_invoice SET number = 'INV-2099-00001' WHERE id = :i"), {"i": two["d2"]})
    with engine.connect() as conn:
        ours = company_ctx(conn, company_name="Stark Manufacturing")
        with pytest.raises(Refusal, match="can't find invoice"):
            preview(ours, inp(invoice="INV-2099-00001"))
        theirs = dict(conn.execute(select(t.invoice).where(t.invoice.c.id == two["d2"])).mappings().one())
        with pytest.raises(Refusal, match="another company"):  # the check below the lookup
            s.check_draft(ours, theirs)
        with pytest.raises(Refusal, match="does not exist in this company"):  # and the commit's locked re-read
            s.submit_draft(ours, two["d2"], None)
        assert preview(company_ctx(conn, company_name="Other Co"), inp(invoice="INV-2099-00001")).card.title


def test_a_draft_changed_after_its_card_makes_the_card_stale_and_posts_nothing(engine):
    d, lines = draft(engine, qty=2)
    card = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": d["number"]})
    with engine.begin() as conn:  # another line, and the total to match: still a valid draft, but not the card's
        raw_line(conn, d["company_id"], d["id"], lines[0]["item_id"], qty="1", rate="28.00", amount="28.00")
        conn.execute(text("UPDATE sales_invoice SET total = '84.00' WHERE id = :i"), {"i": d["id"]})
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded"
    after = snapshot(engine)
    assert after["posting"] == before["posting"] and after["sales_invoice"] == before["sales_invoice"]


def test_a_failure_after_the_posting_rolls_back_the_posting_the_status_and_the_audit(engine, monkeypatch):
    d, _ = draft(engine)
    card = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": d["number"]})
    before = snapshot(engine)
    real_audit = s.audit

    def audit_then_fail(ctx, action, *a, **k):
        real_audit(ctx, action, *a, **k)
        if action == "submit_invoice":
            raise RuntimeError("forced after the posting, the status change and the audit")

    monkeypatch.setattr(s, "audit", audit_then_fail)
    with pytest.raises(RuntimeError, match="forced"):
        approvals.approve(engine, card.approval_id, card.digest)
    after = snapshot(engine)
    for table in ("posting", "ledger_entry", "posting_chain_head", "sales_invoice", "audit_log"):
        assert after[table] == before[table], table


@pytest.mark.parametrize("rate, currency, shown", [
    ("0.1234", "USD", "0.1234"), ("28", "USD", "28.00"), ("28.5", "USD", "28.50"), ("1234.5678", "USD", "1,234.5678"),
    ("150", "JPY", "150"), ("0.25", "JPY", "0.25"), ("1.5", "KWD", "1.500"), ("1.2345", "KWD", "1.2345"),
])
def test_a_rate_is_shown_as_stored_never_rounded(rate, currency, shown):
    from erpclaw_core.money import fmt_rate

    assert fmt_rate(Decimal(rate), currency) == shown


def test_a_four_decimal_rate_is_shown_exactly_on_the_create_and_submit_cards(engine):
    """The submit card showed `@ 0.12` for a stored rate of 0.1234; the owner must approve the rate
    that is posted. 1000 x 0.1234 = 123.40, worked out here."""
    from sqlalchemy import update

    with engine.begin() as conn:
        cid, _, item = _company(conn)
        conn.execute(update(m.item).where(m.item.c.id == item).values(standard_rate=Decimal("0.1234")))
    row = "1000 x Copper Wire @ 0.1234 = 123.40"
    create = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                               {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1000}], "submit": False})
    assert row in str(create.card), create.card
    out = approvals.approve(engine, create.approval_id, create.digest)
    inv, [line] = _invoice(engine, out.operation_key)
    assert line["rate"] == Decimal("0.1234") and line["amount"] == Decimal("123.40")
    submit, done = approve(engine, "submit_invoice", invoice=inv["number"])
    assert row in str(submit.card) and done.status == "committed", submit.card
    assert "Accounts Receivable (Acme Corporation) +123.40" in submit.card["books"]
