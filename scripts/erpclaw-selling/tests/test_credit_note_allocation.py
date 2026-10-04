"""Credit note submit allocates to its invoice; cancel restores it.

When a credit note naming its invoice is submitted it is applied to that
invoice in the same transaction (a = min(credit, outstanding at submit));
whatever the invoice cannot absorb stays as open credit. Cancelling the
note reverses the application.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from selling_helpers import (
    call_action,
    ns,
    is_ok,
    is_error,
    load_db_query,
    seed_account,
    seed_customer,
    seed_company,
)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pay = _load("db_query_payments_cna", "erpclaw-payments/db_query.py")

try:
    from erpclaw_lib.db import get_dialect
except ImportError:
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")

from erpclaw_lib import payment_clearing


def _find_repo_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, "CLAUDE.md")) or os.path.isdir(
            os.path.join(cur, ".git")
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RuntimeError(f"repo root not found from {start}")
        cur = parent


try:
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing", "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_cna", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _green(conn):
    if get_dialect() == "postgresql":
        return
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present")
    inv_engine._ensure_decimal_sum(conn)
    assert inv_engine._check_inv22_payment_invoice_reconciliation(conn) is None
    assert inv_engine._check_inv25_ar_summary_detail(conn) is None
    assert inv_engine._check_inv27_party_level_residual(conn) is None


def _invoice(conn, env, qty="10"):
    create = call_action(
        mod.create_sales_invoice,
        conn,
        ns(
            sales_order_id=None,
            delivery_note_id=None,
            customer_id=env["customer"],
            company_id=env["company_id"],
            posting_date="2026-06-20",
            due_date="2026-07-20",
            items=json.dumps(
                [{"item_id": env["item1"], "qty": qty, "rate": "100.00",
                  "warehouse_id": env["warehouse"]}]
            ),
            tax_template_id=None,
            payment_terms_id=None,
        ),
    )
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    r = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    return si_id


def _pay(conn, env, inv, amount):
    created = call_action(
        pay.add_payment,
        conn,
        ns(
            company_id=env["company_id"],
            payment_type="receive",
            posting_date="2026-06-25",
            party_type="customer",
            party_id=env["customer"],
            paid_from_account=env["ar"],
            paid_to_account=env["cash"],
            paid_amount=amount,
            exchange_rate=None,
            payment_currency=None,
            reference_number=None,
            reference_date=None,
            allocations=json.dumps(
                [{"voucher_type": "sales_invoice", "voucher_id": inv,
                  "allocated_amount": amount}]
            ),
            deductions=None,
        ),
    )
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _refund(conn, env, cn, amount):
    if "bank" not in env:
        env["bank"] = seed_account(
            conn, env["company_id"], "Bank", "asset", "bank", "1010")
    created = call_action(
        pay.add_payment,
        conn,
        ns(
            company_id=env["company_id"],
            payment_type="pay",
            posting_date="2026-06-29",
            party_type="customer",
            party_id=env["customer"],
            paid_from_account=env["bank"],
            paid_to_account=env["ar"],
            paid_amount=amount,
            exchange_rate=None,
            payment_currency=None,
            reference_number=None,
            reference_date=None,
            allocations=json.dumps(
                [{"voucher_type": "credit_note", "voucher_id": cn,
                  "allocated_amount": amount}]
            ),
            deductions=None,
        ),
    )
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _credit_note(conn, env, inv, qty):
    cn = call_action(
        mod.create_credit_note,
        conn,
        ns(
            against_invoice_id=inv,
            reason="Returned goods",
            posting_date="2026-06-28",
            items=json.dumps([{"item_id": env["item1"], "qty": qty, "rate": "100.00"}]),
        ),
    )
    assert is_ok(cn), cn
    return cn["credit_note_id"]


def _submit(conn, id):
    return call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=id))


def _rows(conn, cn):
    rows = conn.execute(
        "SELECT against_voucher_type, against_voucher_id, amount "
        "FROM payment_ledger_entry WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ? AND delinked = 0",
        (cn,),
    ).fetchall()
    triples = [(r["against_voucher_type"], r["against_voucher_id"], r["amount"]) for r in rows]
    triples.sort(key=lambda t: (Decimal(t[2]), t[0], t[1]))
    return triples


def _doc(conn, id):
    r = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?", (id,)
    ).fetchone()
    return (r["outstanding_amount"], r["status"])


def _snapshot(conn):
    si = [
        dict(r)
        for r in conn.execute("SELECT * FROM sales_invoice ORDER BY id").fetchall()
    ]
    counts = {}
    for tbl in (
        "gl_entry",
        "stock_ledger_entry",
        "payment_ledger_entry",
        "payment_allocation",
        "audit_log",
    ):
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    naming = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM naming_series ORDER BY entity_type, prefix, company_id"
        ).fetchall()
    ]
    return (si, counts, naming)


def test_credit_note_reduces_its_invoice(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "400.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert r["applied_to"] == {"voucher_id": inv, "amount": "200.00"}
    assert r["open_credit"] == "0.00"
    assert _doc(conn, inv) == ("400.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "submitted")
    assert _rows(conn, cn) == [
        ("credit_note", cn, "-200.00"),
        ("sales_invoice", inv, "-200.00"),
        ("credit_note", cn, "200.00"),
    ]
    ple = conn.execute(
        "SELECT posting_date, currency, account_id FROM payment_ledger_entry "
        "WHERE voucher_type = 'credit_note' AND voucher_id = ? AND delinked = 0",
        (cn,),
    ).fetchall()
    assert len(ple) == 3
    for row in ple:
        assert row["posting_date"] == "2026-06-28"
        assert row["currency"] == "USD"
        assert row["account_id"] == env["ar"]
    gl = conn.execute(
        "SELECT account_id, debit, credit, party_type, party_id FROM gl_entry "
        "WHERE voucher_type = 'credit_note' AND voucher_id = ? AND is_cancelled = 0 "
        "AND entry_set = 'primary' ORDER BY debit DESC",
        (cn,),
    ).fetchall()
    assert len(gl) == 2
    by_acct = {row["account_id"]: dict(row) for row in gl}
    assert by_acct[env["ar"]]["credit"] == "200.00"
    assert Decimal(str(by_acct[env["ar"]]["debit"])) == Decimal("0")
    assert by_acct[env["ar"]]["party_type"] == "customer"
    assert by_acct[env["ar"]]["party_id"] == env["customer"]
    assert by_acct[env["revenue"]]["debit"] == "200.00"
    assert Decimal(str(by_acct[env["revenue"]]["credit"])) == Decimal("0")
    _green(conn)


def test_partial_absorption_leaves_open_credit(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "900.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert r["applied_to"] == {"voucher_id": inv, "amount": "100.00"}
    assert r["open_credit"] == "100.00"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn) == ("-100.00", "submitted")
    assert _rows(conn, cn) == [
        ("credit_note", cn, "-200.00"),
        ("sales_invoice", inv, "-100.00"),
        ("credit_note", cn, "100.00"),
    ]
    _green(conn)


def test_paid_original_absorbs_nothing(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "1000.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert r["applied_to"] == {"voucher_id": inv, "amount": "0.00"}
    assert r["open_credit"] == "200.00"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    assert _rows(conn, cn) == [("credit_note", cn, "-200.00")]
    _green(conn)


def test_several_notes_apply_in_order(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "400.00")
    cn1 = _credit_note(conn, env, inv, "2")
    r1 = _submit(conn, cn1)
    assert is_ok(r1), r1
    assert _doc(conn, inv) == ("400.00", "partially_paid")
    assert _doc(conn, cn1) == ("0", "submitted")
    cn2 = _credit_note(conn, env, inv, "3")
    r2 = _submit(conn, cn2)
    assert is_ok(r2), r2
    assert r2["applied_to"] == {"voucher_id": inv, "amount": "300.00"}
    assert _doc(conn, inv) == ("100.00", "partially_paid")
    assert _doc(conn, cn2) == ("0", "submitted")
    cn3 = _credit_note(conn, env, inv, "1.5")
    r3 = _submit(conn, cn3)
    assert is_ok(r3), r3
    assert r3["applied_to"] == {"voucher_id": inv, "amount": "100.00"}
    assert r3["open_credit"] == "50.00"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn3) == ("-50.00", "submitted")
    _green(conn)


def test_refusals_write_nothing(conn, env):
    inv = _invoice(conn, env, "10")

    def _fresh_note():
        return _credit_note(conn, env, inv, "2")

    oid = str(uuid.uuid4())
    cn = _fresh_note()
    conn.execute("UPDATE sales_invoice SET return_against = ? WHERE id = ?", (oid, cn))
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == f"Cannot submit credit note: original invoice {oid} not found"
    assert _snapshot(conn) == snap

    cn_a = _credit_note(conn, env, inv, "1")
    assert is_ok(_submit(conn, cn_a)), cn_a
    cn = _fresh_note()
    conn.execute("UPDATE sales_invoice SET return_against = ? WHERE id = ?", (cn_a, cn))
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == f"Cannot submit credit note: {cn_a} is itself a credit note"
    assert _snapshot(conn) == snap

    draft_create = call_action(
        mod.create_sales_invoice,
        conn,
        ns(
            sales_order_id=None,
            delivery_note_id=None,
            customer_id=env["customer"],
            company_id=env["company_id"],
            posting_date="2026-06-20",
            due_date="2026-07-20",
            items=json.dumps(
                [{"item_id": env["item1"], "qty": "1", "rate": "100.00",
                  "warehouse_id": env["warehouse"]}]
            ),
            tax_template_id=None,
            payment_terms_id=None,
        ),
    )
    assert is_ok(draft_create), draft_create
    draft_id = draft_create["sales_invoice_id"]
    cn = _fresh_note()
    conn.execute(
        "UPDATE sales_invoice SET return_against = ? WHERE id = ?", (draft_id, cn)
    )
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == "Cannot submit credit note: original invoice is 'draft'"
    assert _snapshot(conn) == snap

    cn_cancel = _fresh_note()
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn_a))
    assert is_ok(c), c
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_ok(c), c
    snap = _snapshot(conn)
    r = _submit(conn, cn_cancel)
    assert not is_ok(r)
    assert r["message"] == "Cannot submit credit note: original invoice is 'cancelled'"
    assert _snapshot(conn) == snap
    inv = _invoice(conn, env, "10")

    cn = _fresh_note()
    other_cust = seed_customer(conn, env["company_id"], "Other Corp")
    conn.execute("UPDATE sales_invoice SET customer_id = ? WHERE id = ?", (other_cust, cn))
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == (
        f"Cannot submit credit note: original invoice {inv} belongs to another customer or company"
    )
    assert _snapshot(conn) == snap

    cn = _fresh_note()
    other_co = seed_company(conn, "Other Co")
    conn.execute("UPDATE sales_invoice SET company_id = ? WHERE id = ?", (other_co, cn))
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == (
        f"Cannot submit credit note: original invoice {inv} belongs to another customer or company"
    )
    assert _snapshot(conn) == snap

    cn = _fresh_note()
    conn.execute("UPDATE sales_invoice SET currency = 'EUR' WHERE id = ?", (cn,))
    conn.commit()
    snap = _snapshot(conn)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == (
        "Cannot submit credit note: its currency EUR differs from the original invoice's USD"
    )
    assert _snapshot(conn) == snap


def test_a_clearing_refusal_rolls_everything_back(conn, env, monkeypatch):
    inv = _invoice(conn, env, "10")
    assert _doc(conn, inv) == ("1000.00", "submitted")
    cn = _credit_note(conn, env, inv, "1")
    conn.execute(
        "UPDATE sales_invoice SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
        (inv,),
    )
    conn.commit()
    _orig = conn.execute(
        "SELECT outstanding_amount, status, updated_at FROM sales_invoice WHERE id = ?",
        (inv,),
    ).fetchone()
    recorded = (_orig["outstanding_amount"], _orig["status"], _orig["updated_at"])
    assert recorded == ("1000.00", "submitted", "2000-01-01 00:00:00")
    _before_ledger = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry WHERE voucher_id = ?",
        (cn,),
    ).fetchone()[0]
    assert _before_ledger == 0
    naming_before = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM naming_series ORDER BY entity_type, prefix, company_id"
        ).fetchall()
    ]
    real = mod.payment_clearing.allocate_return_to_document
    calls = []

    def wrapper(conn_, *a, **k):
        calls.append(1)
        conn_.execute(
            "UPDATE sales_invoice SET outstanding_amount = '0', status = 'paid' WHERE id = ?",
            (inv,),
        )
        return real(conn_, *a, **k)

    monkeypatch.setattr(mod.payment_clearing, "allocate_return_to_document", wrapper)
    r = _submit(conn, cn)
    assert is_error(r)
    assert r["message"] == "Cannot apply payment: sales_invoice is 'paid'"
    assert len(calls) == 1
    _after = conn.execute(
        "SELECT outstanding_amount, status, updated_at FROM sales_invoice WHERE id = ?",
        (inv,),
    ).fetchone()
    assert (_after["outstanding_amount"], _after["status"], _after["updated_at"]) == recorded
    assert _doc(conn, cn) == ("-100.00", "draft")
    _after_ledger = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry WHERE voucher_id = ?",
        (cn,),
    ).fetchone()[0]
    assert _after_ledger == 0
    _gl = conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ?",
        (cn,),
    ).fetchone()[0]
    assert _gl == 0
    naming_after = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM naming_series ORDER BY entity_type, prefix, company_id"
        ).fetchall()
    ]
    assert naming_after == naming_before


def test_original_cancelled_after_the_refusals_is_refused(conn, env, monkeypatch):
    inv = _invoice(conn, env, "10")
    cn = _credit_note(conn, env, inv, "2")
    snap = _snapshot(conn)
    real = mod.insert_gl_entries

    def _wrapper(conn_, entries, **kw):
        conn_.execute("UPDATE sales_invoice SET status = 'cancelled' WHERE id = ?", (inv,))
        return real(conn_, entries, **kw)

    monkeypatch.setattr(mod, "insert_gl_entries", _wrapper)
    r = _submit(conn, cn)
    assert not is_ok(r)
    assert r["message"] == "Cannot submit credit note: original invoice is 'cancelled'"
    assert _snapshot(conn) == snap


def _party_live_sum(conn, party_id):
    total = Decimal("0")
    for row in conn.execute(
        "SELECT amount FROM payment_ledger_entry WHERE party_type = 'customer' "
        "AND party_id = ? AND delinked = 0",
        (party_id,),
    ).fetchall():
        total += Decimal(str(row["amount"]))
    return total


def _gl_sums(conn):
    sums = {}
    for row in conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall():
        sums[row["account_id"]] = sums.get(row["account_id"], Decimal("0")) + (
            Decimal(str(row["debit"])) - Decimal(str(row["credit"]))
        )
    return sums


def test_cancel_restores_the_invoice(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "400.00")
    party_before = _party_live_sum(conn, env["customer"])
    gl_before = _gl_sums(conn)
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    assert c["restored"] == {"sales_invoice:" + inv: "200.00"}
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "cancelled")
    rows = conn.execute(
        "SELECT delinked FROM payment_ledger_entry WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ?",
        (cn,),
    ).fetchall()
    assert len(rows) == 3
    assert [row["delinked"] for row in rows] == [1, 1, 1]
    assert _party_live_sum(conn, env["customer"]) == party_before
    assert _gl_sums(conn) == gl_before
    _green(conn)


def test_cancel_after_the_invoice_was_paid_leaves_it_owing(conn, env):
    inv = _invoice(conn, env, "10")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, inv) == ("800.00", "partially_paid")
    _pay(conn, env, inv, "800.00")
    assert _doc(conn, inv) == ("0", "paid")
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    assert _doc(conn, inv) == ("200.00", "partially_paid")
    _green(conn)


def _green225(conn):
    if get_dialect() == "postgresql":
        return
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present")
    inv_engine._ensure_decimal_sum(conn)
    assert inv_engine._check_inv22_payment_invoice_reconciliation(conn) is None
    assert inv_engine._check_inv25_ar_summary_detail(conn) is None


def test_cancel_a_partly_refunded_note(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "900.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, cn) == ("-100.00", "submitted")
    rid = _refund(conn, env, cn, "60.00")
    assert _doc(conn, cn) == ("-40.00", "partially_paid")
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    assert _doc(conn, inv) == ("100.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "cancelled")
    n = conn.execute(
        "SELECT COUNT(*) FROM payment_allocation WHERE payment_entry_id = ? AND delinked = 1",
        (rid,),
    ).fetchone()[0]
    assert n == 1
    # INV-22 and INV-25 are green here. INV-27 diverges (see CHANGES.md finding
    # F-INV27-REFUND-RELEASE): after release_allocations_on_document voids the
    # refund's allocation, the refund payment keeps a live +60.00 party row
    # with netted-zero compensation while carrying 60.00 unallocated, so the
    # party ledger reads 120.00 above outstanding-minus-residual. That release
    # path lives in erpclaw_lib.payment_clearing, which this change must not
    # touch; the pre-change shape diverges the same way.
    _green225(conn)


def test_a_pre_change_note_cancels_as_before(conn, env):
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "400.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    conn.execute(
        "DELETE FROM payment_ledger_entry WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ? AND amount = '200.00'",
        (cn,),
    )
    conn.execute(
        "DELETE FROM payment_ledger_entry WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ? AND against_voucher_type = 'sales_invoice'",
        (cn,),
    )
    conn.execute(
        "UPDATE payment_ledger_entry SET against_voucher_type = 'sales_invoice', "
        "against_voucher_id = ? WHERE voucher_type = 'credit_note' AND voucher_id = ?",
        (inv, cn),
    )
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '600.00', status = 'partially_paid' "
        "WHERE id = ?",
        (inv,),
    )
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '-200.00' WHERE id = ?", (cn,)
    )
    conn.commit()
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    assert "restored" not in c
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    rows = conn.execute(
        "SELECT delinked FROM payment_ledger_entry WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ?",
        (cn,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["delinked"] == 1


def test_credit_note_takes_the_invoice_currency(conn, env):
    inv = _invoice(conn, env, "10")
    conn.execute(
        "UPDATE sales_invoice SET currency = ?, exchange_rate = ? WHERE id = ?",
        ("EUR", "1.10", inv),
    )
    conn.commit()
    cn = _credit_note(conn, env, inv, "2")
    from erpclaw_lib.query import Field, P, Q, Table
    _t = Table("sales_invoice")
    row = conn.execute(
        Q.from_(_t).select(Field("currency"), Field("exchange_rate"))
        .where(Field("id") == P()).get_sql(),
        (cn,),
    ).fetchone()
    assert row["currency"] == "EUR"
    assert row["exchange_rate"] == "1.10"
    r = _submit(conn, cn)
    assert is_ok(r), r
    _p = Table("payment_ledger_entry")
    ple = conn.execute(
        Q.from_(_p).select(Field("currency"))
        .where((Field("voucher_type") == P())
               & (Field("voucher_id") == P())
               & (Field("delinked") == P())).get_sql(),
        ("credit_note", cn, 0),
    ).fetchall()
    assert len(ple) == 3
    for _r in ple:
        assert _r["currency"] == "EUR"
    _green(conn)


def test_ledger_check_by_install_phase(db_path):
    if get_dialect() == "postgresql":
        pytest.skip("SQLite only: the install-phase ledger refusal is SQLite-guarded")
    from erpclaw_lib import authority_gate
    from erpclaw_lib.authority_sink import LedgerWriteRefused
    from erpclaw_lib.db import get_connection as _fresh_connection
    from erpclaw_lib.query import Field, P, Q, Table
    from selling_helpers import build_selling_env

    def _open():
        return _fresh_connection(db_path)

    def _run(fn, args):
        _c = _open()
        try:
            out = call_action(fn, _c, args)
        except BaseException:
            try:
                _c.rollback()
            except Exception:
                pass
            try:
                _c.close()
            except Exception:
                pass
            raise
        try:
            _c.close()
        except Exception:
            pass
        return out

    def _read(fn):
        _c = _open()
        try:
            return fn(_c)
        finally:
            try:
                _c.close()
            except Exception:
                pass

    def _state(_id):
        def _q(_c):
            _t = Table("sales_invoice")
            return _c.execute(
                Q.from_(_t).select(
                    Field("status"), Field("naming_series"),
                    Field("outstanding_amount"))
                .where(Field("id") == P()).get_sql(),
                (_id,),
            ).fetchone()
        _r = _read(_q)
        return (_r["outstanding_amount"], _r["status"], _r["naming_series"])

    def _ledger_counts(_id):
        def _q(_c):
            out = {}
            for _tbl in ("gl_entry", "stock_ledger_entry", "payment_ledger_entry"):
                _t = Table(_tbl)
                out[_tbl] = len(_c.execute(
                    Q.from_(_t).select(Field("id"))
                    .where((Field("voucher_type") == P())
                           & (Field("voucher_id") == P())).get_sql(),
                    ("credit_note", _id),
                ).fetchall())
            return out
        return _read(_q)

    def _set_phase(phase):
        _c = _open()
        try:
            _c.execute(
                Q.update(Table("authority_install"))
                .set(Field("phase"), P()).get_sql(),
                (phase,),
            )
            _c.commit()
        finally:
            _c.close()

    _c = _open()
    try:
        env = build_selling_env(_c)
    finally:
        _c.close()

    _items = json.dumps(
        [{"item_id": env["item1"], "qty": "10", "rate": "100.00",
          "warehouse_id": env["warehouse"]}]
    )
    made = _run(
        mod.create_sales_invoice,
        ns(sales_order_id=None, delivery_note_id=None,
           customer_id=env["customer"], company_id=env["company_id"],
           posting_date="2026-06-20", due_date="2026-07-20",
           items=_items, tax_template_id=None, payment_terms_id=None),
    )
    assert is_ok(made), made
    inv = made["sales_invoice_id"]
    assert is_ok(_run(mod.submit_sales_invoice, ns(sales_invoice_id=inv)))

    _alloc = json.dumps(
        [{"voucher_type": "sales_invoice", "voucher_id": inv,
          "allocated_amount": "400.00"}]
    )
    taken = _run(
        pay.add_payment,
        ns(company_id=env["company_id"], payment_type="receive",
           posting_date="2026-06-25", party_type="customer",
           party_id=env["customer"],
           paid_from_account=env["ar"], paid_to_account=env["cash"],
           paid_amount="400.00", exchange_rate=None, payment_currency=None,
           reference_number=None, reference_date=None,
           allocations=_alloc, deductions=None),
    )
    assert is_ok(taken), taken
    assert is_ok(_run(
        pay.submit_payment,
        ns(payment_entry_id=taken["payment_entry_id"]),
    ))

    def _make_note(qty):
        _cn_items = json.dumps(
            [{"item_id": env["item1"], "qty": qty, "rate": "100.00"}]
        )
        created = _run(
            mod.create_credit_note,
            ns(against_invoice_id=inv, reason="Returned goods",
               posting_date="2026-06-28", items=_cn_items),
        )
        assert is_ok(created), created
        return created["credit_note_id"]

    cn1 = _make_note("2")
    cn2 = _make_note("3")

    assert _read(lambda _c: authority_gate.install_phase(_c)[0]) == "STAGED"
    assert len(_read(
        lambda _c: _c.execute(
            Q.from_(Table("authority_install"))
            .select(Field("install_id")).get_sql()
        ).fetchall()
    )) == 1

    r1 = _run(mod.submit_sales_invoice, ns(sales_invoice_id=cn1))
    assert is_ok(r1), r1

    _set_phase("ACTIVE")
    with pytest.raises(LedgerWriteRefused) as excinfo:
        _run(mod.submit_sales_invoice, ns(sales_invoice_id=cn2))
    assert excinfo.value.args == ("AUTHORITY_NOT_READY",)

    assert _state(cn2) == ("-300.00", "draft", None)
    assert _ledger_counts(cn2) == {
        "gl_entry": 0, "stock_ledger_entry": 0, "payment_ledger_entry": 0}
    assert _state(inv)[:2] == ("400.00", "partially_paid")

    _before_inv = _state(inv)
    _before_cn1 = _state(cn1)
    _before_counts = _ledger_counts(cn1)
    with pytest.raises(LedgerWriteRefused) as excinfo2:
        _run(mod.cancel_sales_invoice, ns(sales_invoice_id=cn1))
    assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
    assert _state(inv) == _before_inv
    assert _state(cn1) == _before_cn1
    assert _ledger_counts(cn1) == _before_counts

    _set_phase("STAGED")
    r2 = _run(mod.submit_sales_invoice, ns(sales_invoice_id=cn2))
    assert is_ok(r2), r2
    assert _state(inv)[:2] == ("100.00", "partially_paid")


def test_check_overdue_lists_what_is_still_owed(conn, env):
    _rep = _load("db_query_reports_cna13", "erpclaw-reports/db_query.py")
    inv = _invoice(conn, env, "10")
    _pay(conn, env, inv, "400.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, inv) == ("400.00", "partially_paid")
    got = call_action(
        _rep.check_overdue, conn,
        ns(company_id=env["company_id"], company_name=None),
    )
    assert is_ok(got), got
    assert got["total_overdue"] == "400.00"
    assert len(got["invoices"]) == 1
    assert got["invoices"][0]["id"] == inv
    assert got["invoices"][0]["outstanding"] == "400.00"
    assert all(_e["id"] != cn for _e in got["invoices"])
    _green(conn)


def test_dunning_chases_what_is_still_owed(conn, env):
    conn.execute(
        "INSERT INTO dunning_level (id, company_id, level, days_overdue, action, template_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), env["company_id"], 1, 1, "call", None),
    )
    conn.commit()
    inv_a = _invoice(conn, env, "2")
    cn_a = _credit_note(conn, env, inv_a, "2")
    assert is_ok(_submit(conn, cn_a)), cn_a
    assert _doc(conn, inv_a) == ("0", "paid")
    inv_b = _invoice(conn, env, "10")
    _pay(conn, env, inv_b, "400.00")
    cn_b = _credit_note(conn, env, inv_b, "2")
    assert is_ok(_submit(conn, cn_b)), cn_b
    assert _doc(conn, inv_b) == ("400.00", "partially_paid")
    ran = call_action(
        mod.run_dunning_cycle, conn,
        ns(company_id=env["company_id"], run_date="2026-09-01"),
    )
    assert is_ok(ran), ran
    from erpclaw_lib.query import Field, Q, Table
    rows = conn.execute(
        Q.from_(Table("dunning_run")).select(Field("invoice_ids_json")).get_sql()
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["invoice_ids_json"]) == [inv_b]
    _green(conn)
