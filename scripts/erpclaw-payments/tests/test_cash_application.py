"""Cash application proposes matches and creates only reviewed receipt drafts."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from decimal import Decimal

import pytest

from payments_helpers import build_ar_env, call_action, load_db_query, ns, seed_sales_invoice
from erpclaw_lib.query import Q, P, Table, update_row

mod = load_db_query()


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def args(env, **changes):
    fields = dict(company_id=env["company_id"], party_id=env["customer"],
                  paid_from_account=env["ar"], paid_to_account=env["bank"],
                  paid_amount="100.01", payment_currency="USD", posting_date="2026-06-01",
                  reference_number=None, reference_date=None, exchange_rate="1",
                  allocations=None, deductions=None, reviewed_allocations=None)
    fields.update(changes)
    return ns(**fields)


def change(conn, table, row_id, **values):
    sql = update_row(table, data={k: P() for k in values}, where={"id": P()})
    conn.execute(sql, (*values.values(), row_id))
    conn.commit()


def snapshot(conn):
    return {name: [tuple(row) for row in conn.execute(Q.from_(Table(name)).select("*").get_sql())]
            for name in ("payment_entry", "payment_allocation", "payment_ledger_entry",
                         "gl_entry", "sales_invoice", "naming_series", "audit_log")}


def reviewed(invoice_id, amount="100.01"):
    return json.dumps([dict(invoice_id=invoice_id, allocated_amount=amount)])


def test_preview_reference_priority_exact_decimal_and_no_writes(conn, env):
    older = seed_sales_invoice(conn, env, "60.00")
    exact = seed_sales_invoice(conn, env, "100.01")
    target = seed_sales_invoice(conn, env, "40.01")
    change(conn, "sales_invoice", older, due_date="2026-06-02")
    change(conn, "sales_invoice", target, naming_series="INV-REFERENCE", due_date="2026-07-01")
    before = snapshot(conn)
    result = call_action(mod.preview_cash_application, conn, args(env, reference_number="INV-REFERENCE"))
    assert result["requires_review"] is True
    assert result["proposed_allocations"] == [dict(invoice_id=target, allocated_amount="40.01"),
                                              dict(invoice_id=exact, allocated_amount="60.00")]
    assert result["unallocated_amount"] == "0.00"
    assert snapshot(conn) == before


def test_preview_exact_amount_then_oldest_and_retains_unallocated(conn, env):
    first = seed_sales_invoice(conn, env, "0.10")
    second = seed_sales_invoice(conn, env, "0.20")
    change(conn, "sales_invoice", second, due_date="2026-05-01")
    preview = call_action(mod.preview_cash_application, conn, args(env, paid_amount="0.20"))
    assert preview["proposed_allocations"] == [dict(invoice_id=second, allocated_amount="0.20")]
    result = call_action(mod.preview_cash_application, conn, args(env, paid_amount="0.40"))
    assert result["proposed_allocations"] == [dict(invoice_id=second, allocated_amount="0.20"),
                                              dict(invoice_id=first, allocated_amount="0.10")]
    assert result["unallocated_amount"] == "0.10"


def test_draft_reuses_payment_lifecycle_without_posting(conn, env):
    invoice = seed_sales_invoice(conn, env, "100.01")
    before = snapshot(conn)
    result = call_action(mod.create_cash_application_payment, conn,
                         args(env, paid_amount="120.01", reviewed_allocations=reviewed(invoice)))
    assert result["status"] == "ok"
    pe = Table("payment_entry")
    row = conn.execute(Q.from_(pe).select(pe.star).where(pe.id == P()).get_sql(),
                       (result["payment_entry_id"],)).fetchone()
    assert row["status"] == "draft" and row["payment_type"] == "receive"
    assert Decimal(row["paid_amount"]) == Decimal("120.01")
    assert Decimal(row["unallocated_amount"]) == Decimal("20.00")
    after = snapshot(conn)
    for name in ("sales_invoice", "gl_entry", "payment_ledger_entry"):
        assert after[name] == before[name]
    assert len(after["payment_allocation"]) == 1


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "NaN", "Infinity", "1e2", " 1", 1.2])
def test_refuses_inexact_or_invalid_money_without_writes(conn, env, amount):
    before = snapshot(conn)
    assert call_action(mod.preview_cash_application, conn, args(env, paid_amount=amount))["status"] == "error"
    assert snapshot(conn) == before


@pytest.mark.parametrize("field,value", [("disabled", 1), ("is_frozen", 1), ("is_group", 1),
                                          ("currency", "EUR"), ("account_type", "receivable")])
def test_refuses_unusable_bank_account(conn, env, field, value):
    change(conn, "account", env["bank"], **{field: value})
    before = snapshot(conn)
    assert call_action(mod.preview_cash_application, conn, args(env))["status"] == "error"
    assert snapshot(conn) == before


def test_customer_and_accounts_must_belong_to_company(conn, env):
    other = build_ar_env(conn)
    for changes in (dict(party_id=other["customer"]), dict(paid_to_account=other["bank"]),
                    dict(paid_from_account=other["ar"]), dict(payment_currency="EUR")):
        assert call_action(mod.preview_cash_application, conn, args(env, **changes))["status"] == "error"


def test_preview_excludes_foreign_draft_return_and_currency_invoices(conn, env):
    other = build_ar_env(conn)
    seed_sales_invoice(conn, other, "100.01")
    seed_sales_invoice(conn, env, "100.01", status="draft")
    returned = seed_sales_invoice(conn, env, "100.01")
    foreign_currency = seed_sales_invoice(conn, env, "100.01")
    change(conn, "sales_invoice", returned, is_return=1)
    change(conn, "sales_invoice", foreign_currency, currency="EUR")
    result = call_action(mod.preview_cash_application, conn, args(env))
    assert result["candidates"] == [] and result["unallocated_amount"] == "100.01"


@pytest.mark.parametrize("case", ["missing", "duplicate", "overpaid", "stale", "foreign", "numeric"])
def test_reviewed_allocations_are_explicit_current_and_scoped(conn, env, case):
    invoice = seed_sales_invoice(conn, env, "100.01")
    values = reviewed(invoice)
    if case == "missing":
        values = None
    elif case == "duplicate":
        values = json.dumps([dict(invoice_id=invoice, allocated_amount="10.00")] * 2)
    elif case == "overpaid":
        values = reviewed(invoice, "100.02")
    elif case == "stale":
        change(conn, "sales_invoice", invoice, outstanding_amount="0.01")
    elif case == "foreign":
        values = reviewed(seed_sales_invoice(conn, build_ar_env(conn), "100.01"))
    elif case == "numeric":
        values = reviewed(invoice, 100.01)
    before = snapshot(conn)
    assert call_action(mod.create_cash_application_payment, conn,
                       args(env, reviewed_allocations=values))["status"] == "error"
    assert snapshot(conn) == before


def test_total_allocations_cannot_exceed_receipt(conn, env):
    invoice = seed_sales_invoice(conn, env, "100.01")
    before = snapshot(conn)
    result = call_action(mod.create_cash_application_payment, conn,
                         args(env, paid_amount="50.00", reviewed_allocations=reviewed(invoice)))
    assert result["status"] == "error" and snapshot(conn) == before


def test_late_draft_failure_rolls_back(conn, env, monkeypatch):
    invoice = seed_sales_invoice(conn, env, "100.01")
    before = snapshot(conn)
    def fail(*args, **kwargs):
        raise ValueError("planted draft audit failure")
    monkeypatch.setattr(mod, "audit", fail)
    with pytest.raises(ValueError, match="planted"):
        mod.create_cash_application_payment(conn, args(env, reviewed_allocations=reviewed(invoice)))
    assert snapshot(conn) == before


def test_reviewed_draft_uses_ordinary_submit_and_cancel(conn, env):
    invoice = seed_sales_invoice(conn, env, "100.01")
    created = call_action(mod.create_cash_application_payment, conn,
                          args(env, reviewed_allocations=reviewed(invoice)))
    payment_id = created["payment_entry_id"]
    assert call_action(mod.submit_payment, conn, ns(payment_entry_id=payment_id))["status"] == "ok"
    table = Table("sales_invoice")
    read = Q.from_(table).select(table.outstanding_amount, table.status).where(table.id == P())
    settled = conn.execute(read.get_sql(), (invoice,)).fetchone()
    assert Decimal(settled["outstanding_amount"]) == Decimal("0.00")
    assert settled["status"] == "paid"
    assert call_action(mod.cancel_payment, conn, ns(payment_entry_id=payment_id))["status"] == "ok"
    restored = conn.execute(read.get_sql(), (invoice,)).fetchone()
    assert Decimal(restored["outstanding_amount"]) == Decimal("100.01")
    assert restored["status"] == "submitted"
    gl = Table("gl_entry")
    legs = conn.execute(Q.from_(gl).select(gl.debit, gl.credit).get_sql()).fetchall()
    assert sum((Decimal(r["debit"]) for r in legs), Decimal("0")) == sum(
        (Decimal(r["credit"]) for r in legs), Decimal("0"))


def test_root_routes():
    root = Path(__file__).resolve().parents[2] / "db_query.py"
    spec = importlib.util.spec_from_file_location("cash_application_root", root)
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    assert router.ACTION_MAP["preview-cash-application"] == "erpclaw-payments"
    assert router.ACTION_MAP["create-cash-application-payment"] == "erpclaw-payments"


def test_actual_root_routed_preview_and_draft(conn, db_path, env):
    invoice = seed_sales_invoice(conn, env, "100.01")
    root = Path(__file__).resolve().parents[2] / "db_query.py"
    common = [sys.executable, str(root), "--db-path", str(db_path),
              "--company-id", env["company_id"], "--party-id", env["customer"],
              "--paid-from-account", env["ar"], "--paid-to-account", env["bank"],
              "--paid-amount", "100.01", "--payment-currency", "USD"]
    environment = dict(os.environ, ERPCLAW_HOME=str(root.parent / "erpclaw-setup"),
                       PYTHONPATH=str(root.parent / "erpclaw-setup" / "lib"))
    before = snapshot(conn)
    preview = subprocess.run(common + ["--action", "preview-cash-application"],
                             env=environment, capture_output=True, text=True, timeout=30)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout)["proposed_allocations"] == [
        dict(invoice_id=invoice, allocated_amount="100.01")]
    assert snapshot(conn) == before
    draft = subprocess.run(common + ["--action", "create-cash-application-payment",
                           "--posting-date", "2026-06-01", "--reviewed-allocations", reviewed(invoice)],
                           env=environment, capture_output=True, text=True, timeout=30)
    assert draft.returncode == 0, draft.stdout + draft.stderr
    assert json.loads(draft.stdout)["document_status"] == "created"
    after = snapshot(conn)
    assert len(after["payment_entry"]) == 1
    for name in ("sales_invoice", "gl_entry", "payment_ledger_entry"):
        assert after[name] == before[name]
