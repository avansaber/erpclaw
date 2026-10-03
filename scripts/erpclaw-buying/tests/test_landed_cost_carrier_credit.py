"""m806: a landed-cost charge credits only an account that can hold the carrier's bill.

Every charge's credit account is checked for company, group, disabled and kind
before anything is written; the response notes that the carrier's bill must
already be recorded against that account. Stock valuation is unchanged.

All money assertions are exact Decimal, never float.
"""
import json

import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid,
    seed_account, seed_company, seed_fiscal_year, seed_cost_center,
    seed_supplier, seed_item, seed_warehouse, seed_naming_series,
)

mod = load_db_query()

CARRIER_NOTE = (
    "The carrier's bill for '{desc}' must already be recorded against "
    "'{acct}'; this voucher moves that cost into stock and records nothing owed."
)
KIND_REFUSAL = (
    "Charge {i}: account '{name}' ({kind}) cannot hold a carrier's bill; "
    "credit the expense account the carrier's bill was recorded against, "
    "or an accrual liability the bill will clear"
)


def _submitted_receipt(conn, env, item_id, qty="10", rate="50.00"):
    items = json.dumps([{"item_id": item_id, "qty": qty, "rate": rate,
                         "warehouse_id": env["warehouse"]}])
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit_po = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit_po), f"PO submit failed: {submit_po}"
    pr = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po["purchase_order_id"], company_id=env["company_id"],
        posting_date="2026-06-20", items=None, purchase_receipt_id=None,
    ))
    assert is_ok(pr), f"PR creation failed: {pr}"
    submit_pr = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr["purchase_receipt_id"],
    ))
    assert is_ok(submit_pr), f"PR submit failed: {submit_pr}"
    return pr["purchase_receipt_id"]


def _add_lcv(conn, company_id, pr_ids, charges):
    return call_action(mod.add_landed_cost_voucher, conn, ns(
        purchase_receipt_ids=json.dumps(pr_ids),
        charges=json.dumps(charges),
        company_id=company_id,
    ))


def _counts(conn):
    out = {}
    for table in ("landed_cost_voucher", "landed_cost_charge",
                  "landed_cost_item", "gl_entry", "stock_ledger_entry",
                  "audit_log"):
        out[table] = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    return out


def _msg(result):
    return result.get("message", result.get("error", ""))


def _seed_carrier_accounts(conn, company_id):
    freight_in = seed_account(conn, company_id, "Freight In",
                              "expense", "expense", f"FR-{_uuid()[:6]}")
    accrual = seed_account(conn, company_id, "Freight Accrual",
                           "liability", None, f"FA-{_uuid()[:6]}")
    bank = seed_account(conn, company_id, "Bank A",
                        "asset", "bank", f"BK-{_uuid()[:6]}")
    tax = seed_account(conn, company_id, "Sales Tax Payable",
                       "liability", "tax", f"TX-{_uuid()[:6]}")
    temp = seed_account(conn, company_id, "Opening Balance Equity",
                        "equity", "temporary", f"EQ-{_uuid()[:6]}")
    group = seed_account(conn, company_id, "Group Freight",
                         "expense", "expense", f"GR-{_uuid()[:6]}")
    conn.execute("UPDATE account SET is_group = 1 WHERE id = ?", (group,))
    disabled = seed_account(conn, company_id, "Disabled Freight",
                            "expense", "expense", f"DI-{_uuid()[:6]}")
    conn.execute("UPDATE account SET disabled = 1 WHERE id = ?", (disabled,))
    conn.commit()
    return {"freight_in": freight_in, "accrual": accrual, "bank": bank,
            "tax": tax, "temp": temp, "group": group, "disabled": disabled}


def _assert_nothing_written(conn, before, result, expected):
    assert is_error(result), f"expected refusal, got: {result}"
    assert _msg(result) == expected, f"got: {_msg(result)!r}"
    assert _counts(conn) == before


class TestEnvExpenseAccountAccepted:
    def test_buying_env_expense_is_ordinary_expense(self, conn, env):
        row = conn.execute(
            "SELECT root_type, account_type FROM account WHERE id = ?",
            (env["expense"],)).fetchone()
        assert row["root_type"] == "expense"
        assert row["account_type"] == "expense"


class TestAcceptedExpense:
    def test_posts_stock_and_note(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["freight_in"]},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        assert result["total_landed_cost"] == "200.00"
        assert result["gl_entries_created"] == 2
        assert result["sle_repricings"] == 1
        assert result["notes"] == [CARRIER_NOTE.format(
            desc="Ocean freight", acct="Freight In")]

        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_type = 'landed_cost_voucher' "
            "AND voucher_id = ? ORDER BY account_id, debit, credit",
            (result["landed_cost_voucher_id"],)).fetchall()
        assert len(rows) == 2
        by_acct = {r["account_id"]: r for r in rows}
        stock_row = by_acct[env["stock_acct"]]
        credit_row = by_acct[accts["freight_in"]]
        assert Decimal(stock_row["debit"]) == Decimal("200.00")
        assert Decimal(stock_row["credit"]) == Decimal("0")
        assert Decimal(credit_row["credit"]) == Decimal("200.00")
        assert Decimal(credit_row["debit"]) == Decimal("0")
        assert credit_row["cost_center_id"] == env["cc"]

        items = conn.execute(
            "SELECT * FROM landed_cost_item WHERE landed_cost_voucher_id = ?",
            (result["landed_cost_voucher_id"],)).fetchall()
        assert len(items) == 1
        assert Decimal(items[0]["applicable_charges"]) == Decimal("200.00")
        assert Decimal(items[0]["original_rate"]) == Decimal("50.00")
        assert Decimal(items[0]["final_rate"]) == Decimal("70.00")

        sle = conn.execute(
            "SELECT * FROM stock_ledger_entry "
            "WHERE voucher_type = 'landed_cost_voucher' AND voucher_id = ?",
            (result["landed_cost_voucher_id"],)).fetchall()
        assert len(sle) == 1
        assert Decimal(sle[0]["stock_value_difference"]) == Decimal("200.00")
        assert Decimal(sle[0]["valuation_rate"]) == Decimal("70.00")
        assert Decimal(sle[0]["stock_value"]) == Decimal("700.00")


class TestAcceptedAccrualLiability:
    def test_posts_accrual_and_note(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["accrual"]},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        assert result["total_landed_cost"] == "200.00"
        assert result["notes"] == [CARRIER_NOTE.format(
            desc="Ocean freight", acct="Freight Accrual")]
        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_type = 'landed_cost_voucher' "
            "AND voucher_id = ? ORDER BY account_id, debit, credit",
            (result["landed_cost_voucher_id"],)).fetchall()
        assert len(rows) == 2
        by_acct = {r["account_id"]: r for r in rows}
        assert Decimal(by_acct[env["stock_acct"]]["debit"]) == Decimal("200.00")
        assert Decimal(by_acct[accts["accrual"]]["credit"]) == Decimal("200.00")
        assert by_acct[accts["accrual"]]["cost_center_id"] == env["cc"]


class TestRefusedNothingWritten:
    def test_bank_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["bank"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="Bank A", kind="bank"))

    def test_tax_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["tax"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="Sales Tax Payable", kind="tax"))

    def test_equity_temporary_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["temp"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="Opening Balance Equity", kind="temporary"))

    def test_cogs_refused(self, conn, env):
        _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": env["cogs"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="COGS", kind="cost of goods sold"))

    def test_stock_account_refused(self, conn, env):
        _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": env["stock_acct"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="Stock In Hand", kind="stock"))

    def test_payables_refused(self, conn, env):
        _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": env["ap"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=0, name="Accounts Payable", kind="payable"))

    def test_other_company_refused(self, conn, env):
        _seed_carrier_accounts(conn, env["company_id"])
        other = seed_company(conn, "Other Co")
        other_freight = seed_account(conn, other, "Freight In",
                                     "expense", "expense",
                                     f"OF-{_uuid()[:6]}")
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": other_freight},
        ])
        _assert_nothing_written(
            conn, before, result,
            "Charge 0: account 'Freight In' belongs to a different company")

    def test_disabled_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["disabled"]},
        ])
        _assert_nothing_written(
            conn, before, result,
            "Charge 0: account 'Disabled Freight' is disabled")

    def test_group_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["group"]},
        ])
        _assert_nothing_written(
            conn, before, result,
            "Charge 0: account 'Group Freight' is a group account")

    def test_second_charge_bank_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["freight_in"]},
            {"description": "Handling", "amount": "50.00",
             "expense_account_id": accts["bank"]},
        ])
        _assert_nothing_written(conn, before, result, KIND_REFUSAL.format(
            i=1, name="Bank A", kind="bank"))

    def test_missing_stock_account_refused(self, conn, env):
        accts = _seed_carrier_accounts(conn, env["company_id"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        conn.execute("UPDATE account SET account_type = 'expense' WHERE id = ?",
                     (env["stock_acct"],))
        conn.commit()
        before = _counts(conn)
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["freight_in"]},
        ])
        assert is_error(result), f"expected refusal, got: {result}"
        assert "No Stock-in-Hand account" in _msg(result)
        assert _counts(conn) == before


class TestSecondCompanyDecoy:
    def test_first_company_voucher_touches_only_first_company(self, conn, env):
        from buying_helpers import build_buying_env
        accts = _seed_carrier_accounts(conn, env["company_id"])
        env2 = build_buying_env(conn)
        _submitted_receipt(conn, env2, env2["item1"])
        pr_id = _submitted_receipt(conn, env, env["item1"])
        result = _add_lcv(conn, env["company_id"], [pr_id], [
            {"description": "Ocean freight", "amount": "200.00",
             "expense_account_id": accts["freight_in"]},
        ])
        assert is_ok(result), f"LCV failed: {result}"
        rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_type = 'landed_cost_voucher' "
            "AND voucher_id = ?", (result["landed_cost_voucher_id"],)).fetchall()
        assert len(rows) == 2
        for row in rows:
            acct = conn.execute("SELECT company_id FROM account WHERE id = ?",
                                (row["account_id"],)).fetchone()
            assert acct["company_id"] == env["company_id"]
        assert {r["account_id"] for r in rows} == {
            env["stock_acct"], accts["freight_in"]}
