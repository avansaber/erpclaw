"""Tests for erpclaw-gl cost centers, budgets, and naming series.

Actions tested:
  - add-cost-center, list-cost-centers
  - add-budget, list-budgets
  - seed-naming-series, next-series
"""
import pytest
from gl_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Cost Centers
# ──────────────────────────────────────────────────────────────────────────────

class TestAddCostCenter:
    def test_basic_create(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_cost_center, conn, ns(
            name="Engineering", company_id=cid,
            parent_id=None, is_group=False,
        ))
        assert is_ok(result)
        assert "cost_center_id" in result

    def test_hierarchical(self, conn):
        cid = seed_company(conn)
        parent = call_action(mod.add_cost_center, conn, ns(
            name="Operations", company_id=cid,
            parent_id=None, is_group=True,
        ))
        child = call_action(mod.add_cost_center, conn, ns(
            name="Manufacturing", company_id=cid,
            parent_id=parent["cost_center_id"], is_group=False,
        ))
        assert is_ok(child)
        row = conn.execute("SELECT parent_id FROM cost_center WHERE id=?",
                           (child["cost_center_id"],)).fetchone()
        assert row["parent_id"] == parent["cost_center_id"]

    def test_missing_name_fails(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_cost_center, conn, ns(
            name=None, company_id=cid,
            parent_id=None, is_group=False,
        ))
        assert is_error(result)


class TestListCostCenters:
    def test_list(self, conn):
        cid = seed_company(conn)
        seed_cost_center(conn, cid, "CC A")
        seed_cost_center(conn, cid, "CC B")
        result = call_action(mod.list_cost_centers, conn, ns(
            company_id=cid, parent_id=None, is_group=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2


# ──────────────────────────────────────────────────────────────────────────────
# Budgets
# ──────────────────────────────────────────────────────────────────────────────

class TestAddBudget:
    def test_budget_for_account(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid)
        acct = seed_account(conn, cid, "Travel", "expense", "expense", "5100")
        result = call_action(mod.add_budget, conn, ns(
            fiscal_year_id=fyid,
            account_id=acct,
            cost_center_id=None,
            budget_amount="50000.00",
            action_if_exceeded=None,
        ))
        assert is_ok(result)
        assert "budget_id" in result

    def test_budget_for_cost_center(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid)
        ccid = seed_cost_center(conn, cid, "R&D")
        result = call_action(mod.add_budget, conn, ns(
            fiscal_year_id=fyid,
            account_id=None,
            cost_center_id=ccid,
            budget_amount="100000.00",
            action_if_exceeded="stop",
        ))
        assert is_ok(result)

    def test_budget_missing_amount_fails(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid)
        result = call_action(mod.add_budget, conn, ns(
            fiscal_year_id=fyid,
            account_id=None,
            cost_center_id=None,
            budget_amount=None,
            action_if_exceeded=None,
        ))
        assert is_error(result)


class TestListBudgets:
    def test_list_with_variance(self, conn):
        cid = seed_company(conn)
        fyid = seed_fiscal_year(conn, cid)
        acct = seed_account(conn, cid, "Marketing", "expense", "expense", "5200")
        call_action(mod.add_budget, conn, ns(
            fiscal_year_id=fyid, account_id=acct,
            cost_center_id=None, budget_amount="25000.00",
            action_if_exceeded=None,
        ))
        result = call_action(mod.list_budgets, conn, ns(
            fiscal_year_id=fyid, account_id=None,
            cost_center_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Naming Series
# ──────────────────────────────────────────────────────────────────────────────

class TestSeedNamingSeries:
    def test_seed(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.seed_naming_series, conn, ns(company_id=cid))
        assert is_ok(result)
        assert result["series_created"] > 0

        # Verify in DB
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM naming_series WHERE company_id=?", (cid,)
        ).fetchone()["cnt"]
        assert count == result["series_created"]


class TestNextSeries:
    def test_next_series_increments(self, conn):
        cid = seed_company(conn)
        call_action(mod.seed_naming_series, conn, ns(company_id=cid))

        result1 = call_action(mod.next_series, conn, ns(
            entity_type="journal_entry", company_id=cid,
        ))
        assert is_ok(result1)
        assert "series" in result1

        result2 = call_action(mod.next_series, conn, ns(
            entity_type="journal_entry", company_id=cid,
        ))
        assert result2["series"] != result1["series"]  # Should be incremented
