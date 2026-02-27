"""Tests for anomaly detection and management."""
import pytest
from helpers import (
    _call_action, setup_ai_environment, create_test_gl_entry,
    create_test_sales_invoice, create_test_budget,
    create_test_purchase_invoice, create_test_account,
    create_test_cost_center, create_test_fiscal_year,
)
from db_query import detect_anomalies, list_anomalies, acknowledge_anomaly, dismiss_anomaly


# ---------------------------------------------------------------------------
# 1. Detect on empty company -- no anomalies
# ---------------------------------------------------------------------------

def test_detect_no_data(fresh_db):
    """detect_anomalies on a company with no transactions returns 0 anomalies."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    assert result["status"] == "ok"
    assert result["anomalies_detected"] == 0
    assert result["anomaly_ids"] == []
    assert result["by_type"] == {}
    assert result["by_severity"] == {}


# ---------------------------------------------------------------------------
# 2. Duplicate possible -- same account + same amount within 7 days
# ---------------------------------------------------------------------------

def test_detect_duplicate_possible(fresh_db):
    """Two GL entries with same account and same debit amount within 7 days
    should trigger a duplicate_possible anomaly."""
    env = setup_ai_environment(fresh_db)

    # Create two GL entries on the same account with same debit, 3 days apart
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="2500", credit="0",
        posting_date="2026-01-10",
    )
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="2500", credit="0",
        posting_date="2026-01-12",
    )

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    assert result["status"] == "ok"
    assert result["anomalies_detected"] >= 1
    assert "duplicate_possible" in result["by_type"]
    assert result["by_type"]["duplicate_possible"] >= 1


# ---------------------------------------------------------------------------
# 3. Round number -- debit >= 1000 divisible by 1000
# ---------------------------------------------------------------------------

def test_detect_round_number(fresh_db):
    """A GL entry with debit='5000' (>= 1000 and divisible by 1000)
    should trigger a round_number anomaly."""
    env = setup_ai_environment(fresh_db)

    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="5000", credit="0",
        posting_date="2026-01-15",
    )

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    assert result["status"] == "ok"
    assert result["anomalies_detected"] >= 1
    assert "round_number" in result["by_type"]
    assert result["by_type"]["round_number"] >= 1
    assert result["by_severity"].get("info", 0) >= 1


# ---------------------------------------------------------------------------
# 4. Budget overrun -- actual spend exceeds budget
# ---------------------------------------------------------------------------

def test_detect_budget_overrun(fresh_db):
    """GL entries totalling debit='1500' against a budget of '1000'
    should trigger a budget_overrun anomaly."""
    env = setup_ai_environment(fresh_db)

    # Create budget for expense account with amount 1000
    create_test_budget(
        fresh_db, env["company_id"], env["fiscal_year_id"],
        env["expense_account_id"], budget_amount="1000",
        cost_center_id=None,
    )

    # Create GL entries that exceed the budget (total debit = 1500)
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="800", credit="0",
        posting_date="2026-01-10",
    )
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="700", credit="0",
        posting_date="2026-01-20",
    )

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    assert result["status"] == "ok"
    assert "budget_overrun" in result["by_type"]
    assert result["by_type"]["budget_overrun"] >= 1


# ---------------------------------------------------------------------------
# 5. Late pattern -- overdue sales invoice
# ---------------------------------------------------------------------------

def test_detect_late_pattern(fresh_db):
    """A submitted sales invoice with due_date in the past and outstanding > 0
    should trigger a late_pattern anomaly."""
    env = setup_ai_environment(fresh_db)

    # Invoice due 2025-12-01, still outstanding, detected with to_date=2026-01-15
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="1000", outstanding="1000",
        posting_date="2025-11-15", due_date="2025-12-01",
        status="submitted",
    )

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2025-01-01",
        to_date="2026-01-15",
    )

    assert result["status"] == "ok"
    assert "late_pattern" in result["by_type"]
    assert result["by_type"]["late_pattern"] >= 1
    # 45 days overdue (2025-12-01 to 2026-01-15) => critical
    assert result["by_severity"].get("critical", 0) >= 1


# ---------------------------------------------------------------------------
# 6. Volume change -- > 30% change between periods
# ---------------------------------------------------------------------------

def test_detect_volume_change(fresh_db):
    """Sales invoices totalling 5000 in the current period vs 1000 in the
    prior period should trigger a volume_change anomaly (>30% change)."""
    env = setup_ai_environment(fresh_db)

    # Prior period: 2025-12-17 to 2025-12-31 (15 days), total = 1000
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="1000", outstanding="0",
        posting_date="2025-12-20", due_date="2026-01-20",
        status="submitted",
    )

    # Current period: 2026-01-01 to 2026-01-15 (15 days), total = 5000
    for _ in range(5):
        create_test_sales_invoice(
            fresh_db, env["company_id"], env["customer_id"],
            grand_total="1000", outstanding="0",
            posting_date="2026-01-05", due_date="2026-02-05",
            status="submitted",
        )

    result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-15",
    )

    assert result["status"] == "ok"
    assert "volume_change" in result["by_type"]
    assert result["by_type"]["volume_change"] >= 1


# ---------------------------------------------------------------------------
# 7. List anomalies -- filter by severity
# ---------------------------------------------------------------------------

def test_list_anomalies_filter_severity(fresh_db):
    """After detecting anomalies, listing with severity='warning' should
    return only warning-level anomalies."""
    env = setup_ai_environment(fresh_db)

    # Create data that produces both 'info' (round_number) and 'warning' (duplicate)
    # Round number => info
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="3000", credit="0",
        posting_date="2026-01-10",
    )
    # Duplicate pair => warning
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="3000", credit="0",
        posting_date="2026-01-12",
    )

    # Detect
    detect_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    assert detect_result["anomalies_detected"] >= 1

    # List all anomalies to check what was created
    all_result = _call_action(
        list_anomalies, fresh_db,
    )
    assert all_result["total_count"] >= 2, f"Expected >= 2 anomalies, got {all_result}"

    # We should have both info (round_number) and warning (duplicate_possible)
    severities = {a["severity"] for a in all_result["anomalies"]}
    assert len(severities) >= 2, f"Expected multiple severities, got {severities}"

    # Filter by one severity -- use "info" since round_number is guaranteed
    result = _call_action(
        list_anomalies, fresh_db,
        severity="info",
    )

    assert result["status"] == "ok"
    assert result["total_count"] >= 1
    for anomaly in result["anomalies"]:
        assert anomaly["severity"] == "info"

    # Filter by warning
    result_w = _call_action(
        list_anomalies, fresh_db,
        severity="warning",
    )
    assert result_w["status"] == "ok"
    assert result_w["total_count"] >= 1
    for anomaly in result_w["anomalies"]:
        assert anomaly["severity"] == "warning"


# ---------------------------------------------------------------------------
# 8. List anomalies -- filter by status
# ---------------------------------------------------------------------------

def test_list_anomalies_filter_status(fresh_db):
    """After detecting anomalies, listing with status='new' returns all.
    After acknowledging one, listing status='acknowledged' returns only that one."""
    env = setup_ai_environment(fresh_db)

    # Create round_number anomaly (info)
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="2000", credit="0",
        posting_date="2026-01-15",
    )

    # Detect
    detect_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    assert detect_result["anomalies_detected"] >= 1
    anomaly_id = detect_result["anomaly_ids"][0]

    # All should be 'new'
    new_list = _call_action(
        list_anomalies, fresh_db,
        company_id=env["company_id"],
        status="new",
    )
    assert new_list["total_count"] >= 1

    # Acknowledge one
    _call_action(
        acknowledge_anomaly, fresh_db,
        anomaly_id=anomaly_id,
    )

    # List acknowledged
    ack_list = _call_action(
        list_anomalies, fresh_db,
        company_id=env["company_id"],
        status="acknowledged",
    )
    assert ack_list["total_count"] == 1
    assert ack_list["anomalies"][0]["id"] == anomaly_id
    assert ack_list["anomalies"][0]["status"] == "acknowledged"


# ---------------------------------------------------------------------------
# 9. Acknowledge anomaly
# ---------------------------------------------------------------------------

def test_acknowledge_anomaly(fresh_db):
    """Acknowledging a 'new' anomaly should set its status to 'acknowledged'."""
    env = setup_ai_environment(fresh_db)

    # Create data for anomaly detection
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="4000", credit="0",
        posting_date="2026-01-15",
    )

    # Detect
    detect_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    assert detect_result["anomalies_detected"] >= 1
    anomaly_id = detect_result["anomaly_ids"][0]

    # Acknowledge
    ack_result = _call_action(
        acknowledge_anomaly, fresh_db,
        anomaly_id=anomaly_id,
    )

    assert ack_result["status"] == "ok"
    assert ack_result["anomaly"]["id"] == anomaly_id
    assert ack_result["anomaly"]["status"] == "acknowledged"


# ---------------------------------------------------------------------------
# 10. Dismiss anomaly with reason
# ---------------------------------------------------------------------------

def test_dismiss_anomaly_with_reason(fresh_db):
    """Dismissing an anomaly with a reason should set status='dismissed'
    and resolution_notes to the given reason."""
    env = setup_ai_environment(fresh_db)

    # Create data for anomaly detection
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="6000", credit="0",
        posting_date="2026-01-15",
    )

    # Detect
    detect_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    assert detect_result["anomalies_detected"] >= 1
    anomaly_id = detect_result["anomaly_ids"][0]

    # Dismiss with reason
    dismiss_result = _call_action(
        dismiss_anomaly, fresh_db,
        anomaly_id=anomaly_id,
        reason="false positive",
    )

    assert dismiss_result["status"] == "ok"
    assert dismiss_result["anomaly"]["id"] == anomaly_id
    assert dismiss_result["anomaly"]["status"] == "dismissed"
    assert dismiss_result["anomaly"]["resolution_notes"] == "false positive"


# ---------------------------------------------------------------------------
# 11. Idempotent detection -- second run finds 0 new anomalies
# ---------------------------------------------------------------------------

def test_idempotent_detection(fresh_db):
    """Running detect_anomalies twice on the same data should find 0 new
    anomalies on the second run, since existing ones are still new/acknowledged."""
    env = setup_ai_environment(fresh_db)

    # Create data that triggers anomalies
    create_test_gl_entry(
        fresh_db, env["expense_account_id"],
        debit="7000", credit="0",
        posting_date="2026-01-15",
    )

    # First detection run
    first_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    assert first_result["anomalies_detected"] >= 1

    # Second detection run -- same data, same date range
    second_result = _call_action(
        detect_anomalies, fresh_db,
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    assert second_result["status"] == "ok"
    assert second_result["anomalies_detected"] == 0
    assert second_result["anomaly_ids"] == []
