"""Tests for the S3 CWIP purchase-invoice hook (AVA-43).

`--cwip-asset-id` on create-purchase-invoice routes the bill's expense GL to the
asset's capital_work_in_progress account and records a cwip_cost_accumulation row
in the submit transaction. Absent the flag, behaviour is unchanged (regression).
"""
import json
import uuid
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_account,
)

mod = load_db_query()


def _seed_cwip(conn, env, status="under_construction"):
    """Seed a CWIP account + an asset in `status`; return (cwip_acct, asset_id)."""
    cwip_acct = seed_account(conn, env["company_id"], "CWIP", "asset",
                             "capital_work_in_progress", "1800")
    cat_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO asset_category (id, name, company_id) VALUES (?, ?, ?)",
        (cat_id, f"Buildings {cat_id[:6]}", env["company_id"]))
    asset_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO asset (id, asset_name, naming_series, asset_category_id, "
        "gross_value, current_book_value, status, company_id) "
        "VALUES (?, ?, ?, ?, '0', '0', ?, ?)",
        (asset_id, "Project Alpha Building", "ASSET-CWIP-1", cat_id, status,
         env["company_id"]))
    conn.commit()
    return cwip_acct, asset_id


def _items(env, *specs):
    return json.dumps([{"item_id": env[k], "qty": q, "rate": r} for k, q, r in specs])


def _create_pi(conn, env, items, cwip_asset_id=None, purchase_order_id=None,
               purchase_receipt_id=None):
    return call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=purchase_order_id, purchase_receipt_id=purchase_receipt_id,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date=None,
        items=items, tax_template_id=None, cwip_asset_id=cwip_asset_id))


def _debit_to(conn, account_id, voucher_id):
    val = conn.execute(
        "SELECT COALESCE(SUM(CAST(debit AS REAL)), 0) FROM gl_entry "
        "WHERE account_id = ? AND voucher_id = ? AND is_cancelled = 0",
        (account_id, voucher_id)).fetchone()[0]
    return Decimal(str(val))


class TestCwipInvoiceHook:
    def test_happy_routes_gl_to_cwip_and_records_accumulation(self, conn, env):
        cwip_acct, asset_id = _seed_cwip(conn, env)
        create = _create_pi(conn, env, _items(env, ("item1", "5", "100.00")),
                            cwip_asset_id=asset_id)
        assert is_ok(create), create
        pi_id = create["purchase_invoice_id"]
        sub = call_action(mod.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
        assert is_ok(sub), sub
        assert sub["cwip_asset_id"] == asset_id
        assert sub.get("cwip_accumulation_id")
        # GL: DR CWIP 500, and the default expense account is untouched.
        assert _debit_to(conn, cwip_acct, pi_id) == Decimal("500")
        assert _debit_to(conn, env["expense"], pi_id) == Decimal("0")
        # Accumulation row recorded + asset carrying value bumped.
        accum = conn.execute(
            "SELECT accumulated_amount, source_voucher_type, gl_entry_id "
            "FROM cwip_cost_accumulation WHERE asset_id = ?", (asset_id,)).fetchone()
        assert Decimal(accum["accumulated_amount"]) == Decimal("500.00")
        assert accum["source_voucher_type"] == "purchase_invoice"
        assert accum["gl_entry_id"]
        book = conn.execute("SELECT current_book_value FROM asset WHERE id = ?",
                            (asset_id,)).fetchone()[0]
        assert Decimal(book) == Decimal("500.00")

    def test_reject_if_asset_not_under_construction(self, conn, env):
        _, asset_id = _seed_cwip(conn, env, status="in_use")
        create = _create_pi(conn, env, _items(env, ("item1", "1", "10.00")),
                            cwip_asset_id=asset_id)
        assert is_error(create)
        assert "under_construction" in (create.get("error", "") + create.get("message", ""))

    def test_reject_cwip_combined_with_po_link(self, conn, env):
        _, asset_id = _seed_cwip(conn, env)
        create = _create_pi(conn, env, _items(env, ("item1", "1", "10.00")),
                            cwip_asset_id=asset_id, purchase_order_id="some-po-id")
        assert is_error(create)

    def test_cancel_unwinds_accumulation_and_asset_value(self, conn, env):
        cwip_acct, asset_id = _seed_cwip(conn, env)
        create = _create_pi(conn, env, _items(env, ("item1", "2", "100.00")),
                            cwip_asset_id=asset_id)
        pi_id = create["purchase_invoice_id"]
        assert is_ok(call_action(mod.submit_purchase_invoice, conn,
                                 ns(purchase_invoice_id=pi_id)))
        cancel = call_action(mod.cancel_purchase_invoice, conn,
                             ns(purchase_invoice_id=pi_id))
        assert is_ok(cancel), cancel
        # Accumulation reversed, asset value back to 0, CWIP GL nets to 0.
        status = conn.execute(
            "SELECT status FROM cwip_cost_accumulation WHERE asset_id = ?",
            (asset_id,)).fetchone()[0]
        assert status == "reversed"
        book = conn.execute("SELECT current_book_value FROM asset WHERE id = ?",
                            (asset_id,)).fetchone()[0]
        assert Decimal(book) == Decimal("0")
        net = conn.execute(
            "SELECT COALESCE(SUM(CAST(debit AS REAL) - CAST(credit AS REAL)), 0) "
            "FROM gl_entry WHERE account_id = ? AND voucher_id = ?",
            (cwip_acct, pi_id)).fetchone()[0]
        assert Decimal(str(net)) == Decimal("0")

    def test_backward_compat_no_flag_unchanged(self, conn, env):
        create = _create_pi(conn, env, _items(env, ("item1", "5", "100.00")))
        assert is_ok(create)
        pi_id = create["purchase_invoice_id"]
        sub = call_action(mod.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
        assert is_ok(sub)
        assert "cwip_accumulation_id" not in sub
        # Expense account debited as before; no accumulation row created.
        assert _debit_to(conn, env["expense"], pi_id) == Decimal("500")
        n = conn.execute("SELECT COUNT(*) FROM cwip_cost_accumulation").fetchone()[0]
        assert n == 0
