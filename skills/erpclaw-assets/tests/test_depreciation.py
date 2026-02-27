"""Tests for erpclaw-assets: depreciation scheduling, posting, disposal, reports.

Part A pytest tests -- ~10 tests covering:
- Depreciation schedule generation (straight_line, double_declining)
- Depreciation posting and asset value updates
- Batch depreciation run
- Asset disposal (sale at gain, sale at loss, scrap)
- Asset register and depreciation summary reports
"""
import os
import sys
from decimal import Decimal

import pytest

# Add scripts to path for importing db_query
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from db_query import ACTIONS  # noqa: E402
from helpers import (  # noqa: E402
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_naming_series,
    create_test_account,
    create_test_cost_center,
    create_test_asset_category,
    create_test_asset,
    create_submitted_asset,
    submit_asset,
    generate_schedule,
    setup_asset_environment,
)


# ===================================================================
# 13. test_generate_depreciation_schedule_straight_line
# ===================================================================

def test_generate_depreciation_schedule_straight_line(fresh_db):
    """Generate straight-line schedule; verify monthly amounts,
    accumulated, and book_value_after track correctly.

    Asset: gross=12000, salvage=2000, useful_life=5 years (60 months)
    Monthly amount = (12000 - 2000) / 60 = 166.67
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    result = _call_action(
        ACTIONS["generate-depreciation-schedule"], conn,
        asset_id=asset_id,
    )

    assert result["status"] == "ok"
    assert result["depreciation_method"] == "straight_line"
    assert Decimal(result["total_depreciable_amount"]) == Decimal("10000.00")
    assert result["entries_generated"] == 60

    schedule = result["schedule"]

    # First entry
    first = schedule[0]
    assert first["schedule_date"] == "2026-02-01"
    monthly = Decimal("10000.00") / Decimal("60")
    expected_monthly = monthly.quantize(Decimal("0.01"))
    assert Decimal(first["depreciation_amount"]) == expected_monthly

    # Verify accumulated tracking
    running_accum = Decimal("0")
    for i, entry in enumerate(schedule):
        dep_amt = Decimal(entry["depreciation_amount"])
        running_accum += dep_amt
        assert Decimal(entry["accumulated_amount"]) == running_accum
        expected_bv = Decimal("12000.00") - running_accum
        assert Decimal(entry["book_value_after"]) == expected_bv

    # Final entry: total accumulated should equal depreciable amount
    last = schedule[-1]
    assert Decimal(last["accumulated_amount"]) == Decimal("10000.00")
    assert Decimal(last["book_value_after"]) == Decimal("2000.00")

    # All entries should be pending
    for entry in schedule:
        assert entry["status"] == "pending"


# ===================================================================
# 14. test_generate_depreciation_schedule_double_declining
# ===================================================================

def test_generate_depreciation_schedule_double_declining(fresh_db):
    """Generate double-declining schedule; verify amounts decrease over time
    and book_value never goes below salvage_value.

    Asset: gross=12000, salvage=2000, useful_life=5 years
    Annual rate = 2/5 = 0.4 => monthly rate = 0.4/12
    """
    conn = fresh_db
    env = setup_asset_environment(conn)

    # Create category with double_declining method
    cat_id = create_test_asset_category(
        conn, env["company_id"], name="Computer Equipment DD",
        depreciation_method="double_declining",
        useful_life_years="5",
        asset_account_id=env["asset_account_id"],
        depreciation_account_id=env["depreciation_account_id"],
        accumulated_depreciation_account_id=env["accumulated_depreciation_account_id"],
    )

    asset_id = create_submitted_asset(
        conn, env["company_id"], cat_id,
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    result = _call_action(
        ACTIONS["generate-depreciation-schedule"], conn,
        asset_id=asset_id,
    )

    assert result["status"] == "ok"
    assert result["depreciation_method"] == "double_declining"

    schedule = result["schedule"]
    assert len(schedule) > 0

    # DDR: early months have larger amounts; later months decrease
    first_amount = Decimal(schedule[0]["depreciation_amount"])
    last_amount = Decimal(schedule[-1]["depreciation_amount"])
    assert first_amount >= last_amount

    # First few months should have same amount (book value doesn't drop yet)
    # Then amounts decrease as book value decreases
    # The key invariant: book_value_after never goes below salvage
    for entry in schedule:
        bv_after = Decimal(entry["book_value_after"])
        assert bv_after >= Decimal("2000.00")

    # Accumulated should track correctly
    running_accum = Decimal("0")
    for entry in schedule:
        running_accum += Decimal(entry["depreciation_amount"])
        assert Decimal(entry["accumulated_amount"]) == running_accum

    # Final book value should be at or near salvage value
    final_bv = Decimal(schedule[-1]["book_value_after"])
    assert final_bv == Decimal("2000.00")


# ===================================================================
# 15. test_post_depreciation
# ===================================================================

def test_post_depreciation(fresh_db):
    """Post a single depreciation entry; verify GL entries
    DR Depreciation Expense / CR Accumulated Depreciation.
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate schedule
    sched_result = generate_schedule(conn, asset_id)
    first_schedule_id = sched_result["schedule"][0]["id"]
    first_amount = sched_result["schedule"][0]["depreciation_amount"]

    # Post the first entry
    result = _call_action(
        ACTIONS["post-depreciation"], conn,
        depreciation_schedule_id=first_schedule_id,
        posting_date="2026-02-01",
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"
    assert result["depreciation_amount"] == first_amount
    assert len(result["gl_entry_ids"]) == 2

    # Verify GL entries
    gl_entries = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'depreciation_entry' AND voucher_id = ?
           AND is_cancelled = 0
           ORDER BY debit DESC""",
        (first_schedule_id,),
    ).fetchall()

    assert len(gl_entries) == 2

    # Debit: Depreciation Expense
    dr_entry = [e for e in gl_entries if Decimal(e["debit"]) > 0][0]
    assert dr_entry["account_id"] == env["depreciation_account_id"]
    assert Decimal(dr_entry["debit"]) == Decimal(first_amount)
    assert Decimal(dr_entry["credit"]) == Decimal("0")

    # Credit: Accumulated Depreciation
    cr_entry = [e for e in gl_entries if Decimal(e["credit"]) > 0][0]
    assert cr_entry["account_id"] == env["accumulated_depreciation_account_id"]
    assert Decimal(cr_entry["credit"]) == Decimal(first_amount)
    assert Decimal(cr_entry["debit"]) == Decimal("0")

    # Verify schedule entry is now 'posted'
    sched = conn.execute(
        "SELECT * FROM depreciation_schedule WHERE id = ?",
        (first_schedule_id,),
    ).fetchone()
    assert sched["status"] == "posted"


# ===================================================================
# 16. test_post_depreciation_updates_asset
# ===================================================================

def test_post_depreciation_updates_asset(fresh_db):
    """After posting depreciation, asset book_value and accumulated_depreciation
    are updated correctly.
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate schedule
    sched_result = generate_schedule(conn, asset_id)
    first_schedule_id = sched_result["schedule"][0]["id"]
    dep_amount = Decimal(sched_result["schedule"][0]["depreciation_amount"])

    # Post
    result = _call_action(
        ACTIONS["post-depreciation"], conn,
        depreciation_schedule_id=first_schedule_id,
        posting_date="2026-02-01",
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"

    # Verify asset values
    assert Decimal(result["new_accumulated_depreciation"]) == dep_amount
    expected_book_value = Decimal("12000.00") - dep_amount
    assert Decimal(result["new_book_value"]) == expected_book_value

    # Verify directly in DB
    asset = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    assert Decimal(asset["accumulated_depreciation"]) == dep_amount
    assert Decimal(asset["current_book_value"]) == expected_book_value


# ===================================================================
# 17. test_run_depreciation
# ===================================================================

def test_run_depreciation(fresh_db):
    """Batch post multiple pending depreciation entries."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate schedule
    sched_result = generate_schedule(conn, asset_id)
    monthly_amount = Decimal(sched_result["schedule"][0]["depreciation_amount"])

    # Run depreciation for 3 months (Feb, Mar, Apr 2026)
    result = _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-04-30",
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"
    assert result["entries_posted"] == 3  # Feb, Mar, Apr

    # Verify asset values after 3 months
    asset = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    expected_accum = monthly_amount * 3
    assert Decimal(asset["accumulated_depreciation"]) == expected_accum
    assert Decimal(asset["current_book_value"]) == Decimal("12000.00") - expected_accum

    # Verify 3 schedule entries are posted
    posted_count = conn.execute(
        """SELECT COUNT(*) as cnt FROM depreciation_schedule
           WHERE asset_id = ? AND status = 'posted'""",
        (asset_id,),
    ).fetchone()
    assert posted_count["cnt"] == 3

    # Verify 6 GL entries (2 per posting)
    gl_count = conn.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'depreciation_entry' AND is_cancelled = 0""",
    ).fetchone()
    assert gl_count["cnt"] == 6


# ===================================================================
# 18. test_dispose_asset_sale_at_gain
# ===================================================================

def test_dispose_asset_sale_at_gain(fresh_db):
    """Dispose asset via sale above book value; verify gain GL entries.

    Asset: gross=12000, salvage=2000, after 3 months depreciation:
    - accumulated_dep = 166.67 * 3 = 500.01
    - book_value = 12000 - 500.01 = 11499.99
    Sale at 12500 => gain = 12500 - 11499.99 = 1000.01
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate and post 3 months of depreciation
    sched_result = generate_schedule(conn, asset_id)
    _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-04-30",
        cost_center_id=env["cost_center_id"],
    )

    # Read current book value from DB
    asset_before = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    book_value_before = Decimal(asset_before["current_book_value"])
    accum_dep_before = Decimal(asset_before["accumulated_depreciation"])

    # Sell at a gain
    sale_amount = Decimal("12500.00")

    result = _call_action(
        ACTIONS["dispose-asset"], conn,
        asset_id=asset_id,
        disposal_date="2026-05-01",
        disposal_method="sale",
        sale_amount=str(sale_amount),
        buyer_details="Acme Corp",
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"
    assert result["disposal_method"] == "sale"
    assert result["new_status"] == "sold"
    assert Decimal(result["sale_amount"]) == sale_amount
    assert Decimal(result["book_value_at_disposal"]) == book_value_before

    gain = sale_amount - book_value_before
    assert Decimal(result["gain_or_loss"]) == gain
    assert gain > 0  # This is a gain

    # Verify GL entries were created
    assert len(result["gl_entry_ids"]) > 0

    # Verify asset status updated
    asset_after = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    assert asset_after["status"] == "sold"
    assert Decimal(asset_after["current_book_value"]) == Decimal("0")

    # Verify disposal record
    disposal = conn.execute(
        "SELECT * FROM asset_disposal WHERE asset_id = ?", (asset_id,),
    ).fetchone()
    assert disposal is not None
    assert disposal["disposal_method"] == "sale"
    assert Decimal(disposal["sale_amount"]) == sale_amount
    assert Decimal(disposal["gain_or_loss"]) == gain


# ===================================================================
# 19. test_dispose_asset_sale_at_loss
# ===================================================================

def test_dispose_asset_sale_at_loss(fresh_db):
    """Dispose asset via sale below book value; verify loss GL entries."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate and post 3 months of depreciation
    generate_schedule(conn, asset_id)
    _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-04-30",
        cost_center_id=env["cost_center_id"],
    )

    # Read current book value
    asset_before = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    book_value_before = Decimal(asset_before["current_book_value"])

    # Sell at a loss (below book value)
    sale_amount = Decimal("8000.00")
    assert sale_amount < book_value_before

    result = _call_action(
        ACTIONS["dispose-asset"], conn,
        asset_id=asset_id,
        disposal_date="2026-05-01",
        disposal_method="sale",
        sale_amount=str(sale_amount),
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"
    assert result["disposal_method"] == "sale"
    assert result["new_status"] == "sold"

    loss = sale_amount - book_value_before
    assert Decimal(result["gain_or_loss"]) == loss
    assert loss < 0  # This is a loss

    # Verify GL entries exist
    gl_entries = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'asset_disposal' AND is_cancelled = 0""",
    ).fetchall()
    assert len(gl_entries) > 0

    # Verify total debits = total credits (balanced entries)
    total_debit = sum(Decimal(e["debit"]) for e in gl_entries)
    total_credit = sum(Decimal(e["credit"]) for e in gl_entries)
    assert total_debit == total_credit


# ===================================================================
# 20. test_dispose_asset_scrap
# ===================================================================

def test_dispose_asset_scrap(fresh_db):
    """Scrap an asset; verify write-off GL entries.

    For scrap: sale_amount=0, loss = book_value.
    GL:
    - DR Accumulated Depreciation (accum_dep)
    - DR Loss (book_value)
    - CR Fixed Asset Account (gross_value)
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        gross_value="12000.00", salvage_value="2000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate and post 3 months of depreciation
    generate_schedule(conn, asset_id)
    _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-04-30",
        cost_center_id=env["cost_center_id"],
    )

    # Read current book value
    asset_before = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    book_value_before = Decimal(asset_before["current_book_value"])
    accum_dep_before = Decimal(asset_before["accumulated_depreciation"])

    result = _call_action(
        ACTIONS["dispose-asset"], conn,
        asset_id=asset_id,
        disposal_date="2026-05-15",
        disposal_method="scrap",
        cost_center_id=env["cost_center_id"],
    )

    assert result["status"] == "ok"
    assert result["disposal_method"] == "scrap"
    assert result["new_status"] == "scrapped"
    assert Decimal(result["sale_amount"]) == Decimal("0")

    # Loss should equal the book value (negative gain_or_loss)
    expected_loss = Decimal("0") - book_value_before
    assert Decimal(result["gain_or_loss"]) == expected_loss

    # Verify asset status
    asset_after = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    assert asset_after["status"] == "scrapped"
    assert Decimal(asset_after["current_book_value"]) == Decimal("0")

    # Verify GL entries are balanced
    gl_entries = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'asset_disposal'
           AND voucher_id = ?
           AND is_cancelled = 0""",
        (result["disposal_id"],),
    ).fetchall()

    total_debit = sum(Decimal(e["debit"]) for e in gl_entries)
    total_credit = sum(Decimal(e["credit"]) for e in gl_entries)
    assert total_debit == total_credit
    assert total_credit == Decimal("12000.00")  # CR Fixed Asset = gross_value

    # Verify disposal record
    disposal = conn.execute(
        "SELECT * FROM asset_disposal WHERE id = ?",
        (result["disposal_id"],),
    ).fetchone()
    assert Decimal(disposal["book_value_at_disposal"]) == book_value_before
    assert Decimal(disposal["sale_amount"]) == Decimal("0")


# ===================================================================
# 21. test_asset_register_report
# ===================================================================

def test_asset_register_report(fresh_db):
    """Asset register report shows correct values for all assets."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    # Create two assets
    asset1 = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        name="Laptop A", gross_value="10000.00", salvage_value="1000.00",
        depreciation_start_date="2026-02-01",
    )
    asset2 = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        name="Laptop B", gross_value="8000.00", salvage_value="800.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate and post depreciation for both
    generate_schedule(conn, asset1)
    generate_schedule(conn, asset2)

    _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-03-31",
        cost_center_id=env["cost_center_id"],
    )

    # Get report
    result = _call_action(
        ACTIONS["asset-register-report"], conn,
        company_id=env["company_id"],
        as_of_date="2026-03-31",
    )

    assert result["status"] == "ok"
    assert result["report"] == "Asset Register"
    assert len(result["assets"]) == 2

    summary = result["summary"]
    assert summary["total_assets"] == 2
    assert Decimal(summary["total_gross_value"]) == Decimal("18000.00")

    # Verify each asset has correct gross_value
    asset_map = {a["asset_id"]: a for a in result["assets"]}
    assert Decimal(asset_map[asset1]["gross_value"]) == Decimal("10000.00")
    assert Decimal(asset_map[asset2]["gross_value"]) == Decimal("8000.00")

    # Verify accumulated depreciation is non-zero (2 months posted)
    for a in result["assets"]:
        assert Decimal(a["accumulated_depreciation"]) > Decimal("0")
        assert Decimal(a["current_book_value"]) < Decimal(a["gross_value"])

    # Total book value should be gross - accumulated
    total_accum = Decimal(summary["total_accumulated_depreciation"])
    total_bv = Decimal(summary["total_book_value"])
    assert total_bv == Decimal(summary["total_gross_value"]) - total_accum


# ===================================================================
# 22. test_depreciation_summary
# ===================================================================

def test_depreciation_summary(fresh_db):
    """Depreciation summary report shows correct period totals."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    asset_id = create_submitted_asset(
        conn, env["company_id"], env["category_id"],
        name="Server Rack", gross_value="24000.00", salvage_value="4000.00",
        depreciation_start_date="2026-02-01",
    )

    # Generate and post 3 months of depreciation
    generate_schedule(conn, asset_id)
    _call_action(
        ACTIONS["run-depreciation"], conn,
        company_id=env["company_id"],
        posting_date="2026-04-30",
        cost_center_id=env["cost_center_id"],
    )

    # Monthly amount = (24000 - 4000) / 60 = 333.33
    monthly_amount = (Decimal("20000.00") / Decimal("60")).quantize(Decimal("0.01"))

    # Get summary for Feb-Apr 2026
    result = _call_action(
        ACTIONS["depreciation-summary"], conn,
        company_id=env["company_id"],
        from_date="2026-02-01",
        to_date="2026-04-30",
    )

    assert result["status"] == "ok"
    assert result["report"] == "Depreciation Summary"

    # Should have one category
    assert len(result["categories"]) == 1
    cat = result["categories"][0]
    assert cat["category_name"] == "Office Equipment"

    # Total depreciation for the period = 3 months
    total_dep = Decimal(cat["total_depreciation"])
    expected_total = monthly_amount * 3
    assert total_dep == expected_total

    # Grand total should match
    assert Decimal(result["grand_total_depreciation"]) == expected_total

    # Should have one asset in the category
    assert len(cat["assets"]) == 1
    asset_entry = cat["assets"][0]
    assert asset_entry["asset_name"] == "Server Rack"
    assert asset_entry["entries_count"] == 3
    assert Decimal(asset_entry["total_depreciation"]) == expected_total

    # Summary with date filter that excludes some entries
    result2 = _call_action(
        ACTIONS["depreciation-summary"], conn,
        company_id=env["company_id"],
        from_date="2026-03-01",
        to_date="2026-03-31",
    )
    assert result2["status"] == "ok"
    # Only March entry
    if len(result2["categories"]) > 0:
        cat2 = result2["categories"][0]
        assert cat2["assets"][0]["entries_count"] == 1
        assert Decimal(cat2["total_depreciation"]) == monthly_amount
