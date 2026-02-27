"""AI Engine business scenario integration tests.

Tests the full AI analytics pipeline: anomaly detection, cash flow forecasting,
relationship scoring, correlation discovery, business rules, scenario analysis,
and conversation context management.

Requires GL data created via journal entries to exercise the AI engine's
heuristic detection and analysis capabilities.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
    create_test_customer,
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Shared setup helper
# ---------------------------------------------------------------------------

def _setup_ai_environment(conn):
    """Create company, FY, naming series, accounts, cost center, and customer
    needed for AI engine tests. Returns dict with all IDs."""
    cid = create_test_company(conn, name="AI Test Corp", abbr="AIT")
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    # Accounts
    revenue = create_test_account(conn, cid, "Sales Revenue", "income",
                                  account_type="revenue", account_number="4000")
    expense = create_test_account(conn, cid, "Operating Expense", "expense",
                                  account_type="expense", account_number="5000")
    bank = create_test_account(conn, cid, "Bank Account", "asset",
                               account_type="bank", account_number="1010")
    receivable = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                     account_type="receivable", account_number="1200")
    payable = create_test_account(conn, cid, "Accounts Payable", "liability",
                                  account_type="payable", account_number="2000")

    # Cost center
    cc = create_test_cost_center(conn, cid, "Main")

    # Customer
    customer_id = create_test_customer(conn, cid, "Acme Client")

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "revenue_id": revenue,
        "expense_id": expense,
        "bank_id": bank,
        "receivable_id": receivable,
        "payable_id": payable,
        "cost_center_id": cc,
        "customer_id": customer_id,
    }


def _create_and_submit_je(conn, company_id, posting_date, lines_data):
    """Helper to create and submit a journal entry. Returns journal_entry_id."""
    lines_json = json.dumps(lines_data)
    result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=company_id, posting_date=posting_date,
                          lines=lines_json)
    assert result["status"] == "ok", f"add-journal-entry failed: {result}"
    je_id = result["journal_entry_id"]

    result = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                          journal_entry_id=je_id, company_id=company_id)
    assert result["status"] == "ok", f"submit-journal-entry failed: {result}"
    return je_id


def _post_round_number_entries(conn, env):
    """Post several GL entries with round numbers (multiples of 1000) to
    trigger the round_number anomaly heuristic."""
    dates_and_amounts = [
        ("2026-01-15", "5000.00"),
        ("2026-02-15", "10000.00"),
        ("2026-03-15", "3000.00"),
    ]
    je_ids = []
    for date, amount in dates_and_amounts:
        lines = [
            {"account_id": env["bank_id"], "debit": amount, "credit": "0"},
            {"account_id": env["revenue_id"], "debit": "0", "credit": amount,
             "cost_center_id": env["cost_center_id"]},
        ]
        je_id = _create_and_submit_je(conn, env["company_id"], date, lines)
        je_ids.append(je_id)
    return je_ids


def _post_duplicate_entries(conn, env):
    """Post two identical GL entries within 7 days to trigger the
    duplicate_possible anomaly heuristic."""
    lines = [
        {"account_id": env["expense_id"], "debit": "2500.00", "credit": "0",
         "cost_center_id": env["cost_center_id"]},
        {"account_id": env["bank_id"], "debit": "0", "credit": "2500.00"},
    ]
    je1 = _create_and_submit_je(conn, env["company_id"], "2026-04-01", lines)
    je2 = _create_and_submit_je(conn, env["company_id"], "2026-04-03", lines)
    return je1, je2


# ============================================================================
# Test Class
# ============================================================================

class TestAIEngineScenario:
    """AI Engine business scenario: anomaly detection, forecasting,
    relationship scoring, correlation, and business rules."""

    # ------------------------------------------------------------------
    # 1. Full AI analysis pipeline
    # ------------------------------------------------------------------

    def test_full_ai_analysis(self, fresh_db):
        """End-to-end: create GL data, detect anomalies, forecast cash flow,
        score customer relationship, and verify all results."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Create GL data with round numbers
        _post_round_number_entries(conn, env)

        # Step 1: Detect anomalies
        result = _call_action("erpclaw-ai-engine", "detect-anomalies", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["anomalies_detected"] > 0
        anomaly_ids = result["anomaly_ids"]

        # Step 2: Forecast cash flow
        result = _call_action("erpclaw-ai-engine", "forecast-cash-flow", conn,
                              company_id=cid, horizon_days="30")
        assert result["status"] == "ok"
        assert "scenarios" in result
        assert "pessimistic" in result["scenarios"]
        assert "expected" in result["scenarios"]
        assert "optimistic" in result["scenarios"]

        # Step 3: Score customer relationship
        result = _call_action("erpclaw-ai-engine", "score-relationship", conn,
                              company_id=cid, party_type="customer",
                              party_id=env["customer_id"])
        assert result["status"] == "ok"
        assert "relationship_score" in result
        score = result["relationship_score"]
        assert score["party_type"] == "customer"
        assert score["party_id"] == env["customer_id"]

        # Step 4: Verify anomalies persisted in DB
        anomaly_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM anomaly WHERE json_extract(evidence, '$.company_id') = ?",
            (cid,),
        ).fetchone()["cnt"]
        assert anomaly_count == len(anomaly_ids)

        # Step 5: Verify forecasts persisted
        forecast_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM cash_flow_forecast WHERE json_extract(assumptions, '$.company_id') = ?",
            (cid,),
        ).fetchone()["cnt"]
        assert forecast_count == 3  # pessimistic, expected, optimistic

    # ------------------------------------------------------------------
    # 2. Anomaly detection with suspicious patterns
    # ------------------------------------------------------------------

    def test_anomaly_detection(self, fresh_db):
        """Detect round-number and duplicate anomalies from GL entries."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Post round-number entries (5000, 10000, 3000)
        _post_round_number_entries(conn, env)

        # Post duplicate entries
        _post_duplicate_entries(conn, env)

        # Run detection
        result = _call_action("erpclaw-ai-engine", "detect-anomalies", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["anomalies_detected"] > 0

        # Should have detected round_number anomalies
        # (each round GL entry >= 1000 and divisible by 1000 triggers it)
        by_type = result["by_type"]
        assert "round_number" in by_type
        assert by_type["round_number"] >= 3  # at least 3 round debit entries

        # Should have detected duplicate_possible anomalies
        assert "duplicate_possible" in by_type
        assert by_type["duplicate_possible"] >= 1

        # Verify via list-anomalies
        list_result = _call_action("erpclaw-ai-engine", "list-anomalies", conn,
                                   company_id=cid, limit=50, offset=0)
        assert list_result["status"] == "ok"
        assert list_result["total_count"] > 0
        assert len(list_result["anomalies"]) == list_result["total_count"]

    # ------------------------------------------------------------------
    # 3. Anomaly lifecycle: detect -> acknowledge -> dismiss
    # ------------------------------------------------------------------

    def test_anomaly_lifecycle(self, fresh_db):
        """Full anomaly lifecycle: detect, acknowledge, then dismiss."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Create round-number entries to generate anomalies
        _post_round_number_entries(conn, env)

        # Detect
        result = _call_action("erpclaw-ai-engine", "detect-anomalies", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["anomalies_detected"] > 0
        anomaly_id = result["anomaly_ids"][0]

        # Verify initial status is "new"
        row = conn.execute(
            "SELECT status FROM anomaly WHERE id = ?", (anomaly_id,)
        ).fetchone()
        assert row["status"] == "new"

        # Acknowledge
        result = _call_action("erpclaw-ai-engine", "acknowledge-anomaly", conn,
                              anomaly_id=anomaly_id, company_id=cid)
        assert result["status"] == "ok"
        assert result["anomaly"]["status"] == "acknowledged"

        # Verify DB state
        row = conn.execute(
            "SELECT status FROM anomaly WHERE id = ?", (anomaly_id,)
        ).fetchone()
        assert row["status"] == "acknowledged"

        # Dismiss with reason
        result = _call_action("erpclaw-ai-engine", "dismiss-anomaly", conn,
                              anomaly_id=anomaly_id,
                              reason="Known recurring transfer",
                              company_id=cid)
        assert result["status"] == "ok"
        assert result["anomaly"]["status"] == "dismissed"

        # Verify final DB state
        row = conn.execute(
            "SELECT status, resolution_notes FROM anomaly WHERE id = ?",
            (anomaly_id,),
        ).fetchone()
        assert row["status"] == "dismissed"
        assert row["resolution_notes"] == "Known recurring transfer"

    # ------------------------------------------------------------------
    # 4. Cash flow forecast
    # ------------------------------------------------------------------

    def test_cash_flow_forecast(self, fresh_db):
        """Generate 30-day cash flow forecast from GL data."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Post cash inflows to bank account via JEs
        for i, (date, amount) in enumerate([
            ("2026-01-10", "15000.00"),
            ("2026-02-10", "20000.00"),
            ("2026-03-10", "12000.00"),
        ]):
            lines = [
                {"account_id": env["bank_id"], "debit": amount, "credit": "0"},
                {"account_id": env["revenue_id"], "debit": "0", "credit": amount,
                 "cost_center_id": env["cost_center_id"]},
            ]
            _create_and_submit_je(conn, cid, date, lines)

        # Post some outflows
        lines = [
            {"account_id": env["expense_id"], "debit": "8000.00", "credit": "0",
             "cost_center_id": env["cost_center_id"]},
            {"account_id": env["bank_id"], "debit": "0", "credit": "8000.00"},
        ]
        _create_and_submit_je(conn, cid, "2026-03-15", lines)

        # Generate forecast
        result = _call_action("erpclaw-ai-engine", "forecast-cash-flow", conn,
                              company_id=cid, horizon_days="30")
        assert result["status"] == "ok"
        assert result["horizon_days"] == 30

        # Starting balance should be net of bank GL entries
        # Inflows: 15000 + 20000 + 12000 = 47000; Outflows: 8000
        # Net bank balance = 47000 - 8000 = 39000
        starting = Decimal(result["starting_balance"])
        assert starting == Decimal("39000.00"), f"Expected 39000.00, got {starting}"

        # Three scenarios should be generated
        scenarios = result["scenarios"]
        assert "pessimistic" in scenarios
        assert "expected" in scenarios
        assert "optimistic" in scenarios

        # Confidence interval should be set
        ci = result["confidence_interval"]
        assert Decimal(ci["low"]) <= Decimal(ci["mid"]) <= Decimal(ci["high"])

        # Verify forecast IDs stored in DB
        assert len(result["forecast_ids"]) == 3
        for fid in result["forecast_ids"]:
            row = conn.execute(
                "SELECT * FROM cash_flow_forecast WHERE id = ?", (fid,)
            ).fetchone()
            assert row is not None

        # Retrieve forecasts via get-forecast
        get_result = _call_action("erpclaw-ai-engine", "get-forecast", conn,
                                  company_id=cid)
        assert get_result["status"] == "ok"
        assert get_result["count"] == 3

    # ------------------------------------------------------------------
    # 5. Correlation discovery
    # ------------------------------------------------------------------

    def test_correlation_discovery(self, fresh_db):
        """Discover correlations from GL patterns (runs on empty invoice
        data -- verifies graceful handling and correlation table)."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Post some GL data to ensure the company has activity
        _post_round_number_entries(conn, env)

        # Run correlation discovery (no invoices, so sales/purchase correlation
        # should not fire, but the action should succeed gracefully)
        result = _call_action("erpclaw-ai-engine", "discover-correlations", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert isinstance(result["correlations_discovered"], int)
        assert isinstance(result["correlation_ids"], list)

        # List correlations
        list_result = _call_action("erpclaw-ai-engine", "list-correlations", conn,
                                   company_id=cid, limit=20, offset=0)
        assert list_result["status"] == "ok"
        assert list_result["total_count"] == result["correlations_discovered"]

    # ------------------------------------------------------------------
    # 6. Scenario analysis
    # ------------------------------------------------------------------

    def test_scenario_analysis(self, fresh_db):
        """Create what-if scenarios with different assumptions."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Create a price_change scenario
        assumptions_json = json.dumps({
            "price_increase_pct": "15",
            "affected_items": "all",
            "expected_volume_impact": "-5",
        })
        result = _call_action("erpclaw-ai-engine", "create-scenario", conn,
                              name="What if we raise prices 15%?",
                              company_id=cid,
                              scenario_type="price_change",
                              assumptions=assumptions_json)
        assert result["status"] == "ok"
        scenario = result["scenario"]
        assert scenario["question"] == "What if we raise prices 15%?"
        assert scenario["scenario_type"] == "price_change"
        scenario_id_1 = scenario["id"]

        # Verify assumptions include the company_id
        stored_assumptions = json.loads(scenario["assumptions"])
        assert stored_assumptions["company_id"] == cid
        assert stored_assumptions["price_increase_pct"] == "15"

        # Create a second scenario of different type
        assumptions_json_2 = json.dumps({
            "lost_supplier": "primary_vendor",
            "replacement_lead_time_days": "45",
        })
        result2 = _call_action("erpclaw-ai-engine", "create-scenario", conn,
                               name="What if we lose our primary vendor?",
                               company_id=cid,
                               scenario_type="supplier_loss",
                               assumptions=assumptions_json_2)
        assert result2["status"] == "ok"
        scenario_id_2 = result2["scenario"]["id"]

        # List scenarios for this company
        list_result = _call_action("erpclaw-ai-engine", "list-scenarios", conn,
                                   company_id=cid, limit=20, offset=0)
        assert list_result["status"] == "ok"
        assert list_result["total_count"] == 2
        ids_returned = {s["id"] for s in list_result["scenarios"]}
        assert scenario_id_1 in ids_returned
        assert scenario_id_2 in ids_returned

    # ------------------------------------------------------------------
    # 7. Business rules
    # ------------------------------------------------------------------

    def test_business_rules(self, fresh_db):
        """Create a business rule and evaluate it against action data."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Create a business rule (severity maps to action)
        result = _call_action("erpclaw-ai-engine", "add-business-rule", conn,
                              name="High-value payment approval",
                              company_id=cid,
                              rule_text="All payments above $10,000 require manager approval",
                              severity="warn")
        assert result["status"] == "ok"
        rule = result["business_rule"]
        rule_id = rule["id"]
        assert rule["rule_text"] == "All payments above $10,000 require manager approval"
        assert rule["action"] == "warn"
        assert rule["active"] == 1

        # Verify the rule is in the DB
        db_rule = conn.execute(
            "SELECT * FROM business_rule WHERE id = ?", (rule_id,)
        ).fetchone()
        assert db_rule is not None
        assert db_rule["times_triggered"] == 0

        # List business rules
        list_result = _call_action("erpclaw-ai-engine", "list-business-rules", conn,
                                   company_id=cid, limit=20, offset=0)
        assert list_result["status"] == "ok"
        assert list_result["total_count"] == 1

        # Evaluate rules (rule has no parsed conditions, so it matches everything)
        action_data = json.dumps({"amount": "15000", "party": "vendor_abc"})
        eval_result = _call_action("erpclaw-ai-engine", "evaluate-business-rules", conn,
                                   company_id=cid,
                                   action_type="payment",
                                   action_data=action_data)
        assert eval_result["status"] == "ok"
        assert eval_result["triggered"] is True
        assert len(eval_result["rules"]) >= 1
        assert eval_result["rules"][0]["rule_id"] == rule_id
        assert eval_result["recommended_action"] == "warn"

        # Verify trigger count incremented
        db_rule = conn.execute(
            "SELECT times_triggered FROM business_rule WHERE id = ?", (rule_id,)
        ).fetchone()
        assert db_rule["times_triggered"] == 1

    # ------------------------------------------------------------------
    # 8. Relationship scoring
    # ------------------------------------------------------------------

    def test_relationship_scoring(self, fresh_db):
        """Score customer relationship (no invoices = default score)."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]
        customer_id = env["customer_id"]

        # Score the customer relationship (no invoice/payment history)
        result = _call_action("erpclaw-ai-engine", "score-relationship", conn,
                              company_id=cid, party_type="customer",
                              party_id=customer_id)
        assert result["status"] == "ok"
        score = result["relationship_score"]

        # With no transaction history, should get default score of 50
        assert score["party_type"] == "customer"
        assert score["party_id"] == customer_id
        assert score["overall_score"] == "50"
        assert score["payment_score"] == "50"
        assert score["volume_trend"] == "stable"
        # round_currency on Decimal(0) may produce "0" or "0.00"
        assert Decimal(score["lifetime_value"]) == Decimal("0")
        assert "No transaction history" in score["ai_summary"]

        # Verify persisted in DB
        db_score = conn.execute(
            "SELECT * FROM relationship_score WHERE id = ?", (score["id"],)
        ).fetchone()
        assert db_score is not None
        assert db_score["party_type"] == "customer"

        # List relationship scores
        list_result = _call_action("erpclaw-ai-engine", "list-relationship-scores", conn,
                                   company_id=cid, limit=20, offset=0)
        assert list_result["status"] == "ok"
        assert list_result["total_count"] == 1
        assert list_result["relationship_scores"][0]["id"] == score["id"]

    # ------------------------------------------------------------------
    # 9. Conversation context save and retrieve
    # ------------------------------------------------------------------

    def test_conversation_context(self, fresh_db):
        """Save and retrieve conversation context."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Save a conversation context
        context_data = json.dumps({
            "context_type": "active_workflow",
            "user_id": "user-001",
            "summary": "Reviewing Q1 financial reports",
            "related_entities": [
                {"type": "report", "id": "tb-2026-q1"},
                {"type": "company", "id": cid},
            ],
            "state": {
                "current_step": "review_expenses",
                "completed_steps": ["review_revenue"],
            },
            "priority": 5,
        })

        result = _call_action("erpclaw-ai-engine", "save-conversation-context", conn,
                              context_data=context_data, company_id=cid)
        assert result["status"] == "ok"
        ctx = result["context"]
        ctx_id = ctx["id"]
        assert ctx["context_type"] == "active_workflow"
        assert ctx["summary"] == "Reviewing Q1 financial reports"
        assert ctx["priority"] == 5

        # Retrieve the context by ID
        get_result = _call_action("erpclaw-ai-engine", "get-conversation-context", conn,
                                  context_id=ctx_id, company_id=cid)
        assert get_result["status"] == "ok"
        retrieved = get_result["context"]
        assert retrieved["id"] == ctx_id
        assert retrieved["context_type"] == "active_workflow"
        assert retrieved["summary"] == "Reviewing Q1 financial reports"

        # State and related_entities should be JSON strings
        state = json.loads(retrieved["state"])
        assert state["current_step"] == "review_expenses"

        related = json.loads(retrieved["related_entities"])
        assert len(related) == 2
        assert related[0]["type"] == "report"

        # Save a second context
        context_data_2 = json.dumps({
            "context_type": "pending_decision",
            "user_id": "user-001",
            "summary": "Awaiting budget approval for Q2",
            "state": {"pending": True},
        })
        result2 = _call_action("erpclaw-ai-engine", "save-conversation-context", conn,
                               context_data=context_data_2, company_id=cid)
        assert result2["status"] == "ok"
        ctx2_id = result2["context"]["id"]
        assert ctx2_id != ctx_id  # two distinct contexts created

        # Retrieve second context by explicit ID
        get_result2 = _call_action("erpclaw-ai-engine", "get-conversation-context", conn,
                                   context_id=ctx2_id, company_id=cid)
        assert get_result2["status"] == "ok"
        assert get_result2["context"]["id"] == ctx2_id
        assert get_result2["context"]["summary"] == "Awaiting budget approval for Q2"

        # Verify both contexts exist in DB
        ctx_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversation_context"
        ).fetchone()["cnt"]
        assert ctx_count == 2

    # ------------------------------------------------------------------
    # 10. AI features on empty data (graceful degradation)
    # ------------------------------------------------------------------

    def test_ai_with_empty_data(self, fresh_db):
        """All AI features should handle empty DB gracefully."""
        conn = fresh_db
        env = _setup_ai_environment(conn)
        cid = env["company_id"]

        # Status endpoint on truly empty data (before any AI actions)
        result = _call_action("erpclaw-ai-engine", "status", conn,
                              company_id=cid)
        assert result["status"] == "ok"
        assert result["anomalies"]["new_total"] == 0
        assert result["forecasts"] == 0
        assert result["correlations"] == 0
        assert result["scenarios"] == 0
        assert result["relationship_scores"] == 0

        # Anomaly detection on empty GL -- should return 0 anomalies
        result = _call_action("erpclaw-ai-engine", "detect-anomalies", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["anomalies_detected"] == 0
        assert result["anomaly_ids"] == []

        # Cash flow forecast on empty GL -- should succeed with 0 balance
        result = _call_action("erpclaw-ai-engine", "forecast-cash-flow", conn,
                              company_id=cid, horizon_days="30")
        assert result["status"] == "ok"
        assert Decimal(result["starting_balance"]) == Decimal("0.00")
        assert result["total_ar"] == "0.00"
        assert result["total_ap"] == "0.00"
        # All three scenarios should project 0 balance
        for scenario_name in ("pessimistic", "expected", "optimistic"):
            assert Decimal(result["scenarios"][scenario_name]) == Decimal("0.00")

        # Correlation discovery on empty data
        result = _call_action("erpclaw-ai-engine", "discover-correlations", conn,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["correlations_discovered"] == 0

        # List anomalies on empty data
        result = _call_action("erpclaw-ai-engine", "list-anomalies", conn,
                              company_id=cid, limit=20, offset=0)
        assert result["status"] == "ok"
        assert result["total_count"] == 0
        assert result["anomalies"] == []

        # List business rules on empty data
        result = _call_action("erpclaw-ai-engine", "list-business-rules", conn,
                              company_id=cid, limit=20, offset=0)
        assert result["status"] == "ok"
        assert result["total_count"] == 0

        # List correlations on empty data
        result = _call_action("erpclaw-ai-engine", "list-correlations", conn,
                              company_id=cid, limit=20, offset=0)
        assert result["status"] == "ok"
        assert result["total_count"] == 0

        # List relationship scores on empty data
        result = _call_action("erpclaw-ai-engine", "list-relationship-scores", conn,
                              company_id=cid, limit=20, offset=0)
        assert result["status"] == "ok"
        assert result["total_count"] == 0

        # List scenarios on empty data
        result = _call_action("erpclaw-ai-engine", "list-scenarios", conn,
                              company_id=cid, limit=20, offset=0)
        assert result["status"] == "ok"
        assert result["total_count"] == 0
