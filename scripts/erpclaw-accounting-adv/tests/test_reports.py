"""Tests for erpclaw-accounting-adv cross-domain reports and status.

Actions tested: standards-compliance-dashboard, status
"""
import pytest
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


class TestStandardsComplianceDashboard:
    def test_dashboard(self, conn, env):
        result = call_action(mod.standards_compliance_dashboard, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "asc_606" in result
        assert "asc_842" in result
        assert "intercompany" in result
        assert "consolidation" in result

    def test_dashboard_no_company(self, conn, env):
        result = call_action(mod.standards_compliance_dashboard, conn, ns(
            company_id=None,
        ))
        assert is_ok(result)


class TestStatus:
    def test_status(self, conn, env):
        result = call_action(mod.status, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["skill"] == "erpclaw-accounting-adv"
        assert result["total_tables"] == 12
        assert "record_counts" in result
