"""Tests for erpclaw-assets: asset CRUD, categories, movements, maintenance, dashboard.

Part A pytest tests -- ~12 tests covering:
- Asset category creation and listing
- Asset creation, updating, getting, listing
- Asset movements (transfer)
- Maintenance scheduling and completion
- Status dashboard
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
    force_asset_status,
    generate_schedule,
    setup_asset_environment,
)


# ===================================================================
# 1. test_add_asset_category
# ===================================================================

def test_add_asset_category(fresh_db):
    """Create an asset category with depreciation settings."""
    conn = fresh_db
    company_id = create_test_company(conn)

    result = _call_action(
        ACTIONS["add-asset-category"], conn,
        company_id=company_id,
        name="Office Equipment",
        depreciation_method="straight_line",
        useful_life_years="5",
    )

    assert result["status"] == "ok"
    assert result["name"] == "Office Equipment"
    assert "asset_category_id" in result

    # Verify in DB
    cat = conn.execute(
        "SELECT * FROM asset_category WHERE id = ?",
        (result["asset_category_id"],),
    ).fetchone()
    assert cat is not None
    assert cat["depreciation_method"] == "straight_line"
    assert cat["useful_life_years"] == 5
    assert cat["company_id"] == company_id


# ===================================================================
# 2. test_list_asset_categories
# ===================================================================

def test_list_asset_categories(fresh_db):
    """Verify listing of asset categories for a company."""
    conn = fresh_db
    company_id = create_test_company(conn)

    # Create two categories
    create_test_asset_category(conn, company_id, name="Office Equipment",
                               depreciation_method="straight_line",
                               useful_life_years="5")
    create_test_asset_category(conn, company_id, name="Vehicles",
                               depreciation_method="double_declining",
                               useful_life_years="8")

    result = _call_action(
        ACTIONS["list-asset-categories"], conn,
        company_id=company_id,
    )

    assert result["status"] == "ok"
    assert result["total"] == 2
    names = [c["name"] for c in result["categories"]]
    assert "Office Equipment" in names
    assert "Vehicles" in names


# ===================================================================
# 3. test_add_asset
# ===================================================================

def test_add_asset(fresh_db):
    """Create an asset; verify book_value = gross_value initially."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    result = _call_action(
        ACTIONS["add-asset"], conn,
        company_id=env["company_id"],
        name="Laptop Dell XPS 15",
        asset_category_id=env["category_id"],
        gross_value="12000.00",
        salvage_value="2000.00",
        purchase_date="2026-01-15",
        depreciation_start_date="2026-02-01",
        location="HQ Office",
    )

    assert result["status"] == "ok"
    assert result["asset_name"] == "Laptop Dell XPS 15"
    assert Decimal(result["gross_value"]) == Decimal("12000.00")
    assert Decimal(result["current_book_value"]) == Decimal("12000.00")

    # Verify in DB
    asset = conn.execute(
        "SELECT * FROM asset WHERE id = ?",
        (result["asset_id"],),
    ).fetchone()
    assert asset is not None
    assert asset["status"] == "draft"
    assert Decimal(asset["accumulated_depreciation"]) == Decimal("0")
    assert Decimal(asset["gross_value"]) == Decimal("12000.00")
    assert Decimal(asset["current_book_value"]) == Decimal("12000.00")


# ===================================================================
# 4. test_add_asset_inherits_category
# ===================================================================

def test_add_asset_inherits_category(fresh_db):
    """Asset inherits depreciation method and useful_life from category
    when not explicitly provided.
    """
    conn = fresh_db
    env = setup_asset_environment(conn)

    # Category has straight_line, 5 years
    result = _call_action(
        ACTIONS["add-asset"], conn,
        company_id=env["company_id"],
        name="Standing Desk",
        asset_category_id=env["category_id"],
        gross_value="3000.00",
        salvage_value="300.00",
        purchase_date="2026-01-10",
        depreciation_start_date="2026-02-01",
    )

    assert result["status"] == "ok"

    asset = conn.execute(
        "SELECT * FROM asset WHERE id = ?",
        (result["asset_id"],),
    ).fetchone()
    assert asset["depreciation_method"] == "straight_line"
    assert asset["useful_life_years"] == 5


# ===================================================================
# 5. test_update_asset
# ===================================================================

def test_update_asset(fresh_db):
    """Update asset location and custodian."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"],
                                 location="HQ Office")

    result = _call_action(
        ACTIONS["update-asset"], conn,
        asset_id=asset_id,
        location="Branch Office",
        custodian_employee_id="emp-001",
    )

    assert result["status"] == "ok"
    assert "location" in result["updated_fields"]
    assert "custodian_employee_id" in result["updated_fields"]

    # Verify in DB
    asset = conn.execute(
        "SELECT * FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    assert asset["location"] == "Branch Office"
    assert asset["custodian_employee_id"] == "emp-001"


# ===================================================================
# 6. test_update_asset_non_draft_limited
# ===================================================================

def test_update_asset_non_draft_limited(fresh_db):
    """Cannot change gross_value after submit -- update-asset does not
    support changing gross_value at all (only name, location, custodian,
    warranty, status). Verify that changing status to submitted works,
    and that scrapped/sold assets cannot be updated.
    """
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"])

    # Submit the asset
    submit_asset(conn, asset_id)

    # Can still update location on submitted asset
    result = _call_action(
        ACTIONS["update-asset"], conn,
        asset_id=asset_id,
        location="Warehouse B",
    )
    assert result["status"] == "ok"

    # Force status to scrapped (bypasses disposal workflow for guard-condition test)
    force_asset_status(conn, asset_id, "scrapped")

    result = _call_action(
        ACTIONS["update-asset"], conn,
        asset_id=asset_id,
        location="Office C",
    )
    assert result["status"] == "error"
    assert "scrapped" in result["message"]


# ===================================================================
# 7. test_get_asset
# ===================================================================

def test_get_asset(fresh_db):
    """Get asset with schedule, movements, and maintenance records."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_submitted_asset(conn, env["company_id"], env["category_id"])

    # Generate depreciation schedule
    generate_schedule(conn, asset_id)

    # Record a movement
    _call_action(
        ACTIONS["record-asset-movement"], conn,
        asset_id=asset_id,
        movement_type="transfer",
        movement_date="2026-02-15",
        to_location="Branch Office",
    )

    # Schedule maintenance
    _call_action(
        ACTIONS["schedule-maintenance"], conn,
        asset_id=asset_id,
        maintenance_type="preventive",
        scheduled_date="2026-06-01",
        description="Annual cleaning",
    )

    # Get the asset
    result = _call_action(
        ACTIONS["get-asset"], conn,
        asset_id=asset_id,
    )

    assert result["status"] == "ok"
    asset = result["asset"]
    assert asset["id"] == asset_id
    assert asset["asset_name"] == "Laptop Dell XPS"
    assert len(asset["depreciation_schedule"]) > 0
    assert len(asset["movements"]) == 1
    assert asset["movements"][0]["movement_type"] == "transfer"
    assert len(asset["maintenance"]) == 1
    assert asset["maintenance"][0]["maintenance_type"] == "preventive"
    assert asset["category"] is not None
    assert asset["category"]["name"] == "Office Equipment"


# ===================================================================
# 8. test_list_assets
# ===================================================================

def test_list_assets(fresh_db):
    """List assets with filtering by category, status, and search."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    # Create a second category
    cat2_id = create_test_asset_category(
        conn, env["company_id"], name="Vehicles",
        depreciation_method="double_declining",
        useful_life_years="8",
    )

    # Create assets in different categories
    asset1 = create_test_asset(conn, env["company_id"], env["category_id"],
                               name="Laptop A", gross_value="5000.00",
                               salvage_value="500.00")
    asset2 = create_test_asset(conn, env["company_id"], env["category_id"],
                               name="Laptop B", gross_value="6000.00",
                               salvage_value="600.00")
    asset3 = create_test_asset(conn, env["company_id"], cat2_id,
                               name="Toyota Camry", gross_value="30000.00",
                               salvage_value="5000.00")

    # Submit asset1
    submit_asset(conn, asset1)

    # List all assets for company
    result = _call_action(
        ACTIONS["list-assets"], conn,
        company_id=env["company_id"],
    )
    assert result["status"] == "ok"
    assert result["total"] == 3

    # Filter by category
    result = _call_action(
        ACTIONS["list-assets"], conn,
        company_id=env["company_id"],
        asset_category_id=env["category_id"],
    )
    assert result["total"] == 2

    # Filter by status
    result = _call_action(
        ACTIONS["list-assets"], conn,
        company_id=env["company_id"],
        status="submitted",
    )
    assert result["total"] == 1
    assert result["assets"][0]["id"] == asset1

    # Search by name
    result = _call_action(
        ACTIONS["list-assets"], conn,
        company_id=env["company_id"],
        search="Camry",
    )
    assert result["total"] == 1
    assert result["assets"][0]["id"] == asset3


# ===================================================================
# 9. test_record_asset_movement
# ===================================================================

def test_record_asset_movement(fresh_db):
    """Record a transfer movement; verify asset location is updated."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"],
                                 location="HQ Office")

    result = _call_action(
        ACTIONS["record-asset-movement"], conn,
        asset_id=asset_id,
        movement_type="transfer",
        movement_date="2026-03-01",
        to_location="Branch Office",
        reason="Office relocation",
    )

    assert result["status"] == "ok"
    assert result["movement_type"] == "transfer"
    assert "movement_id" in result

    # Verify asset location updated
    asset = conn.execute(
        "SELECT location FROM asset WHERE id = ?", (asset_id,),
    ).fetchone()
    assert asset["location"] == "Branch Office"

    # Verify movement record in DB
    movement = conn.execute(
        "SELECT * FROM asset_movement WHERE id = ?",
        (result["movement_id"],),
    ).fetchone()
    assert movement is not None
    assert movement["from_location"] == "HQ Office"
    assert movement["to_location"] == "Branch Office"
    assert movement["reason"] == "Office relocation"


# ===================================================================
# 10. test_schedule_maintenance
# ===================================================================

def test_schedule_maintenance(fresh_db):
    """Schedule planned maintenance for an asset."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"])

    result = _call_action(
        ACTIONS["schedule-maintenance"], conn,
        asset_id=asset_id,
        maintenance_type="preventive",
        scheduled_date="2026-06-01",
        description="Quarterly inspection",
        next_due_date="2026-09-01",
    )

    assert result["status"] == "ok"
    assert result["maintenance_type"] == "preventive"
    assert result["scheduled_date"] == "2026-06-01"
    assert "maintenance_id" in result

    # Verify in DB
    maint = conn.execute(
        "SELECT * FROM asset_maintenance WHERE id = ?",
        (result["maintenance_id"],),
    ).fetchone()
    assert maint is not None
    assert maint["status"] == "planned"
    assert maint["description"] == "Quarterly inspection"
    assert maint["next_due_date"] == "2026-09-01"


# ===================================================================
# 11. test_complete_maintenance
# ===================================================================

def test_complete_maintenance(fresh_db):
    """Complete a maintenance task with actual cost."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"])

    # Schedule maintenance first
    sched_result = _call_action(
        ACTIONS["schedule-maintenance"], conn,
        asset_id=asset_id,
        maintenance_type="corrective",
        scheduled_date="2026-03-01",
        description="Screen repair",
    )
    maint_id = sched_result["maintenance_id"]

    # Complete the maintenance
    result = _call_action(
        ACTIONS["complete-maintenance"], conn,
        maintenance_id=maint_id,
        actual_date="2026-03-02",
        cost="250.00",
        performed_by="IT Tech - John",
        description="Screen replaced with new panel",
    )

    assert result["status"] == "ok"
    assert result["actual_date"] == "2026-03-02"
    assert result["cost"] == "250.00"

    # Verify in DB
    maint = conn.execute(
        "SELECT * FROM asset_maintenance WHERE id = ?", (maint_id,),
    ).fetchone()
    assert maint["status"] == "completed"
    assert maint["actual_date"] == "2026-03-02"
    assert maint["cost"] == "250.00"
    assert maint["performed_by"] == "IT Tech - John"
    assert maint["description"] == "Screen replaced with new panel"

    # Completing again should fail
    result2 = _call_action(
        ACTIONS["complete-maintenance"], conn,
        maintenance_id=maint_id,
        actual_date="2026-03-05",
        cost="300.00",
    )
    assert result2["status"] == "error"
    assert "already completed" in result2["message"]


# ===================================================================
# 12. test_status_dashboard
# ===================================================================

def test_status_dashboard(fresh_db):
    """Verify status dashboard counts and totals."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    # Create two assets: one draft, one submitted
    asset1 = create_test_asset(conn, env["company_id"], env["category_id"],
                               name="Laptop A", gross_value="10000.00",
                               salvage_value="1000.00")
    asset2 = create_submitted_asset(conn, env["company_id"], env["category_id"],
                                    name="Laptop B", gross_value="15000.00",
                                    salvage_value="2000.00")

    # Generate depreciation schedule for submitted asset
    generate_schedule(conn, asset2)

    result = _call_action(
        ACTIONS["status"], conn,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["total_assets"] == 2
    assert Decimal(result["total_gross_value"]) == Decimal("25000.00")
    assert Decimal(result["total_book_value"]) == Decimal("25000.00")

    # Verify assets_by_status counts
    by_status = result["assets_by_status"]
    assert by_status.get("draft", 0) == 1
    assert by_status.get("submitted", 0) == 1

    # Pending depreciation entries should exist (60 months for the submitted asset)
    assert result["pending_depreciation_entries"] == 60


# ===================================================================
# 12b. test_add_asset_category_duplicate_name
# ===================================================================

def test_add_asset_category_duplicate_name(fresh_db):
    """Cannot create two categories with the same name in the same company."""
    conn = fresh_db
    company_id = create_test_company(conn)

    result1 = _call_action(
        ACTIONS["add-asset-category"], conn,
        company_id=company_id,
        name="Office Equipment",
        depreciation_method="straight_line",
        useful_life_years="5",
    )
    assert result1["status"] == "ok"

    result2 = _call_action(
        ACTIONS["add-asset-category"], conn,
        company_id=company_id,
        name="Office Equipment",
        depreciation_method="double_declining",
        useful_life_years="3",
    )
    assert result2["status"] == "error"
    assert "already exists" in result2["message"]


# ===================================================================
# 12c. test_add_asset_validates_salvage_less_than_gross
# ===================================================================

def test_add_asset_validates_salvage_less_than_gross(fresh_db):
    """Salvage value must be less than gross value."""
    conn = fresh_db
    env = setup_asset_environment(conn)

    result = _call_action(
        ACTIONS["add-asset"], conn,
        company_id=env["company_id"],
        name="Bad Asset",
        asset_category_id=env["category_id"],
        gross_value="5000.00",
        salvage_value="6000.00",
    )
    assert result["status"] == "error"
    assert "salvage" in result["message"].lower()


# ===================================================================
# 12d. test_dispose_draft_asset_rejected
# ===================================================================

def test_dispose_draft_asset_rejected(fresh_db):
    """Cannot dispose a draft asset -- must be submitted first."""
    conn = fresh_db
    env = setup_asset_environment(conn)
    asset_id = create_test_asset(conn, env["company_id"], env["category_id"])

    result = _call_action(
        ACTIONS["dispose-asset"], conn,
        asset_id=asset_id,
        disposal_date="2026-05-01",
        disposal_method="scrap",
        cost_center_id=env["cost_center_id"],
    )
    assert result["status"] == "error"
    assert "draft" in result["message"].lower()
