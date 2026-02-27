"""Billing scenario integration tests: full billing lifecycle.

Tests the billing workflow end-to-end:
  meter creation -> readings -> rate plan -> billing run -> invoice generation -> adjustments

Covers: erpclaw-billing (meters, readings, rate plans, billing periods,
        bill runs, adjustments, prepaid credits).
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
    create_test_item,
    setup_phase2_environment,
)

# ---------------------------------------------------------------------------
# Patch: billing skill uses "meter" as a naming series entity type, but it
# is not in the shared lib's ENTITY_PREFIXES.  The billing skill's own
# test helpers also apply this patch.
# ---------------------------------------------------------------------------
try:
    from erpclaw_lib.naming import ENTITY_PREFIXES
    if "meter" not in ENTITY_PREFIXES:
        ENTITY_PREFIXES["meter"] = "MTR-"
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Connection wrapper: billing skill sets conn.company_id as an attribute,
# which plain sqlite3.Connection objects do not support.  This wrapper
# delegates all calls to the underlying connection but allows arbitrary
# attribute assignment.
# ---------------------------------------------------------------------------

class _ConnWrapper:
    """Thin wrapper around sqlite3.Connection that permits arbitrary attrs."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Shared setup helper for billing tests
# ---------------------------------------------------------------------------

def _wrap(conn):
    """Wrap a plain sqlite3.Connection so billing actions can set attributes."""
    if isinstance(conn, _ConnWrapper):
        return conn
    return _ConnWrapper(conn)


def _setup_billing_env(conn):
    """Create the minimum environment needed for billing tests.

    Returns a dict with company_id, customer_id, fy_id, cost_center_id,
    receivable_id, income_id, and naming series seeded.
    """
    company_id = create_test_company(conn, name="Billing Corp", abbr="BC")
    fy_id = create_test_fiscal_year(conn, company_id)
    seed_naming_series(conn, company_id)
    cc_id = create_test_cost_center(conn, company_id)

    receivable_id = create_test_account(
        conn, company_id, "Accounts Receivable", "asset",
        account_type="receivable", account_number="1200",
    )
    income_id = create_test_account(
        conn, company_id, "Billing Revenue", "income",
        account_type="revenue", account_number="4000",
    )

    # Set company defaults
    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_income_account_id = ?
           WHERE id = ?""",
        (receivable_id, income_id, company_id),
    )
    conn.commit()

    customer_id = create_test_customer(conn, company_id, name="Acme Utilities Customer")

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cc_id,
        "receivable_id": receivable_id,
        "income_id": income_id,
        "customer_id": customer_id,
    }


def _create_tiered_rate_plan(conn, name="Standard Tiered Plan",
                              base_charge="10.00"):
    """Create a tiered rate plan with 3 tiers and return result dict."""
    tiers = json.dumps([
        {"tier_start": "0", "tier_end": "100", "rate": "0.10"},
        {"tier_start": "100", "tier_end": "500", "rate": "0.08"},
        {"tier_start": "500", "tier_end": None, "rate": "0.05"},
    ])
    result = _call_action("erpclaw-billing", "add-rate-plan", conn,
                          name=name,
                          billing_model="tiered",
                          tiers=tiers,
                          base_charge=base_charge)
    assert result["status"] == "ok", f"add-rate-plan failed: {result}"
    return result


def _create_flat_rate_plan(conn, name="Flat Rate Plan", rate="0.12",
                            base_charge="5.00"):
    """Create a flat rate plan with a single tier and return result dict."""
    tiers = json.dumps([{"tier_start": "0", "rate": rate}])
    result = _call_action("erpclaw-billing", "add-rate-plan", conn,
                          name=name,
                          billing_model="flat",
                          tiers=tiers,
                          base_charge=base_charge)
    assert result["status"] == "ok", f"add-rate-plan (flat) failed: {result}"
    return result


def _create_meter_with_plan(conn, customer_id, rate_plan_id,
                             meter_type="electricity", unit="kWh",
                             name=None):
    """Create a meter associated to a customer and rate plan. Returns result dict."""
    result = _call_action("erpclaw-billing", "add-meter", conn,
                          name=name or "SP-Main",
                          customer_id=customer_id,
                          meter_type=meter_type,
                          unit=unit,
                          install_date="2026-01-01",
                          rate_plan_id=rate_plan_id)
    assert result["status"] == "ok", f"add-meter failed: {result}"
    return result


def _add_reading(conn, meter_id, reading_date, reading_value,
                  reading_type="actual"):
    """Add a meter reading and return result dict."""
    result = _call_action("erpclaw-billing", "add-meter-reading", conn,
                          meter_id=meter_id,
                          reading_date=reading_date,
                          reading_value=reading_value,
                          reading_type=reading_type)
    assert result["status"] == "ok", f"add-meter-reading failed: {result}"
    return result


# ===========================================================================
# Test class
# ===========================================================================

class TestBillingScenario:
    """Integration tests for the billing lifecycle."""

    # -----------------------------------------------------------------------
    # 1. Full billing cycle (end-to-end)
    # -----------------------------------------------------------------------

    def test_full_billing_cycle(self, fresh_db):
        """End-to-end: meter -> readings -> rate plan -> billing run -> invoice.

        Steps:
          1. Create company, customer, accounts, naming series
          2. Create tiered rate plan
          3. Create meter linked to customer and rate plan
          4. Add two readings (consumption = 250 kWh)
          5. Run billing for the period
          6. Verify billing period is rated with correct amounts
          7. Generate invoices from rated billing periods
          8. Verify billing period status updated to invoiced
        """
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        # Rate plan: tiered, base $10
        rp = _create_tiered_rate_plan(conn)
        rp_id = rp["rate_plan"]["id"]

        # Meter
        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        # Readings: 1000 -> 1250 => consumption = 250 kWh
        _add_reading(conn, meter_id, "2026-01-15", "1000")
        _add_reading(conn, meter_id, "2026-01-31", "1250")

        # Run billing
        result = _call_action("erpclaw-billing", "run-billing", conn,
                              company_id=env["company_id"],
                              billing_date="2026-01-31",
                              from_date="2026-01-01",
                              to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["periods_created"] == 1

        bp_id = result["period_ids"][0]

        # Verify billing period in DB
        bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
        assert bp["status"] == "rated"
        assert bp["total_consumption"] == "250"

        # Expected charges for 250 kWh tiered:
        #   Tier 1: 100 * 0.10 = 10.00
        #   Tier 2: 150 * 0.08 = 12.00
        #   Usage charge = 22.00, base = 10.00, total = 32.00
        assert Decimal(bp["usage_charge"]) == Decimal("22.00")
        assert Decimal(bp["base_charge"]) == Decimal("10.00")
        assert Decimal(bp["grand_total"]) == Decimal("32.00")

        # Generate invoices
        inv_result = _call_action("erpclaw-billing", "generate-invoices", conn,
                                  billing_period_ids=json.dumps([bp_id]))
        assert inv_result["status"] == "ok"
        assert inv_result["invoiced"] == 1

        # Verify billing period status updated
        bp_after = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                                (bp_id,)).fetchone()
        assert bp_after["status"] == "invoiced"

    # -----------------------------------------------------------------------
    # 2. Meter creation
    # -----------------------------------------------------------------------

    def test_meter_creation(self, fresh_db):
        """Create a meter with customer association and verify DB state."""
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)
        rp = _create_flat_rate_plan(conn)
        rp_id = rp["rate_plan"]["id"]

        result = _call_action("erpclaw-billing", "add-meter", conn,
                              name="ServicePoint-42",
                              customer_id=env["customer_id"],
                              meter_type="electricity",
                              unit="kWh",
                              install_date="2026-02-01",
                              rate_plan_id=rp_id)
        assert result["status"] == "ok"

        meter = result["meter"]
        meter_id = meter["id"]
        assert meter["customer_id"] == env["customer_id"]
        assert meter["service_type"] == "electricity"
        assert meter["rate_plan_id"] == rp_id
        assert meter["status"] == "active"
        assert meter["install_date"] == "2026-02-01"

        # Verify meter_number was auto-generated
        assert meter["meter_number"] is not None
        assert len(meter["meter_number"]) > 0

        # Verify DB
        db_meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                                (meter_id,)).fetchone()
        assert db_meter is not None
        assert db_meter["customer_id"] == env["customer_id"]
        assert db_meter["service_type"] == "electricity"

        # Verify metadata contains UOM
        metadata = json.loads(db_meter["metadata"])
        assert metadata["uom"] == "kWh"

    # -----------------------------------------------------------------------
    # 3. Meter readings with consumption calculation
    # -----------------------------------------------------------------------

    def test_meter_readings(self, fresh_db):
        """Add sequential readings and verify automatic consumption calc."""
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)
        rp = _create_flat_rate_plan(conn)
        rp_id = rp["rate_plan"]["id"]

        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        # First reading: no previous, consumption should be None
        r1 = _add_reading(conn, meter_id, "2026-01-01", "500")
        assert r1["reading"]["reading_value"] == "500"
        assert r1["reading"]["consumption"] is None
        assert r1["reading"]["previous_reading_value"] is None

        # Second reading: consumption = 1200 - 500 = 700
        r2 = _add_reading(conn, meter_id, "2026-01-15", "1200")
        assert r2["reading"]["reading_value"] == "1200"
        assert r2["reading"]["consumption"] == "700"
        assert r2["reading"]["previous_reading_value"] == "500"

        # Third reading: consumption = 1500 - 1200 = 300
        r3 = _add_reading(conn, meter_id, "2026-01-31", "1500")
        assert r3["reading"]["consumption"] == "300"
        assert r3["reading"]["previous_reading_value"] == "1200"

        # Verify meter's last_reading_value updated
        db_meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                                (meter_id,)).fetchone()
        assert db_meter["last_reading_value"] == "1500"
        assert db_meter["last_reading_date"] == "2026-01-31"

        # Verify total readings count
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM meter_reading WHERE meter_id = ?",
            (meter_id,)).fetchone()["cnt"]
        assert count == 3

    # -----------------------------------------------------------------------
    # 4. Tiered rate plan creation
    # -----------------------------------------------------------------------

    def test_tiered_rate_plan(self, fresh_db):
        """Create a tiered rate plan with 3 tiers and verify structure."""
        conn = _wrap(fresh_db)

        tiers_json = json.dumps([
            {"tier_start": "0", "tier_end": "100", "rate": "0.10"},
            {"tier_start": "100", "tier_end": "500", "rate": "0.08"},
            {"tier_start": "500", "tier_end": None, "rate": "0.05"},
        ])

        result = _call_action("erpclaw-billing", "add-rate-plan", conn,
                              name="3-Tier Electricity Plan",
                              billing_model="tiered",
                              tiers=tiers_json,
                              base_charge="15.00")
        assert result["status"] == "ok"

        plan = result["rate_plan"]
        assert plan["name"] == "3-Tier Electricity Plan"
        assert plan["plan_type"] == "tiered"
        assert plan["base_charge"] == "15.00"

        # Verify 3 tiers were created
        assert len(plan["tiers"]) == 3

        tiers = sorted(plan["tiers"], key=lambda t: int(t["sort_order"]))
        assert tiers[0]["tier_start"] == "0"
        assert tiers[0]["tier_end"] == "100"
        assert tiers[0]["rate"] == "0.10"

        assert tiers[1]["tier_start"] == "100"
        assert tiers[1]["tier_end"] == "500"
        assert tiers[1]["rate"] == "0.08"

        assert tiers[2]["tier_start"] == "500"
        assert tiers[2]["tier_end"] is None
        assert tiers[2]["rate"] == "0.05"

        # Verify DB
        db_tiers = conn.execute(
            "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
            (plan["id"],)).fetchall()
        assert len(db_tiers) == 3

    # -----------------------------------------------------------------------
    # 5. Flat rate plan creation
    # -----------------------------------------------------------------------

    def test_flat_rate_plan(self, fresh_db):
        """Create a flat rate plan and verify single tier."""
        conn = _wrap(fresh_db)

        tiers_json = json.dumps([{"tier_start": "0", "rate": "0.15"}])

        result = _call_action("erpclaw-billing", "add-rate-plan", conn,
                              name="Simple Flat Plan",
                              billing_model="flat",
                              tiers=tiers_json,
                              base_charge="20.00")
        assert result["status"] == "ok"

        plan = result["rate_plan"]
        assert plan["plan_type"] == "flat"
        assert plan["base_charge"] == "20.00"
        assert len(plan["tiers"]) == 1
        assert plan["tiers"][0]["rate"] == "0.15"

        # Verify in DB
        db_plan = conn.execute("SELECT * FROM rate_plan WHERE id = ?",
                               (plan["id"],)).fetchone()
        assert db_plan["plan_type"] == "flat"
        assert db_plan["name"] == "Simple Flat Plan"

    # -----------------------------------------------------------------------
    # 6. Rate consumption calculation
    # -----------------------------------------------------------------------

    def test_rate_consumption(self, fresh_db):
        """Rate a consumption value against a tiered plan, verify tier calc.

        Tiered plan:
          0-100: $0.10/unit
          100-500: $0.08/unit
          500+: $0.05/unit
          Base charge: $10.00

        Consumption = 600 units:
          Tier 1: 100 * 0.10 = $10.00
          Tier 2: 400 * 0.08 = $32.00
          Tier 3: 100 * 0.05 = $5.00
          Usage = $47.00, base = $10.00, total = $57.00
        """
        conn = _wrap(fresh_db)

        rp = _create_tiered_rate_plan(conn, base_charge="10.00")
        rp_id = rp["rate_plan"]["id"]

        result = _call_action("erpclaw-billing", "rate-consumption", conn,
                              rate_plan_id=rp_id,
                              consumption="600")
        assert result["status"] == "ok"

        calc = result["calculation"]
        assert calc["plan_type"] == "tiered"
        assert calc["consumption"] == "600"
        assert Decimal(calc["usage_charge"]) == Decimal("47.00")
        assert Decimal(calc["base_charge"]) == Decimal("10.00")
        assert Decimal(calc["total_charge"]) == Decimal("57.00")

        # Verify breakdown has 3 tiers
        assert len(calc["breakdown"]) == 3

        bd = calc["breakdown"]
        # Tier 1: 100 units at 0.10
        assert Decimal(bd[0]["consumption"]) == Decimal("100")
        assert Decimal(bd[0]["rate"]) == Decimal("0.10")
        assert Decimal(bd[0]["charge"]) == Decimal("10.00")
        # Tier 2: 400 units at 0.08
        assert Decimal(bd[1]["consumption"]) == Decimal("400")
        assert Decimal(bd[1]["rate"]) == Decimal("0.08")
        assert Decimal(bd[1]["charge"]) == Decimal("32.00")
        # Tier 3: 100 units at 0.05
        assert Decimal(bd[2]["consumption"]) == Decimal("100")
        assert Decimal(bd[2]["rate"]) == Decimal("0.05")
        assert Decimal(bd[2]["charge"]) == Decimal("5.00")

    # -----------------------------------------------------------------------
    # 7. Billing period creation
    # -----------------------------------------------------------------------

    def test_billing_period(self, fresh_db):
        """Create a billing period and verify open status with zero amounts."""
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        rp = _create_flat_rate_plan(conn)
        rp_id = rp["rate_plan"]["id"]

        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        result = _call_action("erpclaw-billing", "create-billing-period", conn,
                              customer_id=env["customer_id"],
                              meter_id=meter_id,
                              from_date="2026-02-01",
                              to_date="2026-02-28")
        assert result["status"] == "ok"

        bp = result["billing_period"]
        assert bp["status"] == "open"
        assert bp["period_start"] == "2026-02-01"
        assert bp["period_end"] == "2026-02-28"
        assert bp["customer_id"] == env["customer_id"]
        assert bp["meter_id"] == meter_id
        assert bp["rate_plan_id"] == rp_id
        assert Decimal(bp["grand_total"]) == Decimal("0")
        assert Decimal(bp["total_consumption"]) == Decimal("0")

        # Verify DB
        db_bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                             (bp["id"],)).fetchone()
        assert db_bp is not None
        assert db_bp["status"] == "open"

    # -----------------------------------------------------------------------
    # 8. Run billing for a period
    # -----------------------------------------------------------------------

    def test_run_billing(self, fresh_db):
        """Run billing for a period, verify rated amounts are correct.

        Flat rate plan: $0.12/unit, base $5.00
        Consumption: 200 kWh (readings: 800 -> 1000)
        Expected: usage = 200 * 0.12 = $24.00, base = $5.00, total = $29.00
        """
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        rp = _create_flat_rate_plan(conn, rate="0.12", base_charge="5.00")
        rp_id = rp["rate_plan"]["id"]

        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        # Add readings
        _add_reading(conn, meter_id, "2026-02-01", "800")
        _add_reading(conn, meter_id, "2026-02-28", "1000")
        # Consumption: 1000 - 800 = 200 kWh

        result = _call_action("erpclaw-billing", "run-billing", conn,
                              company_id=env["company_id"],
                              billing_date="2026-02-28",
                              from_date="2026-02-01",
                              to_date="2026-02-28")
        assert result["status"] == "ok"
        assert result["periods_created"] == 1

        bp_id = result["period_ids"][0]
        bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
        assert bp["status"] == "rated"
        assert bp["total_consumption"] == "200"
        assert Decimal(bp["usage_charge"]) == Decimal("24.00")
        assert Decimal(bp["base_charge"]) == Decimal("5.00")
        assert Decimal(bp["grand_total"]) == Decimal("29.00")

        # Verify total_billed in response
        assert Decimal(result["total_billed"]) == Decimal("29.00")

    # -----------------------------------------------------------------------
    # 9. Generate invoices from billing periods
    # -----------------------------------------------------------------------

    def test_generate_invoices(self, fresh_db):
        """Generate invoices from rated billing periods.

        Verifies billing period transitions from 'rated' to 'invoiced'.
        """
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        rp = _create_flat_rate_plan(conn, rate="0.10", base_charge="0")
        rp_id = rp["rate_plan"]["id"]

        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        # Add readings: consumption = 500
        _add_reading(conn, meter_id, "2026-03-01", "0")
        _add_reading(conn, meter_id, "2026-03-31", "500")

        # Run billing to get a rated period
        bill_result = _call_action("erpclaw-billing", "run-billing", conn,
                                   company_id=env["company_id"],
                                   billing_date="2026-03-31",
                                   from_date="2026-03-01",
                                   to_date="2026-03-31")
        assert bill_result["status"] == "ok"
        bp_id = bill_result["period_ids"][0]

        # Verify period is rated before invoice generation
        bp_before = conn.execute("SELECT status FROM billing_period WHERE id = ?",
                                 (bp_id,)).fetchone()
        assert bp_before["status"] == "rated"

        # Generate invoices
        result = _call_action("erpclaw-billing", "generate-invoices", conn,
                              billing_period_ids=json.dumps([bp_id]))
        assert result["status"] == "ok"
        assert result["invoiced"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["billing_period_id"] == bp_id
        assert result["results"][0]["status"] == "invoiced"

        # Verify billing period status is now invoiced
        bp_after = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                                (bp_id,)).fetchone()
        assert bp_after["status"] == "invoiced"
        assert bp_after["invoiced_at"] is not None

    # -----------------------------------------------------------------------
    # 10. Billing adjustment
    # -----------------------------------------------------------------------

    def test_billing_adjustment(self, fresh_db):
        """Add credit and late_fee adjustments, verify grand_total recalculation.

        Start: rated period with $50.00 grand_total (usage=$40, base=$10)
        Add credit adjustment of -$5.00
        Add late_fee adjustment of $3.00
        Net adjustment = -$5.00 + $3.00 = -$2.00
        New grand_total = $10.00 (base) + $40.00 (usage) + (-$2.00) (adj) = $48.00
        """
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        # Flat rate: 0.10/unit, base $10
        rp = _create_flat_rate_plan(conn, rate="0.10", base_charge="10.00")
        rp_id = rp["rate_plan"]["id"]

        m = _create_meter_with_plan(conn, env["customer_id"], rp_id)
        meter_id = m["meter"]["id"]

        # Readings: consumption = 400 -> usage = 400 * 0.10 = $40
        _add_reading(conn, meter_id, "2026-04-01", "100")
        _add_reading(conn, meter_id, "2026-04-30", "500")

        # Run billing
        bill_result = _call_action("erpclaw-billing", "run-billing", conn,
                                   company_id=env["company_id"],
                                   billing_date="2026-04-30",
                                   from_date="2026-04-01",
                                   to_date="2026-04-30")
        bp_id = bill_result["period_ids"][0]

        # Verify initial grand_total
        bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
        assert Decimal(bp["grand_total"]) == Decimal("50.00")

        # Add credit adjustment (-$5.00)
        r_credit = _call_action("erpclaw-billing", "add-billing-adjustment", conn,
                                billing_period_id=bp_id,
                                amount="-5.00",
                                adjustment_type="credit",
                                reason="Loyalty discount")
        assert r_credit["status"] == "ok"
        assert r_credit["adjustment"]["adjustment_type"] == "credit"
        assert r_credit["adjustment"]["amount"] == "-5.00"

        # Add late_fee adjustment (+$3.00)
        r_late = _call_action("erpclaw-billing", "add-billing-adjustment", conn,
                              billing_period_id=bp_id,
                              amount="3.00",
                              adjustment_type="late_fee",
                              reason="Late payment penalty")
        assert r_late["status"] == "ok"

        # Verify updated grand_total: base(10) + usage(40) + adj(-2) = 48
        bp_updated = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                                  (bp_id,)).fetchone()
        assert Decimal(bp_updated["adjustments_total"]) == Decimal("-2.00")
        assert Decimal(bp_updated["grand_total"]) == Decimal("48.00")

        # Verify adjustment records in DB
        adjs = conn.execute(
            "SELECT * FROM billing_adjustment WHERE billing_period_id = ? ORDER BY created_at",
            (bp_id,)).fetchall()
        assert len(adjs) == 2
        assert adjs[0]["adjustment_type"] == "credit"
        assert adjs[1]["adjustment_type"] == "late_fee"

    # -----------------------------------------------------------------------
    # 11. Prepaid credits
    # -----------------------------------------------------------------------

    def test_prepaid_credits(self, fresh_db):
        """Add prepaid credit and check balance."""
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        # Need a rate plan for prepaid credit (any plan will do)
        rp = _create_flat_rate_plan(conn, name="Prepaid Support Plan")
        rp_id = rp["rate_plan"]["id"]

        # Add prepaid credit of $500
        result = _call_action("erpclaw-billing", "add-prepaid-credit", conn,
                              customer_id=env["customer_id"],
                              amount="500.00",
                              valid_until="2026-12-31",
                              rate_plan_id=rp_id)
        assert result["status"] == "ok"
        credit = result["prepaid_credit"]
        assert credit["customer_id"] == env["customer_id"]
        assert credit["original_amount"] == "500.00"
        assert credit["remaining_amount"] == "500.00"
        assert credit["status"] == "active"
        assert credit["period_end"] == "2026-12-31"

        # Add a second prepaid credit of $200
        result2 = _call_action("erpclaw-billing", "add-prepaid-credit", conn,
                               customer_id=env["customer_id"],
                               amount="200.00",
                               valid_until="2026-06-30",
                               rate_plan_id=rp_id)
        assert result2["status"] == "ok"

        # Check balance: should be $700 total remaining, 2 active
        bal_result = _call_action("erpclaw-billing", "get-prepaid-balance", conn,
                                  customer_id=env["customer_id"])
        assert bal_result["status"] == "ok"
        assert bal_result["active_credits"] == 2
        assert Decimal(bal_result["total_remaining"]) == Decimal("700.00")
        assert len(bal_result["balances"]) == 2

        # Verify DB records
        db_credits = conn.execute(
            """SELECT * FROM prepaid_credit_balance
               WHERE customer_id = ? AND status = 'active'""",
            (env["customer_id"],)).fetchall()
        assert len(db_credits) == 2
        total_db = sum(Decimal(c["remaining_amount"]) for c in db_credits)
        assert total_db == Decimal("700.00")

    # -----------------------------------------------------------------------
    # 12. Multiple meters for same customer
    # -----------------------------------------------------------------------

    def test_multiple_meters(self, fresh_db):
        """Billing with multiple meters for the same customer.

        Creates two meters (electricity and water) with different rate plans,
        adds readings for both, runs billing, and verifies two separate
        billing periods with correct per-meter amounts.
        """
        conn = _wrap(fresh_db)
        env = _setup_billing_env(conn)

        # Electricity: flat rate $0.12/kWh, base $10
        rp_elec = _create_flat_rate_plan(conn, name="Electricity Flat",
                                          rate="0.12", base_charge="10.00")
        rp_elec_id = rp_elec["rate_plan"]["id"]

        # Water: flat rate $0.05/gal, base $8
        rp_water = _create_flat_rate_plan(conn, name="Water Flat",
                                           rate="0.05", base_charge="8.00")
        rp_water_id = rp_water["rate_plan"]["id"]

        # Create two meters
        m_elec = _create_meter_with_plan(conn, env["customer_id"], rp_elec_id,
                                          meter_type="electricity", unit="kWh",
                                          name="SP-Electricity")
        m_water = _create_meter_with_plan(conn, env["customer_id"], rp_water_id,
                                           meter_type="water", unit="gallons",
                                           name="SP-Water")
        elec_meter_id = m_elec["meter"]["id"]
        water_meter_id = m_water["meter"]["id"]

        # Add electricity readings: 500 -> 800 => 300 kWh
        _add_reading(conn, elec_meter_id, "2026-05-01", "500")
        _add_reading(conn, elec_meter_id, "2026-05-31", "800")

        # Add water readings: 1000 -> 2000 => 1000 gallons
        _add_reading(conn, water_meter_id, "2026-05-01", "1000")
        _add_reading(conn, water_meter_id, "2026-05-31", "2000")

        # Run billing
        result = _call_action("erpclaw-billing", "run-billing", conn,
                              company_id=env["company_id"],
                              billing_date="2026-05-31",
                              from_date="2026-05-01",
                              to_date="2026-05-31")
        assert result["status"] == "ok"
        assert result["periods_created"] == 2

        # Load both billing periods
        bps = conn.execute(
            """SELECT * FROM billing_period
               WHERE customer_id = ? AND status = 'rated'
               ORDER BY meter_id""",
            (env["customer_id"],)).fetchall()
        assert len(bps) == 2

        # Build a meter_id -> bp mapping
        bp_map = {bp["meter_id"]: bp for bp in bps}

        # Verify electricity: 300 * 0.12 = $36.00 usage + $10.00 base = $46.00
        elec_bp = bp_map[elec_meter_id]
        assert elec_bp["total_consumption"] == "300"
        assert Decimal(elec_bp["usage_charge"]) == Decimal("36.00")
        assert Decimal(elec_bp["base_charge"]) == Decimal("10.00")
        assert Decimal(elec_bp["grand_total"]) == Decimal("46.00")

        # Verify water: 1000 * 0.05 = $50.00 usage + $8.00 base = $58.00
        water_bp = bp_map[water_meter_id]
        assert water_bp["total_consumption"] == "1000"
        assert Decimal(water_bp["usage_charge"]) == Decimal("50.00")
        assert Decimal(water_bp["base_charge"]) == Decimal("8.00")
        assert Decimal(water_bp["grand_total"]) == Decimal("58.00")

        # Verify total billed matches sum
        assert Decimal(result["total_billed"]) == Decimal("104.00")
