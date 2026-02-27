"""Tests for S35 analyze-query-performance action.

Tests: query plan analysis, index detection, full scan detection.
"""
import os
import sqlite3
import sys

import db_query
from helpers import _call_action, _run_init_db

# Add shared lib
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

_LOCAL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../../"))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_LOCAL_ROOT, "init_db.py")):
    PROJECT_ROOT = _LOCAL_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _LOCAL_ROOT


def _fresh_db(tmp_path):
    """Create and return a fresh DB connection."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, db_path


def test_analyze_returns_results(tmp_path):
    """Performance analysis runs and returns structured results."""
    conn, db_path = _fresh_db(tmp_path)
    result = _call_action(db_query.action_analyze_query_performance, conn)
    conn.close()

    assert result["status"] == "ok"
    assert result["total_queries_analyzed"] > 0
    assert "queries_using_index" in result
    assert "full_table_scans" in result
    assert "index_utilization_pct" in result
    assert "total_indexes" in result
    assert "total_tables" in result
    assert isinstance(result["query_plans"], list)


def test_index_utilization_high(tmp_path):
    """ERPClaw's 469 indexes should give high index utilization."""
    conn, db_path = _fresh_db(tmp_path)
    result = _call_action(db_query.action_analyze_query_performance, conn)
    conn.close()

    # With 469 indexes, most queries should use indexes
    assert result["index_utilization_pct"] >= 60, \
        f"Index utilization too low: {result['index_utilization_pct']}%"
    assert result["total_indexes"] > 400


def test_query_plans_have_detail(tmp_path):
    """Each query plan has meaningful detail."""
    conn, db_path = _fresh_db(tmp_path)
    result = _call_action(db_query.action_analyze_query_performance, conn)
    conn.close()

    for qp in result["query_plans"]:
        assert "query" in qp
        assert "plan" in qp
        assert "uses_index" in qp
        assert "full_scan" in qp
        assert isinstance(qp["plan"], list)
        assert len(qp["plan"]) > 0
