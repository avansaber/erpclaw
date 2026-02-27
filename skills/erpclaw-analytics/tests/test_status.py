"""Tests for status and available-metrics actions — 4 tests."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import _call_action, create_test_company
from db_query import action_status, action_available_metrics


class TestStatus:
    def test_status_lists_modules(self, fresh_db):
        """STA-01: status returns installed/not_installed module lists."""
        result = _call_action(action_status, fresh_db)
        assert result["status"] == "ok"
        assert result["installed_count"] > 0
        assert "installed" in result
        assert "not_installed" in result
        assert result["total_modules"] == 19

    def test_status_with_company(self, fresh_db):
        """STA-02: status includes company stats when company-id given."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_status, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert "company_stats" in result
        assert result["company_stats"]["gl_entries"] == 0


class TestAvailableMetrics:
    def test_available_metrics_lists_all(self, fresh_db):
        """STA-03: available-metrics returns all 25 actions."""
        result = _call_action(action_available_metrics, fresh_db)
        assert result["status"] == "ok"
        total = result["available_count"] + result["unavailable_count"]
        assert total == 25

    def test_available_metrics_core_always_available(self, fresh_db):
        """STA-04: Core actions (status, ratios, expenses) always available."""
        result = _call_action(action_available_metrics, fresh_db)
        available_names = [a["action"] for a in result["available"]]
        for action in ["status", "available-metrics", "liquidity-ratios",
                       "profitability-ratios", "expense-breakdown", "cost-trend"]:
            assert action in available_names, f"{action} should be available"
