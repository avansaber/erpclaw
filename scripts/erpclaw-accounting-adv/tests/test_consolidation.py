"""Tests for erpclaw-accounting-adv Multi-Entity Consolidation actions.

Actions tested: add-consolidation-group, list-consolidation-groups, add-group-entity,
                run-consolidation, generate-elimination-entries,
                add-currency-translation, consolidation-trial-balance-report,
                consolidation-summary
"""
import json
import pytest
from decimal import Decimal
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_group(conn, env, name="Global Holdings Group"):
    return call_action(mod.add_consolidation_group, conn, ns(
        company_id=env["company_id"], name=name,
        parent_company_id=env["company_id"],
        consolidation_currency="USD",
    ))


def _add_entity(conn, env, group_id, entity_company_id=None, entity_name="Subsidiary A",
                ownership_pct="100"):
    return call_action(mod.add_group_entity, conn, ns(
        group_id=group_id, company_id=env["company_id"],
        entity_company_id=entity_company_id or env["company_id"],
        entity_name=entity_name, ownership_pct=ownership_pct,
        functional_currency="USD", consolidation_method="full",
    ))


def _setup_group_with_entities(conn, env):
    """Create a group with two entities."""
    grp = _add_group(conn, env)
    _add_entity(conn, env, grp["id"], env["company_id"], "Parent Corp", "100")
    _add_entity(conn, env, grp["id"], env["company2_id"], "Subsidiary Inc", "80")
    return grp


# ──────────────────────────────────────────────────────────────────────────────
# Consolidation Groups
# ──────────────────────────────────────────────────────────────────────────────

class TestAddConsolidationGroup:
    def test_basic_create(self, conn, env):
        result = _add_group(conn, env)
        assert is_ok(result)
        assert result["name"] == "Global Holdings Group"
        assert result["group_status"] == "active"

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_consolidation_group, conn, ns(
            company_id=env["company_id"], name=None,
            parent_company_id=None, consolidation_currency=None,
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn, env):
        result = call_action(mod.add_consolidation_group, conn, ns(
            company_id=None, name="Test Group",
            parent_company_id=None, consolidation_currency=None,
        ))
        assert is_error(result)


class TestListConsolidationGroups:
    def test_list(self, conn, env):
        _add_group(conn, env)
        result = call_action(mod.list_consolidation_groups, conn, ns(
            company_id=env["company_id"], group_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Group Entities
# ──────────────────────────────────────────────────────────────────────────────

class TestAddGroupEntity:
    def test_basic_create(self, conn, env):
        grp = _add_group(conn, env)
        result = _add_entity(conn, env, grp["id"], env["company2_id"],
                             "Subsidiary Inc", "80")
        assert is_ok(result)
        assert result["entity_name"] == "Subsidiary Inc"
        assert result["ownership_pct"] == "80"
        assert result["consolidation_method"] == "full"

    def test_missing_entity_name_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_group_entity, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            entity_company_id=env["company2_id"],
            entity_name=None, ownership_pct="100",
            functional_currency=None, consolidation_method=None,
        ))
        assert is_error(result)

    def test_invalid_consolidation_method_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_group_entity, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            entity_company_id=env["company2_id"],
            entity_name="Test Entity", ownership_pct="100",
            functional_currency=None, consolidation_method="invalid",
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Consolidation Operations
# ──────────────────────────────────────────────────────────────────────────────

class TestRunConsolidation:
    def test_run(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["consolidation_run"] == "completed"

    def test_no_entities_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_error(result)

    def test_missing_period_fails(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date=None,
        ))
        assert is_error(result)


class TestGenerateEliminationEntries:
    def test_generate_with_posted_ic(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        # Create and post an IC transaction between the two companies
        ic = call_action(mod.add_ic_transaction, conn, ns(
            company_id=env["company_id"],
            from_company_id=env["company_id"],
            to_company_id=env["company2_id"],
            transaction_type="sale", amount="25000.00",
            description="IC sale", currency="USD",
            transfer_price_method=None,
        ))
        call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
        call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))

        result = call_action(mod.generate_elimination_entries, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["entries_created"] >= 1

    def test_too_few_entities_fails(self, conn, env):
        grp = _add_group(conn, env)
        _add_entity(conn, env, grp["id"], env["company_id"], "Only One", "100")
        result = call_action(mod.generate_elimination_entries, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
            company_id=env["company_id"],
        ))
        assert is_error(result)


class TestAddCurrencyTranslation:
    def test_add(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="5000.00",
            debit_account="CTA Debit", credit_account="CTA Credit",
            description="EUR translation adjustment",
        ))
        assert is_ok(result)
        assert result["entry_type"] == "currency_translation"
        assert result["amount"] == "5000.00"

    def test_missing_amount_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount=None,
            debit_account=None, credit_account=None,
            description=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestConsolidationTrialBalanceReport:
    def test_report(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.consolidation_trial_balance_report, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["group_name"] == "Global Holdings Group"


class TestConsolidationSummary:
    def test_summary(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        # Add a currency translation entry so we have elimination data
        call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="3000.00",
            debit_account=None, credit_account=None,
            description=None,
        ))
        result = call_action(mod.consolidation_summary, conn, ns(
            group_id=grp["id"],
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["elimination_count"] >= 1
        assert "eliminations_by_type" in result
