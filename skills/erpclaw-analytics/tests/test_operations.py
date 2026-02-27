"""Tests for operations analytics — 4 tests."""
import sys
import os
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import _call_action, create_test_company
from db_query import action_project_profitability, action_quality_dashboard, action_support_metrics


def _create_project(conn, company_id, name, est_cost="0", act_cost="0",
                     total_billed="0", status="open"):
    """Insert a project for testing."""
    pid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO project (id, project_name, company_id, status,
           estimated_cost, actual_cost, total_billed)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pid, name, company_id, status,
         str(est_cost), str(act_cost), str(total_billed)),
    )
    conn.commit()
    return pid


def _create_item_for_qi(conn):
    """Create a minimal item for quality inspection FK."""
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO item (id, item_code, item_name, stock_uom) VALUES (?, ?, 'QI Item', 'Nos')",
        (item_id, f"QI-{item_id[:8]}"),
    )
    conn.commit()
    return item_id


def _create_inspection(conn, company_id, inspection_date, status_val, item_id=None):
    """Insert a quality_inspection for testing."""
    qi_id = str(uuid.uuid4())
    if not item_id:
        item_id = _create_item_for_qi(conn)
    conn.execute(
        """INSERT INTO quality_inspection (id, inspection_type, item_id,
           inspection_date, status)
           VALUES (?, 'incoming', ?, ?, ?)""",
        (qi_id, item_id, inspection_date, status_val),
    )
    conn.commit()
    return qi_id


def _create_customer_for_issue(conn, company_id):
    """Create a minimal customer for issue FK."""
    cust_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer (id, name, customer_type, company_id) VALUES (?, 'Issue Customer', 'company', ?)",
        (cust_id, company_id),
    )
    conn.commit()
    return cust_id


def _create_issue(conn, company_id, status="open", priority="medium", customer_id=None):
    """Insert an issue for testing."""
    if not customer_id:
        customer_id = _create_customer_for_issue(conn, company_id)
    issue_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO issue (id, customer_id, subject, status, priority,
           created_at)
           VALUES (?, ?, 'Test Issue', ?, ?, datetime('now'))""",
        (issue_id, customer_id, status, priority),
    )
    conn.commit()
    return issue_id


class TestProjectProfitability:
    def test_basic_profitability(self, fresh_db):
        """OPS-01: Project profitability shows correct margins."""
        cid = create_test_company(fresh_db)
        _create_project(fresh_db, cid, "Project Alpha",
                        est_cost="50000", act_cost="45000",
                        total_billed="75000")
        _create_project(fresh_db, cid, "Project Beta",
                        est_cost="30000", act_cost="35000",
                        total_billed="38000")

        result = _call_action(action_project_profitability, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["project_count"] == 2
        alpha = [p for p in result["projects"] if p["project_name"] == "Project Alpha"][0]
        # profit = total_billed - actual_cost = 75000 - 45000 = 30000
        assert Decimal(alpha["profit"]) == Decimal("30000.00")

    def test_no_projects(self, fresh_db):
        """OPS-02: Returns empty list when no projects exist."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_project_profitability, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["project_count"] == 0


class TestQualityDashboard:
    def test_basic_quality(self, fresh_db):
        """OPS-03: Quality dashboard shows pass rate correctly."""
        cid = create_test_company(fresh_db)
        item_id = _create_item_for_qi(fresh_db)
        _create_inspection(fresh_db, cid, "2026-01-10", "accepted", item_id)
        _create_inspection(fresh_db, cid, "2026-01-11", "accepted", item_id)
        _create_inspection(fresh_db, cid, "2026-01-12", "rejected", item_id)

        result = _call_action(action_quality_dashboard, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["inspections"]["total"] == 3
        assert result["inspections"]["passed"] == 2
        assert result["inspections"]["failed"] == 1
        assert result["inspections"]["pass_rate"] == "66.7%"


class TestSupportMetrics:
    def test_basic_support(self, fresh_db):
        """OPS-04: Support metrics shows correct resolution rate."""
        cid = create_test_company(fresh_db)
        cust = _create_customer_for_issue(fresh_db, cid)
        _create_issue(fresh_db, cid, "open", "high", cust)
        _create_issue(fresh_db, cid, "open", "medium", cust)
        _create_issue(fresh_db, cid, "resolved", "low", cust)
        _create_issue(fresh_db, cid, "closed", "medium", cust)

        result = _call_action(action_support_metrics, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["total_issues"] == 4
        assert result["open"] == 2
        assert result["resolved"] == 1
        assert result["closed"] == 1
        # Resolution rate = (1+1)/4 = 50%
        assert result["resolution_rate"] == "50.0%"
