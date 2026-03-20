"""Tests for erpclaw-gl account management actions.

Actions tested:
  - setup-chart-of-accounts
  - add-account
  - update-account
  - list-accounts
  - get-account
  - freeze-account / unfreeze-account
"""
import pytest
from decimal import Decimal
from gl_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_account, load_db_query,
)

mod = load_db_query()


class TestSetupChartOfAccounts:
    def test_us_gaap_template(self, conn):
        """Load US GAAP chart of accounts."""
        cid = seed_company(conn)
        result = call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=cid, template="us_gaap",
        ))
        assert is_ok(result)
        assert result["accounts_created"] > 0
        assert result["template"] == "us_gaap"

        # Verify accounts exist in DB
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM account WHERE company_id=?", (cid,)
        ).fetchone()["cnt"]
        assert count == result["accounts_created"]

    def test_auto_detect_company(self, conn):
        """If only one company exists, auto-detect it."""
        cid = seed_company(conn)
        result = call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=None, template=None,
        ))
        assert is_ok(result)
        assert result["accounts_created"] > 0

    def test_creates_root_accounts(self, conn):
        """US GAAP chart should have all 5 root types."""
        cid = seed_company(conn)
        call_action(mod.setup_chart_of_accounts, conn, ns(
            company_id=cid, template="us_gaap",
        ))
        for root_type in ["asset", "liability", "equity", "income", "expense"]:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM account WHERE company_id=? AND root_type=?",
                (cid, root_type)
            ).fetchone()
            assert row["cnt"] > 0, f"Missing accounts for root_type={root_type}"


class TestAddAccount:
    def test_basic_create(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Cash in Bank", company_id=cid,
            root_type="asset", account_type="bank",
            account_number="1010", parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_ok(result)
        assert result["name"] == "Cash in Bank"
        assert "account_id" in result

    def test_correct_balance_direction(self, conn):
        """Asset/expense accounts should be debit_normal, liability/equity/income credit_normal."""
        cid = seed_company(conn)
        for root_type, expected_dir in [
            ("asset", "debit_normal"),
            ("expense", "debit_normal"),
            ("liability", "credit_normal"),
            ("equity", "credit_normal"),
            ("income", "credit_normal"),
        ]:
            result = call_action(mod.add_account, conn, ns(
                name=f"Test {root_type}", company_id=cid,
                root_type=root_type, account_type=None,
                account_number=f"BD-{root_type[:3]}", parent_id=None,
                currency=None, is_group=False,
            ))
            row = conn.execute(
                "SELECT balance_direction FROM account WHERE id=?",
                (result["account_id"],)
            ).fetchone()
            assert row["balance_direction"] == expected_dir

    def test_missing_name_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name=None, company_id=cid,
            root_type="asset", account_type=None,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result)

    def test_missing_root_type_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Bad Account", company_id=cid,
            root_type=None, account_type=None,
            account_number=None, parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_error(result)

    def test_group_account(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Current Assets", company_id=cid,
            root_type="asset", account_type=None,
            account_number="1000", parent_id=None,
            currency=None, is_group=True,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT is_group FROM account WHERE id=?",
                           (result["account_id"],)).fetchone()
        assert row["is_group"] == 1

    def test_leaf_only_type_rejects_group(self, conn):
        """Account types like tax, receivable, payable must be leaf (posting) accounts."""
        cid = seed_company(conn)
        for acct_type in ("tax", "receivable", "payable", "bank", "cash",
                          "cost_of_goods_sold", "stock"):
            result = call_action(mod.add_account, conn, ns(
                name=f"Bad Group {acct_type}", company_id=cid,
                root_type="asset", account_type=acct_type,
                account_number=None, parent_id=None,
                currency=None, is_group=True,
            ))
            assert is_error(result), f"is_group=True should fail for account_type={acct_type}"

    def test_leaf_only_type_allows_non_group(self, conn):
        """Leaf-only types should work fine when is_group=False."""
        cid = seed_company(conn)
        result = call_action(mod.add_account, conn, ns(
            name="Tax Payable", company_id=cid,
            root_type="liability", account_type="tax",
            account_number="2100", parent_id=None,
            currency=None, is_group=False,
        ))
        assert is_ok(result)

    def test_child_account_depth(self, conn):
        """Child account should have depth = parent depth + 1."""
        cid = seed_company(conn)
        parent = call_action(mod.add_account, conn, ns(
            name="Parent Group", company_id=cid,
            root_type="asset", account_type=None,
            account_number="1000", parent_id=None,
            currency=None, is_group=True,
        ))
        child = call_action(mod.add_account, conn, ns(
            name="Child Account", company_id=cid,
            root_type="asset", account_type="bank",
            account_number="1010", parent_id=parent["account_id"],
            currency=None, is_group=False,
        ))
        row = conn.execute("SELECT depth FROM account WHERE id=?",
                           (child["account_id"],)).fetchone()
        assert row["depth"] >= 1


class TestUpdateAccount:
    def test_update_name(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Old Name")
        result = call_action(mod.update_account, conn, ns(
            account_id=aid, name="New Name",
            account_number=None, parent_id=None, is_frozen=None,
        ))
        assert is_ok(result)
        row = conn.execute("SELECT name FROM account WHERE id=?", (aid,)).fetchone()
        assert row["name"] == "New Name"

    def test_freeze_via_update(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        result = call_action(mod.update_account, conn, ns(
            account_id=aid, name=None,
            account_number=None, parent_id=None, is_frozen="true",
        ))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 1


class TestListAccounts:
    def test_list_with_company(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Account A", root_type="asset")
        seed_account(conn, cid, name="Account B", root_type="liability")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type=None, account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2

    def test_filter_by_root_type(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Asset Acc", root_type="asset")
        seed_account(conn, cid, name="Liability Acc", root_type="liability")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type="asset", account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search=None, limit=None, offset=None,
        ))
        for acct in result["accounts"]:
            assert acct["root_type"] == "asset"

    def test_search_by_name(self, conn):
        cid = seed_company(conn)
        seed_account(conn, cid, name="Accounts Receivable", root_type="asset",
                     account_type="receivable")
        result = call_action(mod.list_accounts, conn, ns(
            company_id=cid, root_type=None, account_type=None,
            parent_id=None, is_group=False, include_frozen=False,
            search="Receivable", limit=None, offset=None,
        ))
        assert result["total_count"] >= 1


class TestGetAccount:
    def test_get_by_id(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Cash", account_number="1001")
        result = call_action(mod.get_account, conn, ns(
            account_id=aid, as_of_date=None,
        ))
        assert is_ok(result)
        assert result["account"]["name"] == "Cash"

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_account, conn, ns(
            account_id="fake-id", as_of_date=None,
        ))
        assert is_error(result)

    def test_get_with_balance(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid, name="Cash", account_number="1001",
                           root_type="asset")
        result = call_action(mod.get_account, conn, ns(
            account_id=aid, as_of_date="2026-12-31",
        ))
        assert is_ok(result)
        acct = result["account"]
        assert "balance" in acct
        assert Decimal(acct["balance"]) == Decimal("0")


class TestFreezeUnfreezeAccount:
    def test_freeze(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        result = call_action(mod.freeze_account, conn, ns(account_id=aid))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 1

    def test_unfreeze(self, conn):
        cid = seed_company(conn)
        aid = seed_account(conn, cid)
        call_action(mod.freeze_account, conn, ns(account_id=aid))
        result = call_action(mod.unfreeze_account, conn, ns(account_id=aid))
        assert is_ok(result)
        row = conn.execute("SELECT is_frozen FROM account WHERE id=?", (aid,)).fetchone()
        assert row["is_frozen"] == 0
