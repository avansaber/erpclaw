"""Tests for erpclaw-accounting-adv Intercompany Transactions actions.

Actions tested: add-ic-transaction, update-ic-transaction, get-ic-transaction,
                list-ic-transactions, approve-ic-transaction, post-ic-transaction,
                add-transfer-price-rule, list-transfer-price-rules,
                ic-reconciliation-report, ic-elimination-report
"""
import json
import pytest
from decimal import Decimal
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_ic_txn(conn, env, amount="50000.00", transaction_type="sale"):
    return call_action(mod.add_ic_transaction, conn, ns(
        company_id=env["company_id"],
        from_company_id=env["company_id"],
        to_company_id=env["company2_id"],
        transaction_type=transaction_type,
        amount=amount, description="Intercompany sale of services",
        currency="USD", transfer_price_method="cost_plus",
    ))


# ──────────────────────────────────────────────────────────────────────────────
# IC Transactions
# ──────────────────────────────────────────────────────────────────────────────

class TestAddIcTransaction:
    def test_basic_create(self, conn, env):
        result = _add_ic_txn(conn, env)
        assert is_ok(result)
        assert result["transaction_type"] == "sale"
        assert result["ic_status"] == "draft"
        assert result["amount"] == "50000.00"

    def test_same_company_fails(self, conn, env):
        result = call_action(mod.add_ic_transaction, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company_id"],
            transaction_type="sale", amount="1000",
            description=None, currency=None,
            transfer_price_method=None,
        ))
        assert is_error(result)

    def test_missing_type_fails(self, conn, env):
        result = call_action(mod.add_ic_transaction, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company2_id"],
            transaction_type=None, amount="1000",
            description=None, currency=None,
            transfer_price_method=None,
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_ic_transaction, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company2_id"],
            transaction_type="invalid", amount="1000",
            description=None, currency=None,
            transfer_price_method=None,
        ))
        assert is_error(result)

    def test_zero_amount_fails(self, conn, env):
        result = call_action(mod.add_ic_transaction, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company2_id"],
            transaction_type="sale", amount="0",
            description=None, currency=None,
            transfer_price_method=None,
        ))
        assert is_error(result)


class TestUpdateIcTransaction:
    def test_update_description(self, conn, env):
        ic = _add_ic_txn(conn, env)
        result = call_action(mod.update_ic_transaction, conn, ns(
            id=ic["id"], description="Updated description",
            amount=None, currency=None,
            transaction_type=None, transfer_price_method=None,
        ))
        assert is_ok(result)
        assert "description" in result["updated_fields"]

    def test_no_fields_fails(self, conn, env):
        ic = _add_ic_txn(conn, env)
        result = call_action(mod.update_ic_transaction, conn, ns(
            id=ic["id"], description=None,
            amount=None, currency=None,
            transaction_type=None, transfer_price_method=None,
        ))
        assert is_error(result)

    def test_update_posted_fails(self, conn, env):
        ic = _add_ic_txn(conn, env)
        call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
        result = call_action(mod.update_ic_transaction, conn, ns(
            id=ic["id"], description="Try update",
            amount=None, currency=None,
            transaction_type=None, transfer_price_method=None,
        ))
        assert is_error(result)


class TestGetIcTransaction:
    def test_get(self, conn, env):
        ic = _add_ic_txn(conn, env)
        result = call_action(mod.get_ic_transaction, conn, ns(id=ic["id"]))
        assert is_ok(result)
        assert result["from_company_id"] == env["company_id"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_ic_transaction, conn, ns(id="fake-id"))
        assert is_error(result)


class TestListIcTransactions:
    def test_list(self, conn, env):
        _add_ic_txn(conn, env)
        result = call_action(mod.list_ic_transactions, conn, ns(
            company_id=env["company_id"], from_company_id=None,
            to_company_id=None, transaction_type=None,
            ic_status=None, search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Approval & Posting Workflow
# ──────────────────────────────────────────────────────────────────────────────

class TestApproveIcTransaction:
    def test_approve(self, conn, env):
        ic = _add_ic_txn(conn, env)
        result = call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        assert is_ok(result)
        assert result["ic_status"] == "approved"

    def test_approve_posted_fails(self, conn, env):
        ic = _add_ic_txn(conn, env)
        call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
        result = call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        assert is_error(result)


class TestPostIcTransaction:
    def test_post(self, conn, env):
        ic = _add_ic_txn(conn, env)
        call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        result = call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
        assert is_ok(result)
        assert result["ic_status"] == "posted"
        assert result["posted_date"] is not None

    def test_post_draft_fails(self, conn, env):
        ic = _add_ic_txn(conn, env)
        result = call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Transfer Price Rules
# ──────────────────────────────────────────────────────────────────────────────

class TestAddTransferPriceRule:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_transfer_price_rule, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company2_id"],
            transaction_type="sale", method="cost_plus",
            markup_pct="15.00", effective_date="2026-01-01",
            expiry_date="2026-12-31",
        ))
        assert is_ok(result)
        assert result["method"] == "cost_plus"
        assert result["markup_pct"] == "15.00"

    def test_invalid_method_fails(self, conn, env):
        result = call_action(mod.add_transfer_price_rule, conn, ns(
            company_id=env["company_id"],
            from_company_id=None, to_company_id=None,
            transaction_type=None, method="invalid",
            markup_pct=None, effective_date=None,
            expiry_date=None,
        ))
        assert is_error(result)


class TestListTransferPriceRules:
    def test_list(self, conn, env):
        call_action(mod.add_transfer_price_rule, conn, ns(
            company_id=env["company_id"],
            from_company_id=None, to_company_id=None,
            transaction_type=None, method="cost_plus",
            markup_pct="10", effective_date=None,
            expiry_date=None,
        ))
        result = call_action(mod.list_transfer_price_rules, conn, ns(
            company_id=env["company_id"], from_company_id=None,
            to_company_id=None, transaction_type=None,
            limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestIcReconciliationReport:
    def test_report(self, conn, env):
        _add_ic_txn(conn, env)
        result = call_action(mod.ic_reconciliation_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert len(result["rows"]) >= 1


class TestIcEliminationReport:
    def test_report_with_posted(self, conn, env):
        ic = _add_ic_txn(conn, env)
        call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
        result = call_action(mod.ic_elimination_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["transaction_count"] >= 1
        assert Decimal(result["total_to_eliminate"]) == Decimal("50000.00")

    def test_report_empty_no_posted(self, conn, env):
        _add_ic_txn(conn, env)  # draft only
        result = call_action(mod.ic_elimination_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["transaction_count"] == 0
